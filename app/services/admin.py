from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Admin
from ..security import hash_password, verify_password
from ..timeutil import utcnow

log = logging.getLogger("savepoint.admin")

MIN_PASSWORD_LENGTH = 8


def bootstrap(db: Session, settings: Settings) -> None:
    """Seed the admin password from SAVEPOINT_ADMIN_PASSWORD on first start.

    With SAVEPOINT_RESET_ADMIN_PASSWORD=true the env password overwrites the
    stored one (recovery path), and existing sessions are signed out.
    """
    admin = db.get(Admin, 1)
    if admin is None:
        if not settings.admin_password:
            log.warning(
                "No admin password set. Set SAVEPOINT_ADMIN_PASSWORD and restart to enable login."
            )
            return
        db.add(Admin(id=1, password_hash=hash_password(settings.admin_password), session_epoch=1))
        db.commit()
        log.info("Admin password initialised from SAVEPOINT_ADMIN_PASSWORD")
    elif settings.reset_admin_password and settings.admin_password:
        admin.password_hash = hash_password(settings.admin_password)
        admin.session_epoch += 1
        admin.updated_at = utcnow()
        db.commit()
        log.warning("Admin password reset from SAVEPOINT_ADMIN_PASSWORD")


def check_password(db: Session, password: str) -> Admin | None:
    admin = db.get(Admin, 1)
    if admin is None or not verify_password(admin.password_hash, password):
        return None
    return admin


def change_password(db: Session, admin: Admin, new_password: str) -> int:
    admin.password_hash = hash_password(new_password)
    admin.session_epoch += 1
    admin.updated_at = utcnow()
    db.commit()
    return admin.session_epoch
