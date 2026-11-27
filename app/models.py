from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .ids import uuid7
from .timeutil import UTCDateTime, utcnow


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200))
    exe_names_json: Mapped[str] = mapped_column(Text, default="[]")
    excludes_json: Mapped[str] = mapped_column(Text, default="[]")
    sync_mode: Mapped[str] = mapped_column(String(16), default="auto")
    retention_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Machines this game syncs on: JSON list of machine ids, or NULL for all machines
    # (including ones added later).
    sync_machines_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    head_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("versions.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    roots: Mapped[list["GameRoot"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GameRoot.position",
        lazy="selectin",
    )


class GameRoot(Base):
    __tablename__ = "game_roots"

    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(String(64), primary_key=True)
    path_template: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)

    game: Mapped[Game] = relationship(back_populates="roots")


class RootOverride(Base):
    __tablename__ = "root_overrides"

    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(String(64), primary_key=True)
    machine_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("machines.id", ondelete="CASCADE"), primary_key=True
    )
    path_template: Mapped[str] = mapped_column(Text)


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(100))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Client clock minus server clock, from the X-Savepoint-Client-Time header.
    clock_skew_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    clock_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (Index("ix_versions_game_created", "game_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), index=True
    )
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # For `promoted` versions: the version whose manifest was copied.
    source_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    type: Mapped[str] = mapped_column(String(16))
    machine_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("machines.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    client_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(BigInteger, default=0)
    # Latest file mtime in the manifest (UTC). Used to judge "newer" in conflicts.
    latest_mtime: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    files: Mapped[list["VersionFile"]] = relationship(
        cascade="all, delete-orphan", order_by="VersionFile.path", lazy="select"
    )
    tags: Mapped[list["VersionTag"]] = relationship(
        cascade="all, delete-orphan", order_by="VersionTag.tag", lazy="selectin"
    )
    machine: Mapped[Machine | None] = relationship(lazy="selectin")

    @property
    def tag_names(self) -> list[str]:
        return [t.tag for t in self.tags]


class VersionFile(Base):
    __tablename__ = "version_files"

    version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("versions.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    blob_hash: Mapped[str] = mapped_column(String(64), ForeignKey("blobs.hash"), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[datetime] = mapped_column(UTCDateTime)


class VersionTag(Base):
    __tablename__ = "version_tags"

    version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("versions.id", ondelete="CASCADE"), primary_key=True
    )
    tag: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class Blob(Base):
    __tablename__ = "blobs"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    stored_size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)


class Admin(Base):
    __tablename__ = "admin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text)
    # Bumped on password change so existing sessions are invalidated.
    session_epoch: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    job: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)
