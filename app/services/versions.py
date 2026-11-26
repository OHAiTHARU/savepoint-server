"""Version creation, promotion, diffing and listing."""

from __future__ import annotations

import os
import tempfile
import threading
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..blobstore import BlobStore
from ..errors import ApiError, bad_request, not_found
from ..models import Blob, Game, Version, VersionFile, VersionTag
from ..schemas import VERSION_TYPES, VersionCreate, VersionPatch
from ..timeutil import iso, to_utc, utcnow

# One writer per game at a time, so "compare with head, then insert" is atomic
# within this process (the server runs a single worker).
_game_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def game_lock(game_id: str) -> threading.Lock:
    with _locks_guard:
        return _game_locks[game_id]


# ---------------------------------------------------------------- validation


def validate_path(path: str, aliases: set[str]) -> str:
    if "\\" in path:
        raise bad_request("invalid_path", f"Use forward slashes: {path!r}")
    if path.startswith("/") or "\x00" in path:
        raise bad_request("invalid_path", f"Invalid path: {path!r}")
    alias, sep, rest = path.partition("/")
    if not sep or not rest:
        raise bad_request("invalid_path", f"Path must be '<root alias>/<relative path>': {path!r}")
    if alias not in aliases:
        raise bad_request("unknown_root", f"Unknown root alias {alias!r} in {path!r}")
    for seg in rest.split("/"):
        if seg in ("", ".", ".."):
            raise bad_request("invalid_path", f"Invalid path segment in {path!r}")
    return path


def _validate_manifest(db: Session, game: Game, body: VersionCreate, max_files: int) -> None:
    if len(body.files) > max_files:
        raise bad_request("too_many_files", f"Manifest has more than {max_files} files")
    aliases = {r.alias for r in game.roots}
    seen: set[str] = set()
    for f in body.files:
        validate_path(f.path, aliases)
        key = f.path.lower()  # Windows paths are case-insensitive
        if key in seen:
            raise bad_request("duplicate_path", f"Duplicate path in manifest: {f.path!r}")
        seen.add(key)

    wanted = {f.hash for f in body.files}
    known: dict[str, int] = {}
    hashes = list(wanted)
    for i in range(0, len(hashes), 500):
        for h, size in db.execute(
            select(Blob.hash, Blob.size).where(Blob.hash.in_(hashes[i : i + 500]))
        ):
            known[h] = size
    missing = sorted(wanted - known.keys())
    if missing:
        raise bad_request(
            "missing_blobs", f"{len(missing)} blob(s) have not been uploaded", missing=missing
        )
    for f in body.files:
        if known[f.hash] != f.size:
            raise bad_request(
                "size_mismatch",
                f"Size of {f.path!r} ({f.size}) does not match its blob ({known[f.hash]})",
            )


# ---------------------------------------------------------------- manifests


def manifest_rows(db: Session, version_id: str) -> list[VersionFile]:
    return list(
        db.scalars(
            select(VersionFile).where(VersionFile.version_id == version_id).order_by(VersionFile.path)
        )
    )


def manifest_key(rows) -> frozenset:
    """Identity of a manifest: paths and content. mtimes alone don't make a new version."""
    out = set()
    for r in rows:
        h = getattr(r, "blob_hash", None) or getattr(r, "hash")
        out.add((r.path, h))
    return frozenset(out)


def _stats(files) -> tuple[int, int, Optional[datetime]]:
    total = sum(f.size for f in files)
    latest = max((to_utc(f.mtime) for f in files), default=None)
    return len(files), total, latest


# ---------------------------------------------------------------- create


def create_version(
    db: Session,
    game: Game,
    body: VersionCreate,
    machine_id: Optional[str],
    max_files: int = 100_000,
) -> tuple[Version, bool]:
    """Returns (version, unchanged)."""
    with game_lock(game.id):
        db.refresh(game)
        _validate_manifest(db, game, body, max_files)

        if game.head_version_id:
            head_rows = manifest_rows(db, game.head_version_id)
            if manifest_key(head_rows) == manifest_key(body.files):
                return db.get(Version, game.head_version_id), True

        if body.make_head and body.parent_version_id != game.head_version_id:
            raise ApiError(
                409,
                "head_moved",
                "The head has moved since parent_version_id; resolve the conflict "
                "(upload with make_head=false, then promote) or pull first",
                head_version_id=game.head_version_id,
            )

        count, total, latest = _stats(body.files)
        version = Version(
            game_id=game.id,
            parent_version_id=body.parent_version_id,
            type=body.type,
            machine_id=machine_id,
            created_at=utcnow(),
            client_created_at=to_utc(body.client_created_at) if body.client_created_at else None,
            file_count=count,
            total_size=total,
            latest_mtime=latest,
            pinned=False,
        )
        db.add(version)
        db.flush()
        db.add_all(
            VersionFile(
                version_id=version.id,
                path=f.path,
                blob_hash=f.hash,
                size=f.size,
                mtime=to_utc(f.mtime),
            )
            for f in body.files
        )
        if body.make_head:
            game.head_version_id = version.id
            game.updated_at = version.created_at
        db.commit()
        return version, False


def promote(db: Session, version: Version, machine_id: Optional[str]) -> tuple[Version, bool]:
    """Copy `version`'s manifest into a new `promoted` version and make it head."""
    game = db.get(Game, version.game_id)
    if game is None or game.deleted_at is not None:
        raise not_found("Game")
    with game_lock(game.id):
        db.refresh(game)
        if game.head_version_id == version.id:
            return version, True
        rows = manifest_rows(db, version.id)
        now = utcnow()
        new = Version(
            game_id=game.id,
            parent_version_id=game.head_version_id,
            source_version_id=version.id,
            type="promoted",
            machine_id=machine_id,
            created_at=now,
            client_created_at=None,
            file_count=version.file_count,
            total_size=version.total_size,
            latest_mtime=version.latest_mtime,
            pinned=False,
        )
        db.add(new)
        db.flush()
        db.add_all(
            VersionFile(
                version_id=new.id, path=r.path, blob_hash=r.blob_hash, size=r.size, mtime=r.mtime
            )
            for r in rows
        )
        game.head_version_id = new.id
        game.updated_at = now
        db.commit()
        return new, False


# ---------------------------------------------------------------- read


def get_version(db: Session, version_id: str) -> Version:
    v = db.get(Version, version_id)
    if v is None:
        raise not_found("Version")
    return v


def previous_version(db: Session, version: Version) -> Optional[Version]:
    return db.scalar(
        select(Version)
        .where(Version.game_id == version.game_id, Version.id < version.id)
        .order_by(Version.id.desc())
        .limit(1)
    )


def list_versions(
    db: Session,
    game_id: str,
    limit: int = 50,
    before: Optional[str] = None,
    vtype: Optional[str] = None,
    tag: Optional[str] = None,
) -> list[Version]:
    q = select(Version).where(Version.game_id == game_id)
    if before:
        q = q.where(Version.id < before)
    if vtype:
        if vtype not in VERSION_TYPES:
            raise bad_request("invalid_type", f"Unknown version type {vtype!r}")
        q = q.where(Version.type == vtype)
    if tag:
        q = q.where(
            Version.id.in_(select(VersionTag.version_id).where(VersionTag.tag == tag))
        )
    # UUIDv7 ids are time-ordered, so ordering by id is ordering by creation.
    return list(db.scalars(q.order_by(Version.id.desc()).limit(limit)))


def all_tags(db: Session, game_id: str) -> list[str]:
    return sorted(
        db.scalars(
            select(VersionTag.tag)
            .join(Version, Version.id == VersionTag.version_id)
            .where(Version.game_id == game_id)
            .distinct()
        )
    )


def serialize(version: Version, head_id: Optional[str], files: Optional[list] = None) -> dict:
    out = {
        "id": version.id,
        "game_id": version.game_id,
        "parent_version_id": version.parent_version_id,
        "source_version_id": version.source_version_id,
        "type": version.type,
        "machine_id": version.machine_id,
        "machine_name": version.machine.name if version.machine else None,
        "created_at": iso(version.created_at),
        "client_created_at": iso(version.client_created_at),
        "file_count": version.file_count,
        "total_size": version.total_size,
        "latest_mtime": iso(version.latest_mtime),
        "tags": version.tag_names,
        "note": version.note,
        "pinned": version.pinned,
        "is_head": version.id == head_id,
    }
    if files is not None:
        out["files"] = [
            {"path": f.path, "hash": f.blob_hash, "size": f.size, "mtime": iso(f.mtime)}
            for f in files
        ]
    return out


def diff(db: Session, version: Version, against: Optional[Version]) -> dict:
    new = {r.path: r for r in manifest_rows(db, version.id)}
    old = {r.path: r for r in manifest_rows(db, against.id)} if against else {}

    def entry(r):
        return {"path": r.path, "hash": r.blob_hash, "size": r.size, "mtime": iso(r.mtime)}

    added = [entry(new[p]) for p in sorted(new.keys() - old.keys())]
    removed = [entry(old[p]) for p in sorted(old.keys() - new.keys())]
    changed = []
    unchanged = 0
    for p in sorted(new.keys() & old.keys()):
        if new[p].blob_hash != old[p].blob_hash:
            changed.append(
                {
                    **entry(new[p]),
                    "old_hash": old[p].blob_hash,
                    "old_size": old[p].size,
                    "old_mtime": iso(old[p].mtime),
                }
            )
        else:
            unchanged += 1
    return {
        "version_id": version.id,
        "against_version_id": against.id if against else None,
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged_count": unchanged,
    }


# ---------------------------------------------------------------- mutate


def update_version(db: Session, version: Version, body: VersionPatch) -> Version:
    fields = body.model_fields_set
    if "tags" in fields and body.tags is not None:
        version.tags = [VersionTag(version_id=version.id, tag=t) for t in body.tags]
    if "note" in fields:
        version.note = (body.note or "").strip() or None
    if "pinned" in fields and body.pinned is not None:
        version.pinned = body.pinned
    db.commit()
    db.refresh(version)
    return version


def delete_version(db: Session, version: Version) -> None:
    with game_lock(version.game_id):
        game = db.get(Game, version.game_id)
        if game is not None:
            db.refresh(game)
            if game.head_version_id == version.id:
                raise ApiError(409, "is_head", "The current head cannot be deleted")
        db.execute(delete(VersionFile).where(VersionFile.version_id == version.id))
        db.execute(delete(VersionTag).where(VersionTag.version_id == version.id))
        db.delete(version)
        db.commit()


# ---------------------------------------------------------------- zip


def build_zip(db: Session, blobs: BlobStore, version: Version, tmp_dir) -> str:
    """Write the version to a temp zip and return its path. Caller deletes it."""
    fd, path = tempfile.mkstemp(prefix="download-", suffix=".zip", dir=tmp_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for row in manifest_rows(db, version.id):
                mt = row.mtime.astimezone()
                date_time = (
                    max(mt.year, 1980), mt.month, mt.day, mt.hour, mt.minute, mt.second
                )
                info = zipfile.ZipInfo(row.path, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(info, "w", force_zip64=row.size > 2**31) as out:
                    blobs.copy_to(row.blob_hash, out)
    except Exception:
        os.unlink(path)
        raise
    return path
