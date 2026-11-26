import os
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from ..deps import Principal, get_ctx, get_db, require_auth, require_ui
from ..errors import bad_request
from ..models import Game
from ..schemas import VersionCreate, VersionPatch
from ..services import games as games_svc
from ..services import versions as svc

router = APIRouter(tags=["versions"])


def _head_id(db: Session, game_id: str) -> Optional[str]:
    game = db.get(Game, game_id)
    return game.head_version_id if game else None


@router.get("/games/{game_id}/versions")
def list_versions(
    game_id: str,
    limit: int = Query(50, ge=1, le=500),
    before: Optional[str] = None,
    type: Optional[str] = None,
    tag: Optional[str] = None,
    _: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    game = games_svc.get_game(db, game_id, include_deleted=True)
    rows = svc.list_versions(db, game.id, limit=limit, before=before, vtype=type, tag=tag)
    return {
        "versions": [svc.serialize(v, game.head_version_id) for v in rows],
        # Pass as `before` to get the next page; null when this is the last page.
        "next_before": rows[-1].id if len(rows) == limit else None,
    }


@router.post("/games/{game_id}/versions")
def create_version(
    game_id: str,
    body: VersionCreate,
    request: Request,
    principal: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    ctx = get_ctx(request)
    game = games_svc.get_game(db, game_id)
    version, unchanged = svc.create_version(
        db, game, body, principal.machine_id, ctx.settings.max_manifest_files
    )
    data = svc.serialize(version, game.head_version_id)
    data["unchanged"] = unchanged
    return JSONResponse(data, status_code=200 if unchanged else 201)


@router.get("/versions/{version_id}")
def get_version(
    version_id: str, _: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    v = svc.get_version(db, version_id)
    return svc.serialize(v, _head_id(db, v.game_id), files=svc.manifest_rows(db, v.id))


@router.get("/versions/{version_id}/diff")
def diff_version(
    version_id: str,
    against: Optional[str] = None,
    _: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    v = svc.get_version(db, version_id)
    if against:
        other = svc.get_version(db, against)
        if other.game_id != v.game_id:
            raise bad_request("different_game", "Both versions must belong to the same game")
    else:
        other = svc.previous_version(db, v)
    return svc.diff(db, v, other)


@router.get("/versions/{version_id}/download")
def download(
    version_id: str,
    request: Request,
    _: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    ctx = get_ctx(request)
    v = svc.get_version(db, version_id)
    game = db.get(Game, v.game_id)
    path = svc.build_zip(db, ctx.blobs, v, ctx.settings.tmp_dir)
    stamp = v.created_at.astimezone(ctx.settings.tz).strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in game.name).strip() or "game"
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{safe} {stamp} {v.type}.zip",
        background=BackgroundTask(os.unlink, path),
    )


@router.patch("/versions/{version_id}")
def patch_version(
    version_id: str,
    body: VersionPatch,
    _: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    v = svc.update_version(db, svc.get_version(db, version_id), body)
    return svc.serialize(v, _head_id(db, v.game_id))


@router.post("/versions/{version_id}/promote")
def promote(
    version_id: str, principal: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    new, unchanged = svc.promote(db, svc.get_version(db, version_id), principal.machine_id)
    data = svc.serialize(new, _head_id(db, new.game_id))
    data["unchanged"] = unchanged
    return JSONResponse(data, status_code=200 if unchanged else 201)


@router.delete("/versions/{version_id}", status_code=204)
def delete_version(
    version_id: str, _: Principal = Depends(require_ui), db: Session = Depends(get_db)
):
    svc.delete_version(db, svc.get_version(db, version_id))
