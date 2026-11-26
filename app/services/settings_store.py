"""Key/value settings stored in the `settings` table."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import Setting
from ..schemas import RetentionPolicy

DEFAULT_RETENTION_KEY = "default_retention"
OFFSITE_KEY = "offsite"

OFFSITE_DEFAULTS = {"enabled": False, "remote": "gdrive:savepoint"}


def get(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return default
    return json.loads(row.value_json)


def put(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    data = json.dumps(value)
    if row is None:
        db.add(Setting(key=key, value_json=data))
    else:
        row.value_json = data


def default_retention(db: Session) -> RetentionPolicy:
    raw = get(db, DEFAULT_RETENTION_KEY)
    if raw is None:
        return RetentionPolicy()
    return RetentionPolicy.model_validate(raw)


def set_default_retention(db: Session, policy: RetentionPolicy) -> None:
    put(db, DEFAULT_RETENTION_KEY, policy.model_dump())


def offsite(db: Session) -> dict:
    return {**OFFSITE_DEFAULTS, **(get(db, OFFSITE_KEY) or {})}


def set_offsite(db: Session, enabled: bool, remote: str) -> None:
    put(db, OFFSITE_KEY, {"enabled": bool(enabled), "remote": remote.strip()})
