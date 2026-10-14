"""Claims, findings, evidence, reviews and statutory-source endpoints."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api import db, serializers
from app.api.auth import require_user
from app.api.queries import load_owned_product_row

router = APIRouter(prefix="/api/v1", tags=["regulatory"])


class CreateClaimInput(BaseModel):
    """Claim payload.

    The frontend sends ``{ text, claimType, targetMarket }``. ``targetMarket`` is
    accepted and deliberately ignored: the claim's market is derived from the
    parent product by ``v_claim_list``, so storing it would let a claim disagree
    with its own dossier.
    """

    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(min_length=1)
    claim_type: Optional[Literal[
        "NUTRIENT_FUNCTION", "HEALTH", "NUTRITIONAL_CONTENT",
        "STRUCTURAL_FUNCTION", "OTHER",
    ]] = Field(default=None, alias="claimType")
    status: Literal[
        "PENDING_ANALYSIS", "SUPPORTED", "QUALIFIER_REQUIRED",
        "NOT_SUPPORTED", "PROHIBITED",
    ] = "PENDING_ANALYSIS"
    disclaimer: Optional[str] = None
    status_note: Optional[str] = Field(default=None, alias="statusNote")
    target_market: Optional[str] = Field(default=None, alias="targetMarket")


@router.get("/claims")
async def list_claims(_: Any = Depends(require_user)) -> list[dict[str, Any]]:
    # v_claim_list carries no created_at, so order by the review timestamp
    # and then the claim text for a stable total order.
    rows = await db.fetch_all(
        "SELECT * FROM v_claim_list ORDER BY last_reviewed_at DESC NULLS LAST, text"
    )
    return [serializers.claim(row) for row in rows]


@router.post(
    "/products/{product_id}/claims", status_code=status.HTTP_201_CREATED
)
async def create_claim(
    product_id: str, payload: CreateClaimInput, _: Any = Depends(require_user)
) -> dict[str, Any]:
    """Register a claim on a product.

    ``target_market`` is deliberately not accepted: the view derives it from the
    parent product, so a claim can never disagree with its dossier.
    """
    await load_owned_product_row(product_id)
    row = await db.fetch_one(
        """
        INSERT INTO claims (product_id, text, claim_type, status, disclaimer, status_note)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        product_id,
        payload.text.strip(),
        payload.claim_type,
        payload.status,
        payload.disclaimer,
        payload.status_note,
    )
    record = await db.fetch_one("SELECT * FROM v_claim_list WHERE id = $1", row["id"])
    return serializers.claim(record)


@router.get("/findings")
async def list_findings(_: Any = Depends(require_user)) -> list[dict[str, Any]]:
    """Current-analysis findings across all products, most severe first."""
    rows = await db.fetch_all(
        """
        SELECT * FROM v_finding_list
        WHERE is_current
        ORDER BY
            CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
            detected_at DESC
        """
    )
    return [serializers.finding(row) for row in rows]


@router.get("/evidence")
async def list_evidence(_: Any = Depends(require_user)) -> list[dict[str, Any]]:
    """Every citation, with source, document, section and page resolved by join."""
    rows = await db.fetch_all(
        "SELECT * FROM v_evidence_citation ORDER BY created_at DESC"
    )
    return [serializers.evidence(row) for row in rows]


@router.get("/reviews")
async def list_reviews(_: Any = Depends(require_user)) -> list[dict[str, Any]]:
    """Review tasks; title, severity and assignee resolved through the view."""
    rows = await db.fetch_all(
        "SELECT * FROM v_review_task_list ORDER BY requested_at DESC"
    )
    return [serializers.review_task(row) for row in rows]


class ReviewDecisionInput(BaseModel):
    # Mirrors review_decisions_decision_check in db/001_initial_schema.sql, so an
    # unknown verdict is a 422 here instead of a 500 from Postgres.
    decision: Literal["APPROVE", "REJECT", "REQUEST_MORE_EVIDENCE", "ESCALATE"]
    note: Optional[str] = None


_DECISION_TO_STATUS = {
    "APPROVE": "APPROVED",
    "REJECT": "REJECTED",
    "REQUEST_MORE_EVIDENCE": "IN_REVIEW",
    "ESCALATE": "ESCALATED",
}


@router.post("/reviews/{review_id}/decision")
async def submit_review_decision(
    review_id: str, payload: ReviewDecisionInput, user: Any = Depends(require_user)
) -> dict[str, Any]:
    """Record a review decision.

    The decision is appended to ``review_decisions`` (append-only, enforced by a
    trigger) and the task's denormalised status is updated to match. The
    reviewer is taken from the session, never from the request body, so a client
    cannot decide on someone else's behalf.
    """
    if payload.decision not in _DECISION_TO_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"decision must be one of {sorted(_DECISION_TO_STATUS)}.",
        )

    task = await db.fetch_one("SELECT * FROM review_tasks WHERE id = $1", review_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Review task not found."
        )
    if task["status"] in {"APPROVED", "REJECTED", "ESCALATED", "CANCELLED"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This review task has already been decided.",
        )

    new_status = _DECISION_TO_STATUS[payload.decision]
    pool = await db.get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO review_decisions (review_task_id, reviewer_id, decision, note) "
                "VALUES ($1, $2, $3, $4)",
                review_id,
                user["id"],
                payload.decision,
                payload.note,
            )
            await connection.execute(
                "UPDATE review_tasks SET status = $2, decided_at = now(), decision_note = $3 "
                "WHERE id = $1",
                review_id,
                new_status,
                payload.note,
            )

    record = await db.fetch_one("SELECT * FROM v_review_task_list WHERE id = $1", review_id)
    return serializers.review_task(record)


@router.get("/statutory-sources")
async def list_statutory_sources(
    _: Any = Depends(require_user),
) -> list[dict[str, Any]]:
    """Indexed statutory sources with citation counts computed from evidence."""
    rows = await db.fetch_all(
        "SELECT * FROM v_regulatory_source_list ORDER BY authority, title"
    )
    return [serializers.statutory_source(row) for row in rows]


@router.get("/statutory-sources/{source_id}")
async def get_statutory_source(
    source_id: str, _: Any = Depends(require_user)
) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT * FROM v_regulatory_source_list WHERE id = $1", source_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Statutory source not found."
        )
    return serializers.statutory_source(row)
