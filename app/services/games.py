"""Game CRUD and serialization."""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..errors import ApiError, not_found
from ..models import Blob, Game, GameRoot, Machine, RootOverride, Version, VersionFile
from ..schemas import GameCreate, GamePatch, RetentionPolicy
from ..timeutil import iso, utcnow


def exe_names(game: Game) -> list[str]:
    return json.loads(game.exe_names_json or "[]")


def excludes(game: Game) -> list[str]:
    return json.loads(game.excludes_json or "[]")


def retention(game: Game) -> Optional[RetentionPolicy]:
    if not game.retention_json:
        return None
    return RetentionPolicy.model_validate(json.loads(game.retention_json))


def get_game(db: Session, game_id: str, include_deleted: bool = False) -> Game:
    game = db.get(Game, game_id)
    if game is None or (game.deleted_at is not None and not include_deleted):
        raise not_found("Game")
    return game


def list_games(db: Session, include_deleted: bool = False) -> list[Game]:
    q = select(Game)
    if not include_deleted:
        q = q.where(Game.deleted_at.is_(None))
    return list(db.scalars(q.order_by(func.lower(Game.name))))


def overrides_for(db: Session, game_ids: list[str], machine_id: Optional[str]) -> dict:
    """{(game_id, alias): path_template} for one machine."""
    if not machine_id or not game_ids:
        return {}
    rows = db.scalars(
        select(RootOverride).where(
            RootOverride.machine_id == machine_id, RootOverride.game_id.in_(game_ids)
        )
    )
    return {(r.game_id, r.alias): r.path_template for r in rows}


def serialize(game: Game, overrides: Optional[dict] = None) -> dict:
    overrides = overrides or {}
    pol = retention(game)
    return {
        "id": game.id,
        "name": game.name,
        "roots": [
            {
                "alias": r.alias,
                "path_template": r.path_template,
                "resolved_template": overrides.get((game.id, r.alias), r.path_template),
            }
            for r in game.roots
        ],
        "exe_names": exe_names(game),
        "excludes": excludes(game),
        "sync_mode": game.sync_mode,
        "retention": pol.model_dump() if pol else None,
        "head_version_id": game.head_version_id,
        "created_at": iso(game.created_at),
        "updated_at": iso(game.updated_at),
        "deleted_at": iso(game.deleted_at),
    }


def _set_roots(db: Session, game: Game, roots) -> None:
    new_aliases = {r.alias for r in roots}
    removed = [r.alias for r in game.roots if r.alias not in new_aliases]
    if removed:
        db.execute(
            delete(RootOverride).where(
                RootOverride.game_id == game.id, RootOverride.alias.in_(removed)
            )
        )
    existing = {r.alias: r for r in game.roots}
    for row in list(game.roots):
        if row.alias not in new_aliases:
            game.roots.remove(row)
    for i, r in enumerate(roots):
        row = existing.get(r.alias)
        if row is None:
            row = GameRoot(alias=r.alias)
            game.roots.append(row)
        row.path_template = r.path_template
        row.position = i
    game.roots.sort(key=lambda row: row.position)


def create_game(db: Session, body: GameCreate) -> Game:
    game = Game(
        name=body.name,
        exe_names_json=json.dumps(body.exe_names),
        excludes_json=json.dumps(body.excludes),
        sync_mode=body.sync_mode,
        retention_json=json.dumps(body.retention.model_dump()) if body.retention else None,
    )
    game.roots = [
        GameRoot(alias=r.alias, path_template=r.path_template, position=i)
        for i, r in enumerate(body.roots)
    ]
    db.add(game)
    db.commit()
    return game


def update_game(db: Session, game: Game, body: GamePatch) -> Game:
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        game.name = body.name
    if "roots" in fields and body.roots is not None:
        _set_roots(db, game, body.roots)
    if "exe_names" in fields and body.exe_names is not None:
        game.exe_names_json = json.dumps(body.exe_names)
    if "excludes" in fields and body.excludes is not None:
        game.excludes_json = json.dumps(body.excludes)
    if "sync_mode" in fields and body.sync_mode is not None:
        game.sync_mode = body.sync_mode
    if "retention" in fields:
        game.retention_json = json.dumps(body.retention.model_dump()) if body.retention else None
    game.updated_at = utcnow()
    db.commit()
    return game


def soft_delete(db: Session, game: Game) -> None:
    game.deleted_at = utcnow()
    game.updated_at = game.deleted_at
    db.commit()


def restore(db: Session, game: Game) -> None:
    game.deleted_at = None
    game.updated_at = utcnow()
    db.commit()


def purge(db: Session, game: Game) -> None:
    """Hard delete a soft-deleted game and all its versions. Blobs are left for GC."""
    if game.deleted_at is None:
        raise ApiError(409, "not_deleted", "Delete the game before purging it")
    game.head_version_id = None
    db.flush()
    db.execute(delete(Version).where(Version.game_id == game.id))
    db.delete(game)
    db.commit()


def set_override(
    db: Session, game: Game, alias: str, machine_id: str, path_template: Optional[str]
) -> None:
    if alias not in {r.alias for r in game.roots}:
        raise not_found("Root")
    if db.get(Machine, machine_id) is None:
        raise not_found("Machine")
    row = db.get(RootOverride, (game.id, alias, machine_id))
    if path_template is None or not path_template.strip():
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(
            RootOverride(
                game_id=game.id, alias=alias, machine_id=machine_id, path_template=path_template
            )
        )
    else:
        row.path_template = path_template
    game.updated_at = utcnow()
    db.commit()


def all_overrides(db: Session, game_id: str) -> list[RootOverride]:
    return list(db.scalars(select(RootOverride).where(RootOverride.game_id == game_id)))


def game_stats(db: Session) -> dict[str, dict]:
    """Per-game version count, last activity and stored size of referenced blobs.

    Stored size counts each blob once per game, so it reads as "what this game
    costs on disk", though blobs shared between games are counted in each.
    """
    stats: dict[str, dict] = {}
    for game_id, count, last in db.execute(
        select(Version.game_id, func.count(Version.id), func.max(Version.created_at)).group_by(
            Version.game_id
        )
    ):
        stats[game_id] = {"version_count": count, "last_version_at": last, "stored_size": 0}
    per_game_blobs = (
        select(Version.game_id.label("game_id"), VersionFile.blob_hash.label("h"))
        .join(VersionFile, VersionFile.version_id == Version.id)
        .distinct()
        .subquery()
    )
    for game_id, stored in db.execute(
        select(per_game_blobs.c.game_id, func.sum(Blob.stored_size))
        .join(Blob, Blob.hash == per_game_blobs.c.h)
        .group_by(per_game_blobs.c.game_id)
    ):
        stats.setdefault(game_id, {"version_count": 0, "last_version_at": None})
        stats[game_id]["stored_size"] = int(stored or 0)
    return stats
