"""Template setup, filters and small web helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..timeutil import utcnow

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

TYPE_LABELS = {
    "session": "Session",
    "checkpoint": "Checkpoint",
    "manual": "Manual",
    "offline": "Offline",
    "pre_restore": "Pre-restore",
    "promoted": "Promoted",
}


def filesize(n: Optional[int]) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}" if size < 100 else f"{size:.0f} {unit}"
        size /= 1024
    return f"{n} B"


def reltime(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    if dt is None:
        return "never"
    now = now or utcnow()
    secs = int((now - dt).total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        text = "just now" if not future else "in a moment"
        return text
    for limit, div, unit in (
        (3600, 60, "min"),
        (86400, 3600, "h"),
        (86400 * 30, 86400, "d"),
        (86400 * 365, 86400 * 30, "mo"),
    ):
        if secs < limit:
            value = max(1, round(secs / div))
            break
    else:
        value, unit = max(1, round(secs / (86400 * 365))), "y"
    return f"in {value} {unit}" if future else f"{value} {unit} ago"


def skew(ms: Optional[int]) -> str:
    if ms is None:
        return "unknown"
    secs = abs(ms) / 1000
    if secs < 2:
        return "in sync"
    if secs < 120:
        amount = f"{secs:.0f} s"
    elif secs < 7200:
        amount = f"{secs / 60:.0f} min"
    else:
        amount = f"{secs / 3600:.1f} h"
    return f"{amount} {'ahead' if ms > 0 else 'behind'}"


def setup_filters(tz) -> None:
    env = templates.env

    def localtime(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
        if dt is None:
            return "—"
        return dt.astimezone(tz).strftime(fmt)

    env.filters["localtime"] = localtime
    env.filters["filesize"] = filesize
    env.filters["reltime"] = reltime
    env.filters["skew"] = skew
    env.globals["TYPE_LABELS"] = TYPE_LABELS


def flash(request: Request, message: str, kind: str = "ok") -> None:
    msgs = request.session.get("flash", [])
    msgs.append({"kind": kind, "message": message})
    request.session["flash"] = msgs[-5:]


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flash", []) if "session" in request.scope else []


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
