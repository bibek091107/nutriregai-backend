"""Health endpoints: liveness and readiness probes.

Deliberately unauthenticated: it is how developers, load balancers, and orchestrators
(like Render, Kubernetes, or Vercel) check whether the service is alive and ready
to accept traffic. It reports database connectivity without exposing connection details.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api import db

router = APIRouter(tags=["health"])


@router.get("/health/live")
@router.get("/api/v1/health/live")
async def liveness() -> dict[str, Any]:
    """Liveness probe: returns 200 if the Python process is running and accepting HTTP requests."""
    return {"status": "ok", "live": True}


@router.get("/health/ready")
@router.get("/api/v1/health/ready")
async def readiness(response: Response) -> dict[str, Any]:
    """Readiness probe: returns 200 if the PostgreSQL database is reachable, 503 if unavailable."""
    database_ok = True
    try:
        val = await db.fetch_value("SELECT 1")
        if val != 1:
            database_ok = False
    except Exception:
        database_ok = False

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": False}

    return {"status": "ok", "database": True}


@router.get("/health")
@router.get("/api/health")
@router.get("/api/v1/health")
async def health(response: Response) -> dict[str, Any]:
    """General health check: reports overall status and database connectivity."""
    database_ok = True
    try:
        val = await db.fetch_value("SELECT 1")
        if val != 1:
            database_ok = False
    except Exception:  # noqa: BLE001 - health must never raise uncaught
        database_ok = False

    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ok" if database_ok else "degraded", "database": database_ok}
