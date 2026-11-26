"""Nightly jobs (retention + GC, offsite backup) and their run history."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy import select

from .context import AppContext
from .models import JobRun
from .services import offsite, settings_store
from .services.retention import apply_retention, collect_garbage, games_by_id
from .timeutil import utcnow

log = logging.getLogger("savepoint.jobs")

RETENTION = "retention"
OFFSITE = "offsite"
JOB_NAMES = {RETENTION: "Retention + GC", OFFSITE: "Offsite backup"}


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


class JobRunner:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._locks = {RETENTION: threading.Lock(), OFFSITE: threading.Lock()}
        self._scheduler = None

    # ------------------------------------------------------------ running

    def is_running(self, job: str) -> bool:
        return self._locks[job].locked()

    def _record(self, job: str, body: Callable[[list[str]], str]) -> Optional[JobRun]:
        lock = self._locks[job]
        if not lock.acquire(blocking=False):
            log.info("%s already running; skipped", job)
            return None
        try:
            with self.ctx.Session() as db:
                run = JobRun(job=job, started_at=utcnow(), status="running")
                db.add(run)
                db.commit()
                run_id = run.id
            lines: list[str] = []
            status, summary = "ok", ""
            try:
                summary = body(lines)
            except Exception as exc:  # noqa: BLE001 - recorded for the UI
                status, summary = "error", str(exc) or exc.__class__.__name__
                lines.append(traceback.format_exc())
                log.exception("%s failed", job)
            with self.ctx.Session() as db:
                run = db.get(JobRun, run_id)
                run.finished_at = utcnow()
                run.status = status
                run.summary = summary
                run.log = "\n".join(lines)[-100_000:]
                db.commit()
                log.info("%s finished: %s %s", job, status, summary)
                return run
        finally:
            lock.release()

    def run_retention(self, now=None) -> Optional[JobRun]:
        settings = self.ctx.settings

        def body(lines: list[str]) -> str:
            when = now or utcnow()
            with self.ctx.Session() as db:
                names = games_by_id(db)
                removed = apply_retention(db, when, settings.tz)
                total = 0
                for game_id, ids in removed.items():
                    total += len(ids)
                    lines.append(f"{names.get(game_id, game_id)}: deleted {len(ids)} version(s)")
                    lines.extend(f"  - {i}" for i in ids)
                gc = collect_garbage(
                    db, self.ctx.blobs, when, timedelta(hours=settings.gc_grace_hours)
                )
            lines.append(
                f"GC: {gc['blobs_deleted']} blob(s), {_fmt_bytes(gc['bytes_freed'])} freed; "
                f"{gc['stray_files_deleted']} stray file(s); {gc['tmp_files_deleted']} temp file(s)"
            )
            lines.extend(f"  - blob {h}" for h in gc["deleted_hashes"])
            return (
                f"{total} version(s) deleted, {gc['blobs_deleted']} blob(s) removed, "
                f"{_fmt_bytes(gc['bytes_freed'])} freed"
            )

        return self._record(RETENTION, body)

    def run_offsite(self, force: bool = False) -> Optional[JobRun]:
        with self.ctx.Session() as db:
            cfg = settings_store.offsite(db)
        if not cfg["enabled"] and not force:
            log.info("offsite backup disabled; skipped")
            return None

        def body(lines: list[str]) -> str:
            result = offsite.run_offsite(self.ctx.settings, cfg["remote"], lines)
            return f"Synced to {result['remote']}"

        return self._record(OFFSITE, body)

    def run_in_background(self, job: str, **kwargs) -> bool:
        if self.is_running(job):
            return False
        target = self.run_retention if job == RETENTION else self.run_offsite
        threading.Thread(target=target, kwargs=kwargs, daemon=True, name=f"job-{job}").start()
        return True

    # ------------------------------------------------------------ history

    def last_run(self, job: str) -> Optional[JobRun]:
        with self.ctx.Session() as db:
            return db.scalar(
                select(JobRun).where(JobRun.job == job).order_by(JobRun.id.desc()).limit(1)
            )

    def recent_runs(self, limit: int = 20) -> list[JobRun]:
        with self.ctx.Session() as db:
            return list(db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(limit)))

    # ------------------------------------------------------------ scheduling

    def start_scheduler(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        settings = self.ctx.settings
        sched = BackgroundScheduler(timezone=settings.tz)
        for job, at, func in (
            (RETENTION, settings.retention_time, self.run_retention),
            (OFFSITE, settings.offsite_time, self.run_offsite),
        ):
            hour, _, minute = at.partition(":")
            sched.add_job(
                func,
                CronTrigger(hour=int(hour), minute=int(minute or 0), timezone=settings.tz),
                id=job,
                coalesce=True,
                misfire_grace_time=3600,
                max_instances=1,
            )
        sched.start()
        self._scheduler = sched
        log.info(
            "Scheduler started (retention %s, offsite %s, %s)",
            settings.retention_time,
            settings.offsite_time,
            settings.timezone,
        )

    def next_run_times(self) -> dict:
        if not self._scheduler:
            return {}
        return {j.id: j.next_run_time for j in self._scheduler.get_jobs()}

    def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None


def summarize(run: Optional[JobRun]) -> Optional[dict]:
    if run is None:
        return None
    return json.loads(
        json.dumps(
            {
                "job": run.job,
                "status": run.status,
                "summary": run.summary,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        )
    )
