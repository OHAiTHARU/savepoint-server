import hashlib
import json

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, require_auth
from ..models import Game, Version
from ..timeutil import iso

router = APIRouter(tags=["heads"])


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    candidates = [c.strip().removeprefix("W/") for c in header.split(",")]
    return etag in candidates


@router.get("/heads")
def heads(request: Request, _: Principal = Depends(require_auth), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Game.id, Version.id, Version.created_at, Version.machine_id)
        .join(Version, Version.id == Game.head_version_id)
        .where(Game.deleted_at.is_(None))
        .order_by(Game.id)
    )
    body = {
        game_id: {"version_id": vid, "created_at": iso(created), "machine_id": machine_id}
        for game_id, vid, created, machine_id in rows
    }
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(payload, media_type="application/json", headers=headers)
