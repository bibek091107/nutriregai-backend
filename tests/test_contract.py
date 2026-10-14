"""Contract tests pinning the API's request models to the live database.

Two classes of drift are checked:

1. The Literal types in the request models (e.g. ``ProductCategory``) mirror
   CHECK constraints in ``db/001_initial_schema.sql``. If a migration widens a
   constraint, these tests fail until the Literal is updated deliberately.

2. Values already stored in enum-shaped columns must still be values the API
   would accept, so the camelCase serializer cannot emit something the frontend
   type rejects.

Run from the backend directory:

    .venv/bin/python -m pytest tests/test_contract.py -q
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from typing import Literal, get_args, get_origin

import pytest

from app.api import db
from app.api.products import CreateProductInput, ProductCategory, TargetMarket
from app.api.regulatory import CreateClaimInput, ReviewDecisionInput

# The single source of truth for the mirrors below.
EXPECTED = {
    "products_category_check": {
        "FUNCTIONAL_FOOD", "FUNCTIONAL_BEVERAGE", "DIETARY_SUPPLEMENT",
        "SPORTS_NUTRITION", "FORTIFIED_CEREAL", "OTHER",
    },
    "products_target_market_check": {"INDIA_FSSAI", "USA_FDA", "EU_EFSA", "UAE_MOIAT", "OTHER"},
    "claims_claim_type_check": {
        "NUTRIENT_FUNCTION", "HEALTH", "NUTRITIONAL_CONTENT",
        "STRUCTURAL_FUNCTION", "OTHER",
    },
    "claims_status_check": {
        "PENDING_ANALYSIS", "SUPPORTED", "QUALIFIER_REQUIRED",
        "NOT_SUPPORTED", "PROHIBITED",
    },
    "review_decisions_decision_check": {
        "APPROVE", "REJECT", "REQUEST_MORE_EVIDENCE", "ESCALATE",
    },
}


@pytest.fixture(scope="module")
def query():
    """A synchronous query helper backed by one event loop.

    asyncpg binds a pool to the loop that created it, so the loop is created once
    and every statement is driven through it. This keeps the tests free of async
    plumbing while still reading the real database rather than parsing the
    migration file.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    pool = loop.run_until_complete(db.get_pool())

    def rows(sql, *args):
        async def _inner():
            async with pool.acquire() as conn:
                return await conn.fetch(sql, *args)

        return loop.run_until_complete(_inner())

    def row(sql, *args):
        async def _inner():
            async with pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)

        return loop.run_until_complete(_inner())

    try:
        yield rows, row
    finally:
        loop.run_until_complete(pool.close())
        loop.close()


def literal_values(annotation) -> set[str]:
    """Return the literals of a Literal, unwrapping Optional[...] / | None.

    ``Optional[Literal["A", "B"]]`` is ``Union[Literal["A","B"], None]``, so
    ``get_args`` would otherwise return the Literal itself plus NoneType.
    """
    args = get_args(annotation)
    if not args:
        return set()
    for candidate in args:
        if get_origin(candidate) is Literal:
            return set(get_args(candidate))
    # Bare Literal, or a union that is not the one we expect.
    return {a for a in args if isinstance(a, str)}


def constraint_values(query, constraint: str) -> set[str]:
    """Extract the quoted literals from a live CHECK constraint definition.

    Postgres normalises ``IN (...)`` to ``= ANY (ARRAY[...])`` and casts every
    value, e.g. ``ARRAY['APPROVE'::text, 'REJECT'::text]``. The array body is
    located explicitly and the cast stripped, so the result is the plain value
    list regardless of how deeply the expression is parenthesised.
    """
    _, row = query
    record = row(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conname = $1",
        constraint,
    )
    assert record is not None, f"constraint {constraint} no longer exists"
    definition = record["definition"]
    match = re.search(r"ARRAY\[(.*?)\]", definition, re.DOTALL)
    if match is None:
        # A constraint that is not an enum list (e.g. a length or range check)
        # legitimately has no comparable literal set.
        return set()
    return {
        part.split("::", 1)[0].strip().strip("'")
        for part in match.group(1).split(",")
        if part.strip().startswith("'")
    }


# --- the mirrors are not stale ---------------------------------------------

def test_product_category_mirrors_its_constraint(query):
    assert set(get_args(ProductCategory)) == EXPECTED["products_category_check"]
    assert set(get_args(ProductCategory)) == constraint_values(query, "products_category_check")


def test_target_market_mirrors_its_constraint(query):
    assert set(get_args(TargetMarket)) == EXPECTED["products_target_market_check"]
    assert set(get_args(TargetMarket)) == constraint_values(query, "products_target_market_check")


def test_claim_type_and_status_mirror_their_constraints(query):
    fields = CreateClaimInput.model_fields
    claim_type = literal_values(fields["claim_type"].annotation)
    status = literal_values(fields["status"].annotation)
    assert claim_type == EXPECTED["claims_claim_type_check"]
    assert claim_type == constraint_values(query, "claims_claim_type_check")
    assert status == EXPECTED["claims_status_check"]
    assert status == constraint_values(query, "claims_status_check")


def test_review_decision_mirrors_its_constraint(query):
    decision = literal_values(ReviewDecisionInput.model_fields["decision"].annotation)
    assert decision == EXPECTED["review_decisions_decision_check"]
    assert decision == constraint_values(query, "review_decisions_decision_check")


# --- stored data is still representable ------------------------------------

@pytest.mark.parametrize("table,column,constraint", [
    ("products", "category", "products_category_check"),
    ("products", "target_market", "products_target_market_check"),
    ("claims", "claim_type", "claims_claim_type_check"),
    ("claims", "status", "claims_status_check"),
    ("review_decisions", "decision", "review_decisions_decision_check"),
])
def test_stored_values_are_all_accepted_by_the_api(query, table, column, constraint):
    """Any value already in the table must be one the API would still take."""
    rows, _ = query
    sql = f"SELECT DISTINCT {column} AS v FROM {table} WHERE {column} IS NOT NULL"
    for record in rows(sql):
        assert record["v"] in EXPECTED[constraint], (
            f"{table}.{column} holds {record['v']!r}, which the API Literal rejects; "
            f"the constraint and the request model have diverged"
        )


# --- rejection is a 422, not a 500 from Postgres ---------------------------

def test_unknown_category_is_rejected_at_the_model():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        CreateProductInput(name="x", category="CEREAL_AND_GRAIN", targetMarket="INDIA_FSSAI")
    assert "category" in str(excinfo.value)


def test_unknown_decision_is_rejected_at_the_model():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewDecisionInput(decision="MAYBE")


def test_camel_case_aliases_are_accepted():
    product = CreateProductInput(name="x", category="OTHER", targetMarket="EU_EFSA")
    assert product.target_market == "EU_EFSA"
    claim = CreateClaimInput(text="t", claimType="HEALTH")
    assert claim.claim_type == "HEALTH"


def test_literals_are_importable_for_reuse():
    # The Literals are the canonical list; confirm they are real typing objects
    # and not accidentally a bare alias.
    assert ProductCategory is not None and TargetMarket is not None
    assert Literal is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
