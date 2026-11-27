from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, require_auth
from ..errors import ApiError
from ..schemas import GameCreate, GamePatch, OverrideIn, SyncToggle
from ..services import games as svc

router = APIRouter(tags=["games"])


def _one(db: Session, game, principal: Principal) -> dict:
    return svc.serialize(game, svc.overrides_for(db, [game.id], principal.machine_id), principal.machine_id)


@router.get("/games")
def list_games(principal: Principal = Depends(require_auth), db: Session = Depends(get_db)):
    games = svc.list_games(db)
    overrides = svc.overrides_for(db, [g.id for g in games], principal.machine_id)
    return {"games": [svc.serialize(g, overrides, principal.machine_id) for g in games]}


@router.post("/games", status_code=201)
def create_game(
    body: GameCreate, principal: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    return _one(db, svc.create_game(db, body), principal)


@router.get("/games/{game_id}")
def get_game(
    game_id: str, principal: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    return _one(db, svc.get_game(db, game_id), principal)


@router.patch("/games/{game_id}")
def patch_game(
    game_id: str,
    body: GamePatch,
    principal: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    game = svc.update_game(db, svc.get_game(db, game_id), body)
    return _one(db, game, principal)


@router.delete("/games/{game_id}", status_code=204)
def delete_game(
    game_id: str, _: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    svc.soft_delete(db, svc.get_game(db, game_id))


@router.put("/games/{game_id}/roots/{alias}/override")
def put_override(
    game_id: str,
    alias: str,
    body: OverrideIn,
    machine_id: Optional[str] = Query(
        None, description="Web UI only: which machine to set the override for"
    ),
    principal: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if principal.machine is not None:
        target = principal.machine.id  # a machine can only set its own override
    elif machine_id:
        target = machine_id
    else:
        raise ApiError(400, "machine_required", "machine_id is required with a web UI session")
    game = svc.get_game(db, game_id)
    svc.set_override(db, game, alias, target, body.path_template)
    return svc.serialize(game, svc.overrides_for(db, [game.id], target), target)


@router.put("/games/{game_id}/sync")
def put_sync(
    game_id: str,
    body: SyncToggle,
    machine_id: Optional[str] = Query(None, description="Web UI only: which machine to change"),
    principal: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Turn syncing this game on or off for the calling machine (or ?machine_id= from the UI)."""
    if principal.machine is not None:
        target = principal.machine.id
    elif machine_id:
        target = machine_id
    else:
        raise ApiError(400, "machine_required", "machine_id is required with a web UI session")
    game = svc.get_game(db, game_id)
    svc.set_sync_enabled(db, game, target, body.enabled)
    return svc.serialize(game, svc.overrides_for(db, [game.id], target), target)
