"""Server-rendered web UI (Jinja2 + htmx)."""

from __future__ import annotations

import shutil
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import admin_session_valid, check_csrf, csrf_token, get_ctx, get_db
from ..errors import ApiError
from ..jobs import JOB_NAMES, OFFSITE, RETENTION
from ..models import Admin, Blob, Game, JobRun, Machine, Version, VersionFile
from ..schemas import VERSION_TYPES, GameCreate, GamePatch, RetentionPolicy, VersionPatch
from ..services import admin as admin_svc
from ..services import games as games_svc
from ..services import settings_store
from ..services import versions as versions_svc
from ..timeutil import utcnow
from ..api.machines import create_machine, revoke_machine
from .helpers import flash, is_htmx, pop_flashes, setup_filters, templates

router = APIRouter(include_in_schema=False)
PAGE_SIZE = 30


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def login_redirect(request: Request, exc: LoginRequired) -> Response:
    url = "/login"
    if exc.next_url and exc.next_url != "/":
        url += "?next=" + quote(exc.next_url, safe="")
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def admin_required(request: Request, db: Session = Depends(get_db)) -> None:
    if not admin_session_valid(request, db):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise LoginRequired(target if request.method == "GET" else "/")


async def csrf_required(request: Request) -> None:
    token = request.headers.get("x-csrf-token")
    if not token:
        form = await request.form()
        token = form.get("csrf")
    check_csrf(request, token)


def render(request: Request, name: str, context: Optional[dict] = None, status: int = 200):
    ctx = get_ctx(request)
    setup_filters(ctx.settings.tz)
    data = {
        "csrf": csrf_token(request) if "session" in request.scope else "",
        "flashes": pop_flashes(request),
        "tz_name": ctx.settings.timezone,
        "now": utcnow(),
        "path": request.url.path,
    }
    data.update(context or {})
    return templates.TemplateResponse(request, name, data, status_code=status)


def render_error(request: Request, status: int, message: str):
    if is_htmx(request):
        return HTMLResponse(
            f'<div class="flash flash-error">{_escape(message)}</div>',
            status_code=status,
            headers={"HX-Retarget": "#flash-area", "HX-Reswap": "beforeend"},
        )
    return render(request, "error.html", {"status": status, "message": message}, status=status)


def _escape(text: str) -> str:
    from markupsafe import escape

    return str(escape(text))


def _safe_next(url: Optional[str]) -> str:
    if not url:
        return "/"
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return "/"
    return url


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


# ===================================================================== auth


@router.get("/login")
def login_page(request: Request, next: Optional[str] = None, db: Session = Depends(get_db)):
    if admin_session_valid(request, db):
        return RedirectResponse(_safe_next(next), status_code=303)
    configured = db.get(Admin, 1) is not None
    return render(request, "login.html", {"next": _safe_next(next), "configured": configured})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    ctx = get_ctx(request)
    next_url = _safe_next(form.get("next"))
    ip = _client_ip(request)
    wait = ctx.login_limiter.retry_after(ip)
    if wait:
        return render(
            request,
            "login.html",
            {"next": next_url, "configured": True, "error": f"Too many attempts. Try again in {wait // 60 + 1} min."},
            status=429,
        )
    admin = admin_svc.check_password(db, str(form.get("password", "")))
    if admin is None:
        ctx.login_limiter.record_failure(ip)
        return render(
            request,
            "login.html",
            {"next": next_url, "configured": True, "error": "Wrong password."},
            status=401,
        )
    ctx.login_limiter.reset(ip)
    request.session.clear()
    request.session.update({"admin": True, "epoch": admin.session_epoch})
    csrf_token(request)
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_required)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ===================================================================== games


def _machines(db: Session, include_revoked: bool = False) -> list[Machine]:
    q = select(Machine).order_by(Machine.created_at)
    if not include_revoked:
        q = q.where(Machine.revoked_at.is_(None))
    return list(db.scalars(q))


@router.get("/", dependencies=[Depends(admin_required)])
def games_page(request: Request, db: Session = Depends(get_db)):
    games = games_svc.list_games(db, include_deleted=True)
    stats = games_svc.game_stats(db)
    head_ids = [g.head_version_id for g in games if g.head_version_id]
    heads = {
        v.id: v for v in db.scalars(select(Version).where(Version.id.in_(head_ids)))
    } if head_ids else {}
    live = [g for g in games if g.deleted_at is None]
    deleted = [g for g in games if g.deleted_at is not None]
    machines = _machines(db)
    sync_labels = {g.id: _sync_label(games_svc.sync_machines(g), machines) for g in games}
    return render(
        request,
        "games.html",
        {"games": live, "deleted": deleted, "stats": stats, "heads": heads, "sync_labels": sync_labels},
    )


def _game_form_data(form) -> dict:
    aliases = form.getlist("root_alias")
    paths = form.getlist("root_path")
    roots = [
        {"alias": a.strip(), "path_template": p.strip()}
        for a, p in zip(aliases, paths)
        if a.strip() or p.strip()
    ]
    lines = lambda key: [x.strip() for x in str(form.get(key, "")).splitlines() if x.strip()]  # noqa: E731
    data = {
        "name": str(form.get("name", "")).strip(),
        "roots": roots,
        "exe_names": lines("exe_names"),
        "excludes": lines("excludes"),
        "sync_mode": form.get("sync_mode", "auto"),
        "sync_machines": (
            [str(m) for m in form.getlist("sync_machine")] if form.get("sync_scope") == "only" else None
        ),
    }
    if form.get("retention_mode") == "custom":
        data["retention"] = _retention_from_form(form)
    else:
        data["retention"] = None
    return data


def _retention_from_form(form, prefix: str = "ret_") -> dict:
    out = {}
    for key in RetentionPolicy.model_fields:
        raw = str(form.get(prefix + key, "")).strip()
        out[key] = int(raw) if raw.lstrip("-").isdigit() else None
    if out.get("keep_newest") is None:
        out["keep_newest"] = 0
    return out


def _game_form_context(db: Session, game: Optional[Game], data: Optional[dict] = None, error=None):
    default = settings_store.default_retention(db)
    if data is None and game is not None:
        pol = games_svc.retention(game)
        data = {
            "name": game.name,
            "roots": [{"alias": r.alias, "path_template": r.path_template} for r in game.roots],
            "exe_names": games_svc.exe_names(game),
            "excludes": games_svc.excludes(game),
            "sync_mode": game.sync_mode,
            "retention": pol.model_dump() if pol else None,
            "sync_machines": games_svc.sync_machines(game),
        }
    data = data or {
        "name": "",
        "roots": [{"alias": "saves", "path_template": ""}],
        "exe_names": [],
        "excludes": [],
        "sync_mode": "auto",
        "retention": None,
        "sync_machines": None,
    }
    return {
        "game": game,
        "form": data,
        "error": error,
        "machines": _machines(db),
        "default_retention": default.model_dump(),
        "retention_fields": RETENTION_FIELDS,
    }


RETENTION_FIELDS = [
    ("keep_newest", "Always keep newest", "versions", "Never deleted, whatever their age."),
    ("checkpoint_keep_hours", "Checkpoints / offline / pre-restore", "hours", "Deleted after this long."),
    ("session_keep_all_days", "Keep every session / manual / promoted", "days", "All kept for this long."),
    ("session_daily_days", "Then newest per day until", "days", ""),
    ("session_weekly_days", "Then newest per week until", "days", "Older ones are deleted."),
    ("max_versions", "Hard cap", "versions", "Applied last; blank for no cap."),
]


@router.get("/games/new", dependencies=[Depends(admin_required)])
def new_game_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "game_form.html", _game_form_context(db, None))


@router.post("/games/new", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def new_game_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    data = _game_form_data(form)
    try:
        body = GameCreate.model_validate(data)
    except ValidationError as exc:
        return render(
            request,
            "game_form.html",
            _game_form_context(db, None, data, _validation_message(exc)),
            status=400,
        )
    game = games_svc.create_game(db, body)
    flash(request, f"Added {game.name}.")
    return RedirectResponse(f"/games/{game.id}", status_code=303)


@router.get("/games/{game_id}", dependencies=[Depends(admin_required)])
def game_page(
    request: Request,
    game_id: str,
    type: Optional[str] = None,
    tag: Optional[str] = None,
    db: Session = Depends(get_db),
):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    vtype = type if type in VERSION_TYPES else None
    versions = versions_svc.list_versions(db, game.id, limit=PAGE_SIZE, vtype=vtype, tag=tag or None)
    stats = games_svc.game_stats(db).get(game.id, {})
    machines = _machines(db)
    allowed = games_svc.sync_machines(game)
    return render(
        request,
        "game.html",
        {
            "game": game,
            "versions": versions,
            "head_id": game.head_version_id,
            "filter_type": vtype,
            "filter_tag": tag or "",
            "tags": versions_svc.all_tags(db, game.id),
            "types": VERSION_TYPES,
            "stats": stats,
            "next_before": versions[-1].id if len(versions) == PAGE_SIZE else None,
            "exe_names": games_svc.exe_names(game),
            "sync_label": _sync_label(allowed, machines),
        },
    )


def _sync_label(allowed: Optional[list[str]], machines: list[Machine]) -> str:
    """'All machines', 'Gaming PC only', 'Gaming PC, Laptop' or 'No machines'."""
    if allowed is None:
        return "All machines"
    names = [m.name for m in machines if m.id in allowed]
    if not names:
        return "No machines (backups paused)"
    return names[0] + " only" if len(names) == 1 else ", ".join(names)


@router.get("/games/{game_id}/versions", dependencies=[Depends(admin_required)])
def game_versions_partial(
    request: Request,
    game_id: str,
    before: Optional[str] = None,
    type: Optional[str] = None,
    tag: Optional[str] = None,
    db: Session = Depends(get_db),
):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    vtype = type if type in VERSION_TYPES else None
    versions = versions_svc.list_versions(
        db, game.id, limit=PAGE_SIZE, before=before, vtype=vtype, tag=tag or None
    )
    return render(
        request,
        "partials/version_page.html",
        {
            "game": game,
            "versions": versions,
            "head_id": game.head_version_id,
            "filter_type": vtype,
            "filter_tag": tag or "",
            "next_before": versions[-1].id if len(versions) == PAGE_SIZE else None,
        },
    )


@router.get("/games/{game_id}/settings", dependencies=[Depends(admin_required)])
def game_settings_page(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    return render(request, "game_form.html", _game_settings_context(db, game))


def _game_settings_context(db: Session, game: Game, data=None, error=None) -> dict:
    ctx = _game_form_context(db, game, data, error)
    overrides = {(o.alias, o.machine_id): o.path_template for o in games_svc.all_overrides(db, game.id)}
    ctx["machines"] = _machines(db)
    ctx["overrides"] = overrides
    return ctx


@router.post(
    "/games/{game_id}/settings", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
async def game_settings_submit(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    form = await request.form()
    data = _game_form_data(form)
    try:
        body = GamePatch.model_validate(data)
    except ValidationError as exc:
        return render(
            request,
            "game_form.html",
            _game_settings_context(db, game, data, _validation_message(exc)),
            status=400,
        )
    games_svc.update_game(db, game, body)
    flash(request, "Settings saved.")
    return RedirectResponse(f"/games/{game.id}/settings", status_code=303)


@router.post(
    "/games/{game_id}/overrides", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
async def game_override_submit(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    form = await request.form()
    machines = _machines(db)
    changed = 0
    existing = {(o.alias, o.machine_id): o.path_template for o in games_svc.all_overrides(db, game.id)}
    for root in game.roots:
        for m in machines:
            key = f"ovr__{root.alias}__{m.id}"
            if key not in form:
                continue
            value = str(form.get(key, "")).strip() or None
            if existing.get((root.alias, m.id)) != value:
                games_svc.set_override(db, game, root.alias, m.id, value)
                changed += 1
    flash(request, f"Overrides saved ({changed} changed)." if changed else "No override changes.")
    return RedirectResponse(f"/games/{game.id}/settings#overrides", status_code=303)


@router.post(
    "/games/{game_id}/delete", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
def game_delete(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id)
    games_svc.soft_delete(db, game)
    flash(request, f"Deleted {game.name}. Its versions are kept until you purge it.")
    return RedirectResponse("/", status_code=303)


@router.post(
    "/games/{game_id}/restore", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
def game_restore(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    games_svc.restore(db, game)
    flash(request, f"Restored {game.name}.")
    return RedirectResponse(f"/games/{game.id}", status_code=303)


@router.post(
    "/games/{game_id}/purge", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
async def game_purge(request: Request, game_id: str, db: Session = Depends(get_db)):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    form = await request.form()
    if str(form.get("confirm_name", "")).strip() != game.name:
        flash(request, "Type the game's exact name to purge it.", "error")
        return RedirectResponse("/#deleted", status_code=303)
    name = game.name
    games_svc.purge(db, game)
    flash(request, f"Purged {name} and all its versions. Space is freed by the next GC run.")
    return RedirectResponse("/", status_code=303)


# ===================================================================== versions


def _version_row(request: Request, db: Session, version: Version):
    game = db.get(Game, version.game_id)
    return render(
        request,
        "partials/version_row.html",
        {"v": version, "game": game, "head_id": game.head_version_id if game else None},
    )


@router.get("/versions/{version_id}", dependencies=[Depends(admin_required)])
def version_page(
    request: Request, version_id: str, against: Optional[str] = None, db: Session = Depends(get_db)
):
    version = versions_svc.get_version(db, version_id)
    game = db.get(Game, version.game_id)
    if against:
        other = versions_svc.get_version(db, against)
        if other.game_id != version.game_id:
            raise ApiError(400, "different_game", "Both versions must belong to the same game")
    else:
        other = versions_svc.previous_version(db, version)
    diff = versions_svc.diff(db, version, other)
    files = versions_svc.manifest_rows(db, version.id)
    head = db.get(Version, game.head_version_id) if game.head_version_id else None
    recent = versions_svc.list_versions(db, game.id, limit=50)
    return render(
        request,
        "version.html",
        {
            "game": game,
            "v": version,
            "other": other,
            "diff": diff,
            "files": files,
            "head_id": game.head_version_id,
            "head": head,
            "recent": recent,
            "source": db.get(Version, version.source_version_id) if version.source_version_id else None,
        },
    )


@router.post("/versions/{version_id}/pin", dependencies=[Depends(admin_required), Depends(csrf_required)])
def version_pin(request: Request, version_id: str, db: Session = Depends(get_db)):
    v = versions_svc.get_version(db, version_id)
    v = versions_svc.update_version(db, v, VersionPatch(pinned=not v.pinned))
    return _version_row(request, db, v)


@router.post("/versions/{version_id}/meta", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def version_meta(request: Request, version_id: str, db: Session = Depends(get_db)):
    v = versions_svc.get_version(db, version_id)
    form = await request.form()
    tags = [t for t in str(form.get("tags", "")).split(",")]
    try:
        patch = VersionPatch(tags=tags, note=str(form.get("note", "")))
    except ValidationError as exc:
        raise ApiError(400, "invalid", _validation_message(exc))
    v = versions_svc.update_version(db, v, patch)
    if is_htmx(request):
        return _version_row(request, db, v)
    flash(request, "Saved.")
    return RedirectResponse(f"/versions/{v.id}", status_code=303)


@router.post(
    "/versions/{version_id}/promote", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
def version_promote(request: Request, version_id: str, db: Session = Depends(get_db)):
    v = versions_svc.get_version(db, version_id)
    new, unchanged = versions_svc.promote(db, v, None)
    if unchanged:
        flash(request, "That version is already the head.")
    else:
        flash(request, "Made head. Machines syncing this game restore it on their next check.")
    target = f"/games/{v.game_id}"
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@router.post(
    "/versions/{version_id}/delete", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
def version_delete(request: Request, version_id: str, db: Session = Depends(get_db)):
    v = versions_svc.get_version(db, version_id)
    game_id = v.game_id
    versions_svc.delete_version(db, v)
    if is_htmx(request) and request.headers.get("hx-target", "").startswith("v-"):
        return HTMLResponse("")
    flash(request, "Version deleted.")
    target = f"/games/{game_id}"
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


# ===================================================================== machines


@router.get("/machines", dependencies=[Depends(admin_required)])
def machines_page(request: Request, db: Session = Depends(get_db)):
    new_key = request.session.pop("new_machine_key", None)
    return render(
        request,
        "machines.html",
        {"machines": _machines(db, include_revoked=True), "new_key": new_key},
    )


@router.post("/machines", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def machines_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name or len(name) > 100:
        flash(request, "Give the machine a name (up to 100 characters).", "error")
        return RedirectResponse("/machines", status_code=303)
    machine, key = create_machine(db, name)
    # Shown once on the next page view, then gone.
    request.session["new_machine_key"] = {"name": machine.name, "key": key}
    return RedirectResponse("/machines", status_code=303)


@router.post(
    "/machines/{machine_id}/revoke", dependencies=[Depends(admin_required), Depends(csrf_required)]
)
def machines_revoke(request: Request, machine_id: str, db: Session = Depends(get_db)):
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise ApiError(404, "not_found", "Machine not found")
    revoke_machine(db, machine)
    flash(request, f"Revoked the key for {machine.name}.")
    return RedirectResponse("/machines", status_code=303)


# ===================================================================== settings


def _storage_stats(request: Request, db: Session) -> dict:
    ctx = get_ctx(request)
    blob_count, logical, stored = db.execute(
        select(func.count(Blob.hash), func.coalesce(func.sum(Blob.size), 0), func.coalesce(func.sum(Blob.stored_size), 0))
    ).one()
    version_count = db.scalar(select(func.count(Version.id)))
    file_refs = db.scalar(select(func.count()).select_from(VersionFile))
    referenced_size = db.scalar(select(func.coalesce(func.sum(VersionFile.size), 0)))
    db_size = sum(
        p.stat().st_size
        for p in ctx.settings.data_dir.glob("savepoint.db*")
        if p.is_file()
    )
    try:
        disk = shutil.disk_usage(ctx.settings.data_dir)
        disk_free, disk_total = disk.free, disk.total
    except OSError:
        disk_free = disk_total = None
    return {
        "blob_count": blob_count,
        "logical_size": int(logical),
        "stored_size": int(stored),
        "version_count": version_count,
        "file_refs": file_refs,
        "referenced_size": int(referenced_size),
        "db_size": db_size,
        "disk_free": disk_free,
        "disk_total": disk_total,
    }


@router.get("/settings", dependencies=[Depends(admin_required)])
def settings_page(request: Request, db: Session = Depends(get_db)):
    ctx = get_ctx(request)
    runs = list(db.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(15)))
    return render(
        request,
        "settings.html",
        {
            "retention": settings_store.default_retention(db).model_dump(),
            "retention_fields": RETENTION_FIELDS,
            "offsite": settings_store.offsite(db),
            "stats": _storage_stats(request, db),
            "last_retention": ctx.jobs.last_run(RETENTION),
            "last_offsite": ctx.jobs.last_run(OFFSITE),
            "running": {j: ctx.jobs.is_running(j) for j in (RETENTION, OFFSITE)},
            "next_runs": ctx.jobs.next_run_times(),
            "runs": runs,
            "job_names": JOB_NAMES,
            "rclone_config": ctx.settings.rclone_config,
            "schedule": {
                RETENTION: ctx.settings.retention_time,
                OFFSITE: ctx.settings.offsite_time,
            },
        },
    )


@router.post("/settings/retention", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def settings_retention(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        policy = RetentionPolicy.model_validate(_retention_from_form(form))
    except ValidationError as exc:
        flash(request, _validation_message(exc), "error")
        return RedirectResponse("/settings#retention", status_code=303)
    settings_store.set_default_retention(db, policy)
    db.commit()
    flash(request, "Default retention saved. It applies from the next nightly run.")
    return RedirectResponse("/settings#retention", status_code=303)


@router.post("/settings/offsite", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def settings_offsite(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    remote = str(form.get("remote", "")).strip()
    enabled = form.get("enabled") == "on"
    if enabled and ":" not in remote:
        flash(request, "Remote must look like name:path, e.g. gdrive:savepoint.", "error")
        return RedirectResponse("/settings#offsite", status_code=303)
    settings_store.set_offsite(db, enabled, remote)
    db.commit()
    flash(request, "Offsite backup settings saved.")
    return RedirectResponse("/settings#offsite", status_code=303)


@router.post("/settings/password", dependencies=[Depends(admin_required), Depends(csrf_required)])
async def settings_password(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = str(form.get("current", ""))
    new = str(form.get("new", ""))
    confirm = str(form.get("confirm", ""))
    admin = admin_svc.check_password(db, current)
    if admin is None:
        flash(request, "Current password is wrong.", "error")
    elif len(new) < admin_svc.MIN_PASSWORD_LENGTH:
        flash(request, f"New password must be at least {admin_svc.MIN_PASSWORD_LENGTH} characters.", "error")
    elif new != confirm:
        flash(request, "New passwords don't match.", "error")
    else:
        epoch = admin_svc.change_password(db, admin, new)
        # Keep this browser signed in; every other session is invalidated.
        request.session["epoch"] = epoch
        flash(request, "Password changed. Other sessions have been signed out.")
    return RedirectResponse("/settings#password", status_code=303)


@router.post("/settings/jobs/{job}/run", dependencies=[Depends(admin_required), Depends(csrf_required)])
def settings_run_job(request: Request, job: str):
    ctx = get_ctx(request)
    if job not in JOB_NAMES:
        raise ApiError(404, "not_found", "Unknown job")
    kwargs = {"force": True} if job == OFFSITE else {}
    if ctx.jobs.run_in_background(job, **kwargs):
        flash(request, f"{JOB_NAMES[job]} started. Refresh to see the result.")
    else:
        flash(request, f"{JOB_NAMES[job]} is already running.", "error")
    return RedirectResponse("/settings#jobs", status_code=303)


@router.get("/settings/jobs/runs/{run_id}", dependencies=[Depends(admin_required)])
def job_run_page(request: Request, run_id: str, db: Session = Depends(get_db)):
    run = db.get(JobRun, run_id)
    if run is None:
        raise ApiError(404, "not_found", "Job run not found")
    return render(request, "job_run.html", {"run": run, "job_names": JOB_NAMES})


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=301)

