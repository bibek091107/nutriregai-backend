"""Health endpoint.

Deliberately unauthenticated: it is how a developer or a load balancer checks the
service is up, and it exposes nothing but liveness. It reports database
connectivity, but never the connection string.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api import db

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/api/health")
@router.get("/api/v1/health")
async def health() -> dict[str, Any]:
    database_ok = True
    try:
        await db.fetch_value("SELECT 1")
    except Exception:  # noqa: BLE001 - health must never raise
        database_ok = False
    return {"status": "ok" if database_ok else "degraded", "database": database_ok}
