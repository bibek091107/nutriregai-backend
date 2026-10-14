"""Shared query helpers.

Product reads always go through ``v_product_stats`` plus ``v_product_workflow``.
The view computes the four counts and the three risk totals, so no endpoint
re-derives them and the numbers can never disagree between screens.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, status

from app.api import db

PRODUCT_STATS = "v_product_stats"
PRODUCT_WORKFLOW = "v_product_workflow"


async def load_product(product_id: str) -> dict[str, Any]:
    """Load one product with its nine ordered stages, or raise 404."""
    row = await db.fetch_one(f"SELECT * FROM {PRODUCT_STATS} WHERE id = $1", product_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found."
        )
    stages = await db.fetch_all(
        f"SELECT * FROM {PRODUCT_WORKFLOW} WHERE product_id = $1 ORDER BY position",
        product_id,
    )
    if not stages:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Product has no workflow stages; the bootstrap trigger did not run.",
        )
    return {"stats": row, "stages": stages}


async def load_owned_product_row(product_id: str) -> Any:
    """Return the raw ``products`` row, raising 404 when absent.

    Used for writes, which target base tables rather than read views.
    """
    row = await db.fetch_one("SELECT * FROM products WHERE id = $1", product_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found."
        )
    return row


def optional_uuid(value: Optional[str], field: str) -> Optional[str]:
    """Validate an optional UUID path/query value before it reaches SQL."""
    if value in (None, ""):
        return None
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field} must be a UUID.",
        ) from None
