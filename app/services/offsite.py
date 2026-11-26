"""Offsite backup: SQLite online backup + rclone sync of blobs and the DB copy."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from ..config import Settings


class OffsiteError(Exception):
    pass


def backup_database(db_path: Path, dest: Path) -> int:
    """Consistent copy of a live WAL database via the SQLite backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    os.replace(tmp, dest)
    return dest.stat().st_size


def _run(cmd: list[str], env: dict, log: list[str], timeout: int) -> None:
    log.append("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    output = (proc.stdout + proc.stderr).strip()
    if output:
        log.append(output[-20000:])
    if proc.returncode != 0:
        raise OffsiteError(f"{cmd[0]} {cmd[1]} exited with {proc.returncode}")


def run_offsite(settings: Settings, remote: str, log: list[str]) -> dict:
    remote = remote.strip().rstrip("/")
    if not remote or ":" not in remote:
        raise OffsiteError("Remote must look like 'name:path', e.g. gdrive:savepoint")
    binary = settings.rclone_binary
    if shutil.which(binary) is None:
        raise OffsiteError(f"rclone binary {binary!r} not found")

    db_copy = settings.backup_dir / "savepoint.db"
    size = backup_database(settings.db_path, db_copy)
    log.append(f"Database backup written to {db_copy} ({size} bytes)")

    env = dict(os.environ)
    if settings.rclone_config:
        env["RCLONE_CONFIG"] = settings.rclone_config
    common = ["--stats-one-line", "--stats", "0", "-v"]
    # Blobs are content-addressed and immutable, so size-only checks are safe
    # and avoid hashing every file on the remote.
    _run(
        [binary, "sync", str(settings.blob_dir), f"{remote}/blobs", "--size-only", *common],
        env,
        log,
        timeout=6 * 3600,
    )
    _run(
        [binary, "copyto", str(db_copy), f"{remote}/savepoint.db", *common],
        env,
        log,
        timeout=3600,
    )
    return {"remote": remote, "db_bytes": size}
