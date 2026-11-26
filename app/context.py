"""Process-wide objects, attached to app.state.ctx."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .blobstore import BlobStore
from .config import Settings
from .security import LoginRateLimiter

if TYPE_CHECKING:
    from .jobs import JobRunner


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    Session: sessionmaker
    blobs: BlobStore
    login_limiter: LoginRateLimiter
    jobs: "JobRunner" = field(default=None)  # type: ignore[assignment]
