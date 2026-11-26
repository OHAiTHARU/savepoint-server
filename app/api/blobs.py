import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..blobstore import HashMismatch, is_valid_hash
from ..deps import Principal, get_ctx, get_db, require_auth
from ..errors import ApiError, bad_request, not_found
from ..models import Blob
from ..schemas import BlobCheck

router = APIRouter(tags=["blobs"])


def _check_hash(h: str) -> str:
    if not is_valid_hash(h):
        raise bad_request("invalid_hash", "Hash must be 64 lowercase hex characters (SHA-256)")
    return h


@router.post("/blobs/check")
def check(body: BlobCheck, _: Principal = Depends(require_auth), db: Session = Depends(get_db)):
    wanted = []
    for h in body.hashes:
        _check_hash(h)
        if h not in wanted:
            wanted.append(h)
    have: set[str] = set()
    for i in range(0, len(wanted), 500):
        have.update(db.scalars(select(Blob.hash).where(Blob.hash.in_(wanted[i : i + 500]))))
    return {"missing": [h for h in wanted if h not in have]}


@router.put("/blobs/{sha256}")
async def put_blob(
    sha256: str,
    request: Request,
    _: Principal = Depends(require_auth),
    db: Session = Depends(get_db),
):
    _check_hash(sha256)
    ctx = get_ctx(request)
    if db.get(Blob, sha256) is not None and ctx.blobs.exists(sha256):
        # Idempotent: drain the body and report success without rewriting.
        async for _chunk in request.stream():
            pass
        return JSONResponse({"hash": sha256, "created": False}, status_code=200)

    writer = ctx.blobs.writer()
    try:
        async for chunk in request.stream():
            # Compression is CPU-bound; keep the event loop responsive.
            await asyncio.to_thread(writer.write, chunk)
        size, stored = await asyncio.to_thread(writer.commit, sha256)
    except HashMismatch as exc:
        raise bad_request(
            "hash_mismatch", "Uploaded content does not match the hash", actual=exc.actual
        )
    except BaseException:
        writer.abort()
        raise

    existing = db.get(Blob, sha256)
    if existing is None:
        db.add(Blob(hash=sha256, size=size, stored_size=stored))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # a concurrent upload of the same blob won
    else:
        existing.stored_size = stored
        db.commit()
    return JSONResponse({"hash": sha256, "created": True, "size": size}, status_code=201)


@router.get("/blobs/{sha256}")
def get_blob(
    sha256: str, request: Request, _: Principal = Depends(require_auth), db: Session = Depends(get_db)
):
    _check_hash(sha256)
    ctx = get_ctx(request)
    blob = db.get(Blob, sha256)
    if blob is None:
        raise not_found("Blob")
    if not ctx.blobs.exists(sha256):
        raise ApiError(500, "blob_missing_on_disk", "Blob is recorded but its file is missing")
    return StreamingResponse(
        ctx.blobs.open_stream(sha256),
        media_type="application/octet-stream",
        headers={"Content-Length": str(blob.size), "ETag": f'"{sha256}"'},
    )
