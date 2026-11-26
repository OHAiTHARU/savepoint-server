from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, require_machine, require_ui
from ..errors import not_found
from ..models import Machine
from ..schemas import MachineCreate
from ..security import hash_api_key, new_api_key
from ..timeutil import iso, utcnow

router = APIRouter(tags=["machines"])


def serialize(m: Machine) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "created_at": iso(m.created_at),
        "last_seen_at": iso(m.last_seen_at),
        "revoked_at": iso(m.revoked_at),
        "clock_skew_ms": m.clock_skew_ms,
        "clock_checked_at": iso(m.clock_checked_at),
    }


def create_machine(db: Session, name: str) -> tuple[Machine, str]:
    key = new_api_key()
    machine = Machine(name=name.strip(), api_key_hash=hash_api_key(key))
    db.add(machine)
    db.commit()
    return machine, key


def revoke_machine(db: Session, machine: Machine) -> None:
    if machine.revoked_at is None:
        machine.revoked_at = utcnow()
        db.commit()


@router.get("/machines/me")
def me(principal: Principal = Depends(require_machine)):
    m = principal.machine
    # server_time lets the client measure its own clock offset; sending
    # X-Savepoint-Client-Time on requests records that offset server-side.
    return {"id": m.id, "name": m.name, "server_time": iso(utcnow()), "clock_skew_ms": m.clock_skew_ms}


@router.get("/machines")
def list_machines(_: Principal = Depends(require_ui), db: Session = Depends(get_db)):
    rows = db.scalars(select(Machine).order_by(Machine.created_at))
    return {"machines": [serialize(m) for m in rows]}


@router.post("/machines", status_code=201)
def post_machine(
    body: MachineCreate, _: Principal = Depends(require_ui), db: Session = Depends(get_db)
):
    machine, key = create_machine(db, body.name)
    return {"id": machine.id, "name": machine.name, "api_key": key}


@router.delete("/machines/{machine_id}", status_code=204)
def delete_machine(
    machine_id: str, _: Principal = Depends(require_ui), db: Session = Depends(get_db)
):
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise not_found("Machine")
    revoke_machine(db, machine)
