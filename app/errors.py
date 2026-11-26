from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Rendered as {"error": {"code", "message", ...extra}} with the given status."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def bad_request(code: str, message: str, **extra: Any) -> ApiError:
    return ApiError(400, code, message, **extra)
