"""Errors the app raises on purpose, and the one handler that turns them into HTTP responses.

Raise an `AppError` subclass from a service; the handler answers `{"detail": message, "code": code}`
with the class's status. `detail` is what the SPA shows, as with HTTPException; `code` is a stable
machine-readable name. Routers move from HTTPException to these area by area (M4).
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status = 400
    code = "error"
    message = "Something went wrong"

    def __init__(self, message: str | None = None):
        self.detail = message or self.message
        super().__init__(self.detail)


class Unauthorized(AppError):
    status, code, message = 401, "unauthorized", "Not signed in"


class Forbidden(AppError):
    status, code, message = 403, "forbidden", "Not allowed"


class NotFound(AppError):
    status, code, message = 404, "not_found", "Not found"


class Conflict(AppError):
    status, code, message = 409, "conflict", "Conflict"


class Invalid(AppError):
    status, code, message = 422, "invalid", "Invalid input"


class RateLimited(AppError):
    status, code, message = 429, "rate_limited", "Too many requests. Try again later."


async def _handle(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)


def install(app: FastAPI) -> None:
    """Register the AppError handler (api/app.py does this once)."""
    app.add_exception_handler(AppError, _handle)
