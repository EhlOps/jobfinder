"""Liveness and readiness.

    /healthz  the process is up; no I/O (liveness: a failing database must not restart the api)
    /readyz   safe to send traffic: the database answers and its schema is not behind this image's
              alembic head (#43); services/health.py decides
"""
from fastapi import Request
from fastapi.responses import JSONResponse

from jobfinder.services import health as service


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def readyz(request: Request) -> JSONResponse:
    r = await service.readiness(request.app.state.ctx.sessions, request.app.state.alembic)
    if not r.ready:
        return JSONResponse({"status": "unavailable", "reason": r.reason}, status_code=503)
    body = {"status": "ok", "revision": r.revision}
    if r.note:
        body["note"] = r.note
    return JSONResponse(body)
