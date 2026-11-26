"""FastAPI dependencies: DB session and authentication."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Optional

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .context import AppContext
from .errors import ApiError
from .models import Admin, Machine
from .security import hash_api_key
from .timeutil import to_utc, utcnow

LAST_SEEN_THROTTLE = timedelta(seconds=60)
CLIENT_TIME_HEADER = "x-savepoint-client-time"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_db(request: Request) -> Iterator[Session]:
    ctx: AppContext = request.app.state.ctx
    db = ctx.Session()
    try:
        yield db
    finally:
        db.close()


@dataclass
class Principal:
    kind: str  # "machine" | "admin"
    machine: Optional[Machine] = None

    @property
    def machine_id(self) -> Optional[str]:
        return self.machine.id if self.machine else None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"


def _parse_client_time(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return to_utc(dt)


def _machine_from_bearer(request: Request, db: Session) -> Optional[Machine]:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    machine = db.scalar(
        select(Machine).where(Machine.api_key_hash == hash_api_key(token.strip()))
    )
    if machine is None or machine.revoked_at is not None:
        raise ApiError(401, "invalid_api_key", "API key is invalid or revoked")

    now = utcnow()
    dirty = False
    if machine.last_seen_at is None or now - machine.last_seen_at > LAST_SEEN_THROTTLE:
        machine.last_seen_at = now
        dirty = True
    client_time = request.headers.get(CLIENT_TIME_HEADER)
    if client_time:
        parsed = _parse_client_time(client_time)
        if parsed is not None:
            machine.clock_skew_ms = int((parsed - now).total_seconds() * 1000)
            machine.clock_checked_at = now
            dirty = True
    if dirty:
        db.commit()
    return machine


def admin_session_valid(request: Request, db: Session) -> bool:
    session = request.scope.get("session")
    if not session or not session.get("admin"):
        return False
    admin = db.get(Admin, 1)
    return admin is not None and session.get("epoch") == admin.session_epoch


def csrf_token(request: Request) -> str:
    session = request.session
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf(request: Request, submitted: Optional[str]) -> None:
    expected = request.session.get("csrf")
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token")


def authenticate(request: Request, db: Session) -> Optional[Principal]:
    machine = _machine_from_bearer(request, db)
    if machine is not None:
        return Principal("machine", machine)
    if admin_session_valid(request, db):
        if request.method in UNSAFE_METHODS:
            check_csrf(request, request.headers.get(CSRF_HEADER))
        return Principal("admin")
    return None


def require_auth(request: Request, db: Session = Depends(get_db)) -> Principal:
    principal = authenticate(request, db)
    if principal is None:
        raise ApiError(401, "unauthorized", "Authentication required")
    return principal


def require_machine(principal: Principal = Depends(require_auth)) -> Principal:
    if principal.machine is None:
        raise ApiError(403, "machine_only", "This endpoint needs a machine API key")
    return principal


def require_ui(principal: Principal = Depends(require_auth)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "ui_only", "This endpoint needs a web UI session")
    return principal
