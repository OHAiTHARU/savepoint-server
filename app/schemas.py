"""Request bodies and shared value objects for /api/v1."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

SyncMode = Literal["auto", "manual"]

# Types a client may create directly. `promoted` is only created by /promote.
ClientVersionType = Literal["session", "checkpoint", "manual", "offline", "pre_restore"]
VERSION_TYPES = ("session", "checkpoint", "manual", "offline", "pre_restore", "promoted")
SHORT_LIVED_TYPES = ("checkpoint", "offline", "pre_restore")
LONG_LIVED_TYPES = ("session", "manual", "promoted")

ALIAS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
TAG_PATTERN = r"^[^\s,][^,]{0,63}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetentionPolicy(Strict):
    """Every number is configurable; null means "no limit" for that rule."""

    keep_newest: int = Field(5, ge=0, description="Newest N versions are never deleted")
    checkpoint_keep_hours: Optional[int] = Field(48, ge=0)
    session_keep_all_days: Optional[int] = Field(14, ge=0)
    session_daily_days: Optional[int] = Field(90, ge=0)
    session_weekly_days: Optional[int] = Field(365, ge=0)
    max_versions: Optional[int] = Field(None, ge=1)


class RootIn(Strict):
    alias: str = Field(pattern=ALIAS_PATTERN)
    path_template: str = Field(min_length=1, max_length=1000)


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out


class GameCreate(Strict):
    name: str = Field(min_length=1, max_length=200)
    roots: list[RootIn] = Field(min_length=1)
    exe_names: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    sync_mode: SyncMode = "auto"
    retention: Optional[RetentionPolicy] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("exe_names", "excludes")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_list(v)

    @field_validator("roots")
    @classmethod
    def _unique_aliases(cls, v: list[RootIn]) -> list[RootIn]:
        seen = set()
        for r in v:
            key = r.alias.lower()
            if key in seen:
                raise ValueError(f"duplicate root alias {r.alias!r}")
            seen.add(key)
        return v


class GamePatch(Strict):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    roots: Optional[list[RootIn]] = Field(None, min_length=1)
    exe_names: Optional[list[str]] = None
    excludes: Optional[list[str]] = None
    sync_mode: Optional[SyncMode] = None
    retention: Optional[RetentionPolicy] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return GameCreate._name(v) if v is not None else v

    @field_validator("exe_names", "excludes")
    @classmethod
    def _lists(cls, v):
        return _clean_list(v) if v is not None else v

    @field_validator("roots")
    @classmethod
    def _unique(cls, v):
        return GameCreate._unique_aliases(v) if v is not None else v


class OverrideIn(Strict):
    path_template: Optional[str] = Field(None, max_length=1000)


class MachineCreate(Strict):
    name: str = Field(min_length=1, max_length=100)


class BlobCheck(Strict):
    hashes: list[str] = Field(max_length=100_000)


class FileEntry(Strict):
    path: str = Field(min_length=1, max_length=4096)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    # Must carry a UTC offset. The server normalizes to UTC; naive times are
    # rejected so a machine's local timezone can never be mistaken for UTC.
    mtime: AwareDatetime


class VersionCreate(Strict):
    parent_version_id: Optional[str] = None
    type: ClientVersionType
    make_head: bool = True
    client_created_at: Optional[AwareDatetime] = None
    files: list[FileEntry]


class VersionPatch(Strict):
    tags: Optional[list[str]] = None
    note: Optional[str] = Field(None, max_length=5000)
    pinned: Optional[bool] = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v):
        if v is None:
            return v
        import re

        out = []
        for t in v:
            t = t.strip()
            if not t:
                continue
            if not re.match(TAG_PATTERN, t):
                raise ValueError(f"invalid tag {t!r}")
            if t not in out:
                out.append(t)
        return out
