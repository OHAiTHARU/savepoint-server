from fastapi import APIRouter

from . import blobs, games, heads, machines, misc, versions

router = APIRouter(prefix="/api/v1")
for module in (misc, machines, games, heads, blobs, versions):
    router.include_router(module.router)
