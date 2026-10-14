"""Session endpoints.

``GET /api/v1/auth/session`` returns the signed-in user, or 401 when anonymous.
The frontend treats a failed call as ``anonymous`` and renders a neutral
signed-out state, so 401 is the correct answer rather than a null body.

``POST /api/v1/auth/dev-login`` exists for local development only and refuses to
run unless ``DEV_LOGIN_ENABLED`` is set.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api import db, serializers
from app.api.auth import (
    DEV_LOGIN_ENABLED,
    SESSION_COOKIE,
    create_session,
    destroy_session,
    resolve_session,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/session")
async def read_session(
    nutrireg_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, Any]:
    """The current user, or 401 when there is no valid session."""
    user = await resolve_session(nutrireg_session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in."
        )
    return serializers.session_user(user)


class DevLoginInput(BaseModel):
    """Dev-login payload.

    camelCase aliases, matching the rest of the contract. This endpoint is not
    part of the frontend contract -- the app has no sign-in screen -- so the
    naming is chosen for consistency with every other request body.
    """

    model_config = ConfigDict(populate_by_name=True)

    display_name: str = Field(min_length=1, max_length=160, alias="displayName")
    email: str = Field(min_length=3, max_length=320)
    organization_name: Optional[str] = Field(default=None, alias="organizationName")
    role: Optional[str] = None


@router.post("/dev-login")
async def dev_login(payload: DevLoginInput, response: Response) -> dict[str, Any]:
    """Create or reuse a local user and issue a session cookie.

    Refused unless ``DEV_LOGIN_ENABLED`` is truthy, so this cannot be used to
    mint sessions in an environment where it was not deliberately switched on.
    """
    if not DEV_LOGIN_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found."
        )

    email = payload.email.strip().lower()
    # The users_email_check constraint requires lower(email) = btrim(email), so
    # reject rather than silently repair a malformed address.
    if "@" not in email or email != email.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="email must be a valid address.",
        )
    row = await db.fetch_one(
        """
        INSERT INTO users (display_name, email, organization_name, role)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (email) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            organization_name = EXCLUDED.organization_name,
            role = EXCLUDED.role,
            is_active = TRUE
        RETURNING id, display_name, email, organization_name, role
        """,
        payload.display_name.strip(),
        email,
        payload.organization_name,
        payload.role,
    )
    await create_session(str(row["id"]), response)
    return serializers.session_user(row)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=FastAPIResponse,
)
async def logout(
    response: Response,
    nutrireg_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> FastAPIResponse:
    """Revoke the current session and clear the cookie."""
    await destroy_session(nutrireg_session, response)
    return FastAPIResponse(status_code=status.HTTP_204_NO_CONTENT)
