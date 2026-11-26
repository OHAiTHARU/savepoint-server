from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import migrate
from .api import router as api_router
from .blobstore import BlobStore
from .config import Settings
from .context import AppContext
from .db import make_engine, make_sessionmaker
from .errors import ApiError
from .jobs import JobRunner
from .security import LoginRateLimiter
from .services import admin as admin_svc
from .web import router as web_router
from .web.views import LoginRequired, login_redirect, render_error

log = logging.getLogger("savepoint")

STATIC_DIR = Path(__file__).parent / "web" / "static"
SESSION_COOKIE = "savepoint_session"


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    migrate.upgrade(settings.db_path)

    engine = make_engine(settings.db_path)
    ctx = AppContext(
        settings=settings,
        engine=engine,
        Session=make_sessionmaker(engine),
        blobs=BlobStore(settings.blob_dir, settings.tmp_dir),
        login_limiter=LoginRateLimiter(
            settings.login_max_failures, settings.login_window_seconds
        ),
    )
    ctx.jobs = JobRunner(ctx)
    with ctx.Session() as db:
        admin_svc.bootstrap(db, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_scheduler:
            ctx.jobs.start_scheduler()
        try:
            yield
        finally:
            ctx.jobs.shutdown()
            engine.dispose()

    app = FastAPI(
        title="Savepoint",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.ctx = ctx

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.resolve_secret_key(),
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_max_age_days * 86400,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        headers = {}
        if exc.status == 401 and _is_api(request):
            headers["WWW-Authenticate"] = "Bearer"
        if _is_api(request):
            return JSONResponse(exc.body(), status_code=exc.status, headers=headers)
        return render_error(request, exc.status, exc.message)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return login_redirect(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        if _is_api(request):
            first = details[0] if details else {"loc": [], "msg": "Invalid request"}
            where = ".".join(str(p) for p in first["loc"][1:]) or "body"
            return JSONResponse(
                {
                    "error": {
                        "code": "validation_error",
                        "message": f"{where}: {first['msg']}",
                        "details": details,
                    }
                },
                status_code=422,
            )
        return render_error(request, 400, "Invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if _is_api(request):
            code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
            return JSONResponse(
                {"error": {"code": code, "message": str(exc.detail)}},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
        return render_error(request, exc.status_code, str(exc.detail))

    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
