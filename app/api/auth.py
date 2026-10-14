"""Authentication: opaque cookie sessions backed by the ``sessions`` table.

The raw token exists only inside the HttpOnly cookie. What is stored is its
SHA-256 hex digest, so a database dump cannot be replayed as a live session.

Local development creates its user on first login through ``POST
/api/v1/auth/dev-login``. That endpoint is refused unless ``DEV_LOGIN_ENABLED``
is set, so it cannot be reached in a deployed environment by accident.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
from fastapi import Cookie, Depends, HTTPException, status

from app.api import db
from app.api.config import SESSION_COOKIE, get_settings

DEV_LOGIN_ENABLED = os.environ.get("DEV_LOGIN_ENABLED", "").lower() in {"1", "true", "yes"}


def _hash(token: str) -> str:
    """Session tokens are stored as a 64-character SHA-256 hex digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cookie_kwargs() -> dict:
    settings = get_settings()
    return {
        "httponly": True,
        "samesite": "lax",
        "max_age": settings.session_ttl_hours * 3600,
        "secure": (
            os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
            or bool(os.environ.get("VERCEL"))
        ),
    }


async def resolve_session(token: Optional[str]) -> Any:
    """Return the user row for a valid, unexpired, unrevoked session token."""
    if not token:
        return None
    return await db.fetch_one(
        """
        SELECT u.id, u.display_name, u.email, u.organization_name, u.role
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = $1
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
          AND u.is_active
        """,
        _hash(token),
    )


async def create_session(user_id: str, response: Response) -> str:
    """Issue a fresh session token and set it on the response."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=get_settings().session_ttl_hours
    )
    await db.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
        user_id,
        _hash(token),
        expires_at,
    )
    response.set_cookie(SESSION_COOKIE, token, path="/", **_cookie_kwargs())
    return token


async def destroy_session(token: Optional[str], response: Response) -> None:
    """Revoke a session. The row is retained so revocation is auditable."""
    if token:
        await db.execute(
            "UPDATE sessions SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL",
            _hash(token),
        )
    response.delete_cookie(SESSION_COOKIE, path="/")


async def current_user(
    nutrireg_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> Any:
    """FastAPI dependency: the signed-in user, or None when anonymous."""
    return await resolve_session(nutrireg_session)


async def require_user(
    user: Any = Depends(current_user),
) -> Any:
    """FastAPI dependency for endpoints that must not serve anonymous callers.

    ``Depends`` is explicit here on purpose. Passing ``current_user`` as a bare
    default value leaves the parameter annotated as ``Any``, which FastAPI does
    not resolve as a sub-dependency, so the endpoint would silently receive the
    function object and every route would appear authenticated.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return user
