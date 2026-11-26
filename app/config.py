"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    admin_password: str | None = None
    reset_admin_password: bool = False
    secret_key: str = ""
    timezone: str = "UTC"
    # Cookie is only sent over HTTPS when true. Disable for plain-HTTP local testing.
    secure_cookies: bool = True
    session_max_age_days: int = 30
    enable_scheduler: bool = True
    # Cron-style "HH:MM" in `timezone` for the nightly jobs.
    retention_time: str = "03:30"
    offsite_time: str = "04:15"
    # Unreferenced blobs younger than this are never collected, so a client that
    # uploaded blobs but hasn't created its version yet isn't raced by GC.
    gc_grace_hours: int = 24
    rclone_binary: str = "rclone"
    rclone_config: str | None = None
    login_max_failures: int = 5
    login_window_seconds: int = 900
    max_manifest_files: int = 100_000
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "savepoint.db"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        data_dir = Path(env.get("SAVEPOINT_DATA_DIR", "/data"))
        return cls(
            data_dir=data_dir,
            admin_password=env.get("SAVEPOINT_ADMIN_PASSWORD") or None,
            reset_admin_password=_bool(env.get("SAVEPOINT_RESET_ADMIN_PASSWORD")),
            secret_key=env.get("SAVEPOINT_SECRET_KEY", ""),
            timezone=env.get("TZ", "UTC") or "UTC",
            secure_cookies=_bool(env.get("SAVEPOINT_SECURE_COOKIES"), True),
            session_max_age_days=_int(env.get("SAVEPOINT_SESSION_DAYS"), 30),
            enable_scheduler=_bool(env.get("SAVEPOINT_ENABLE_SCHEDULER"), True),
            retention_time=env.get("SAVEPOINT_RETENTION_TIME", "03:30"),
            offsite_time=env.get("SAVEPOINT_OFFSITE_TIME", "04:15"),
            gc_grace_hours=_int(env.get("SAVEPOINT_GC_GRACE_HOURS"), 24),
            rclone_binary=env.get("SAVEPOINT_RCLONE_BINARY", "rclone"),
            rclone_config=env.get("RCLONE_CONFIG") or None,
        )

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.blob_dir, self.tmp_dir, self.backup_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolve_secret_key(self) -> str:
        """Use the configured key, or a random one persisted in the data dir."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret_key"
        if path.exists():
            self.secret_key = path.read_text().strip()
        else:
            self.secret_key = secrets.token_urlsafe(48)
            path.write_text(self.secret_key)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return self.secret_key
