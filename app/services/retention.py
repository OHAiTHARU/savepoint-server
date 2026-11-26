"""Retention rules (DESIGN §7) and blob garbage collection.

`plan_retention` is pure: it takes versions and a policy and returns the ids to
delete, which keeps the rules easy to test with fixed clocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..blobstore import BlobStore
from ..models import Blob, Game, Version, VersionFile, VersionTag
from ..schemas import LONG_LIVED_TYPES, SHORT_LIVED_TYPES, RetentionPolicy
from ..timeutil import UTC
from . import games as games_svc
from . import settings_store


@dataclass(frozen=True)
class VersionInfo:
    id: str
    type: str
    created_at: datetime
    pinned: bool
    tagged: bool


def plan_retention(
    versions: Iterable[VersionInfo],
    policy: RetentionPolicy,
    head_id: Optional[str],
    now: datetime,
    tz: tzinfo = UTC,
) -> set[str]:
    """Return the ids of versions to delete."""
    ordered = sorted(versions, key=lambda v: (v.created_at, v.id), reverse=True)
    newest = {v.id for v in ordered[: policy.keep_newest]}

    def protected(v: VersionInfo) -> bool:
        return v.id == head_id or v.pinned or v.tagged or v.id in newest

    # The newest long-lived version per local day / ISO week. Protected versions
    # count too: if the newest of a day is pinned, the rest of that day can go.
    day_winner: dict = {}
    week_winner: dict = {}
    for v in ordered:
        if v.type not in LONG_LIVED_TYPES:
            continue
        local = v.created_at.astimezone(tz)
        day_winner.setdefault(local.date(), v.id)
        iso_year, iso_week, _ = local.isocalendar()
        week_winner.setdefault((iso_year, iso_week), v.id)

    def days(n: Optional[int]) -> Optional[timedelta]:
        return None if n is None else timedelta(days=n)

    keep_all = days(policy.session_keep_all_days)
    daily = days(policy.session_daily_days)
    weekly = days(policy.session_weekly_days)

    doomed: set[str] = set()
    for v in ordered:
        if protected(v):
            continue
        age = now - v.created_at
        if v.type in SHORT_LIVED_TYPES:
            hours = policy.checkpoint_keep_hours
            if hours is not None and age > timedelta(hours=hours):
                doomed.add(v.id)
            continue
        # Long-lived: session, manual, promoted.
        if keep_all is None or age <= keep_all:
            continue
        local = v.created_at.astimezone(tz)
        if daily is None or age <= daily:
            if day_winner.get(local.date()) != v.id:
                doomed.add(v.id)
            continue
        if weekly is None or age <= weekly:
            iso_year, iso_week, _ = local.isocalendar()
            if week_winner.get((iso_year, iso_week)) != v.id:
                doomed.add(v.id)
            continue
        doomed.add(v.id)

    if policy.max_versions is not None:
        remaining = [v for v in ordered if v.id not in doomed]
        excess = len(remaining) - policy.max_versions
        for v in reversed(remaining):  # oldest first
            if excess <= 0:
                break
            if not protected(v):
                doomed.add(v.id)
                excess -= 1
    return doomed


def _version_infos(db: Session, game_id: str) -> list[VersionInfo]:
    tagged = set(
        db.scalars(
            select(VersionTag.version_id)
            .join(Version, Version.id == VersionTag.version_id)
            .where(Version.game_id == game_id)
        )
    )
    rows = db.execute(
        select(Version.id, Version.type, Version.created_at, Version.pinned).where(
            Version.game_id == game_id
        )
    )
    return [
        VersionInfo(id=i, type=t, created_at=c, pinned=bool(p), tagged=i in tagged)
        for i, t, c, p in rows
    ]


def apply_retention(
    db: Session, now: datetime, tz: tzinfo = UTC, dry_run: bool = False
) -> dict[str, list[str]]:
    """Apply retention to every live game. Returns {game_id: [deleted version ids]}."""
    default = settings_store.default_retention(db)
    removed: dict[str, list[str]] = {}
    for game in games_svc.list_games(db):
        policy = games_svc.retention(game) or default
        doomed = plan_retention(_version_infos(db, game.id), policy, game.head_version_id, now, tz)
        if not doomed:
            continue
        removed[game.id] = sorted(doomed)
        if dry_run:
            continue
        ids = list(doomed)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            db.execute(delete(VersionFile).where(VersionFile.version_id.in_(chunk)))
            db.execute(delete(VersionTag).where(VersionTag.version_id.in_(chunk)))
            db.execute(delete(Version).where(Version.id.in_(chunk)))
        db.commit()
    return removed


def collect_garbage(
    db: Session, blobs: BlobStore, now: datetime, grace: timedelta
) -> dict:
    """Delete blobs no version references (older than `grace`), and stray files."""
    referenced = select(VersionFile.blob_hash).distinct()
    candidates = list(
        db.execute(
            select(Blob.hash, Blob.stored_size).where(
                Blob.hash.not_in(referenced), Blob.created_at < now - grace
            )
        )
    )
    freed = 0
    hashes = []
    for h, stored in candidates:
        hashes.append(h)
        freed += stored
    for i in range(0, len(hashes), 500):
        db.execute(delete(Blob).where(Blob.hash.in_(hashes[i : i + 500])))
    db.commit()
    for h in hashes:
        blobs.delete(h)

    # Files on disk with no DB row (e.g. a crash between rename and commit).
    known = set(db.scalars(select(Blob.hash)))
    strays = 0
    cutoff = (now - grace).timestamp()
    for h in blobs.iter_hashes():
        if h not in known:
            try:
                if blobs.path_for(h).stat().st_mtime < cutoff:
                    blobs.delete(h)
                    strays += 1
            except OSError:
                pass
    tmp_removed = blobs.clean_tmp(grace.total_seconds())
    return {
        "blobs_deleted": len(hashes),
        "bytes_freed": freed,
        "stray_files_deleted": strays,
        "tmp_files_deleted": tmp_removed,
        "deleted_hashes": hashes,
    }


def games_by_id(db: Session) -> dict[str, str]:
    return {g.id: g.name for g in db.scalars(select(Game))}
