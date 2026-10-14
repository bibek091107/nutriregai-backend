"""Product, dashboard and analysis endpoints."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api import db, serializers
from app.api.auth import require_user
from app.api.queries import load_owned_product_row, load_product

router = APIRouter(prefix="/api/v1", tags=["products"])

# These mirror the CHECK constraints in db/001_initial_schema.sql. They exist so an
# out-of-range value is answered with a 422 naming the offending field, rather
# than reaching Postgres and surfacing as an unhandled 500. test_contract.py
# asserts these stay in sync with the live constraints, so they cannot drift.
ProductCategory = Literal[
    "FUNCTIONAL_FOOD", "FUNCTIONAL_BEVERAGE", "DIETARY_SUPPLEMENT",
    "SPORTS_NUTRITION", "FORTIFIED_CEREAL", "OTHER",
]
TargetMarket = Literal["INDIA_FSSAI", "USA_FDA", "EU_EFSA", "UAE_MOIAT", "OTHER"]


class CreateProductInput(BaseModel):
    """Product create/update payload.

    Field names are camelCase because the frontend contract
    (``CreateProductInput`` in src/lib/api/types.ts) is camelCase throughout, and
    the client serialises the object it is given without renaming keys. An alias
    with ``populate_by_name`` set means the snake_case form is still accepted, so
    a hand-written curl against the API works too.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=160)
    category: ProductCategory
    target_market: TargetMarket = Field(alias="targetMarket")
    description: Optional[str] = None
    sku: Optional[str] = Field(default=None, max_length=64)


@router.get("/dashboard/summary")
async def dashboard_summary(_: Any = Depends(require_user)) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM v_dashboard_summary")
    return serializers.dashboard_summary(row)


@router.get("/products")
async def list_products(_: Any = Depends(require_user)) -> list[dict[str, Any]]:
    """All products with counts, risks and the nine workflow stages.

    Filters are applied by the frontend client, so the whole set is returned.
    """
    rows = await db.fetch_all("SELECT * FROM v_product_stats ORDER BY created_at DESC")
    if not rows:
        return []
    stages = await db.fetch_all(
        "SELECT * FROM v_product_workflow ORDER BY product_id, position"
    )
    by_product: dict[str, list] = {}
    for stage in stages:
        by_product.setdefault(str(stage["product_id"]), []).append(stage)
    return [serializers.product(row, by_product.get(str(row["id"]), [])) for row in rows]


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: CreateProductInput, _: Any = Depends(require_user)
) -> dict[str, Any]:
    """Create a product.

    The database trigger inserts the nine workflow stages and the initial
    NOT_STARTED analysis, so this endpoint stays a single insert and cannot
    produce a half-initialised dossier.
    """
    row = await db.fetch_one(
        """
        INSERT INTO products (name, sku, category, target_market, description)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        payload.name.strip(),
        payload.sku,
        payload.category,
        payload.target_market,
        payload.description,
    )
    loaded = await load_product(str(row["id"]))
    return serializers.product(loaded["stats"], loaded["stages"])


@router.get("/products/{product_id}")
async def get_product(product_id: str, _: Any = Depends(require_user)) -> dict[str, Any]:
    loaded = await load_product(product_id)
    return serializers.product(loaded["stats"], loaded["stages"])


@router.patch("/products/{product_id}")
async def update_product(
    product_id: str, payload: CreateProductInput, _: Any = Depends(require_user)
) -> dict[str, Any]:
    """Partially update a product.

    Every field is written, so an omitted field is cleared to NULL. That matches
    the PATCH semantics the frontend uses, where a blank input means "remove".
    """
    await load_owned_product_row(product_id)
    await db.execute(
        """
        UPDATE products
        SET name = $2, sku = $3, category = $4,
            target_market = $5, description = $6
        WHERE id = $1
        """,
        product_id,
        payload.name.strip(),
        payload.sku,
        payload.category,
        payload.target_market,
        payload.description,
    )
    loaded = await load_product(product_id)
    return serializers.product(loaded["stats"], loaded["stages"])


# --------------------------------------------------------------------------
# Nested read models
# --------------------------------------------------------------------------
@router.get("/products/{product_id}/ingredients")
async def list_ingredients(
    product_id: str, _: Any = Depends(require_user)
) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        """
        SELECT * FROM v_product_ingredient_list
        WHERE product_id = $1
        ORDER BY lower(name)
        """,
        product_id,
    )
    return [serializers.ingredient(row) for row in rows]


class CreateIngredientInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=320)
    cas_number: Optional[str] = Field(default=None, alias="casNumber", max_length=64)
    amount_per_serving: Optional[str] = Field(default=None, alias="amountPerServing")
    amount_unit: Optional[str] = Field(default=None, alias="amountUnit")
    purpose: Optional[str] = None
    notes: Optional[str] = None


@router.post("/products/{product_id}/ingredients", status_code=status.HTTP_201_CREATED)
async def add_product_ingredient(
    product_id: str, payload: CreateIngredientInput, _: Any = Depends(require_user)
) -> dict[str, Any]:
    await load_owned_product_row(product_id)

    # Look up existing ingredient by name (case-insensitive), or insert a new one.
    ingredient_row = await db.fetch_one(
        "SELECT id FROM ingredients WHERE lower(name) = lower($1)",
        payload.name.strip(),
    )
    if ingredient_row is None:
        ingredient_row = await db.fetch_one(
            "INSERT INTO ingredients (name, cas_number) VALUES ($1, $2) RETURNING id",
            payload.name.strip(),
            payload.cas_number.strip() if payload.cas_number else None,
        )

    try:
        pi_row = await db.fetch_one(
            """
            INSERT INTO product_ingredients 
                (product_id, ingredient_id, amount_per_serving, amount_unit)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            product_id,
            ingredient_row["id"],
            float(payload.amount_per_serving) if payload.amount_per_serving else None,
            payload.amount_unit.strip() if payload.amount_unit else None,
        )
    except Exception as exc:
        if "unique constraint" in str(exc).lower() or "duplicate key" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ingredient is already added to this product.",
            )
        raise

    # The view v_product_ingredient_list exposes pi.id (the junction row PK),
    # not pi.ingredient_id. Query by the junction row id.
    record = await db.fetch_one(
        "SELECT * FROM v_product_ingredient_list WHERE id = $1",
        pi_row["id"]
    )
    return serializers.ingredient(record)


from fastapi import File, UploadFile
import csv
import io

@router.post("/products/{product_id}/ingredients/import", status_code=status.HTTP_201_CREATED)
async def import_product_ingredients(
    product_id: str, file: UploadFile = File(...), _: Any = Depends(require_user)
) -> dict[str, Any]:
    await load_owned_product_row(product_id)
    
    content = await file.read()
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid CSV file format.")
    
    imported = 0
    for row in reader:
        name = row.get("Name") or row.get("name") or row.get("Ingredient")
        if not name:
            continue
            
        cas = row.get("CAS") or row.get("cas_number") or row.get("CAS Number")
        amount = row.get("Amount") or row.get("amount_per_serving") or row.get("Amount Per Serving")
        unit = row.get("Unit") or row.get("amount_unit")
        
        # Look up existing ingredient by name, or insert a new one.
        ingredient_row = await db.fetch_one(
            "SELECT id FROM ingredients WHERE lower(name) = lower($1)",
            name.strip(),
        )
        if ingredient_row is None:
            ingredient_row = await db.fetch_one(
                "INSERT INTO ingredients (name, cas_number) VALUES ($1, $2) RETURNING id",
                name.strip(),
                cas.strip() if cas else None,
            )
        
        try:
            amt_val = float(amount) if amount else None
        except ValueError:
            amt_val = None
            
        try:
            await db.execute(
                """
                INSERT INTO product_ingredients 
                    (product_id, ingredient_id, amount_per_serving, amount_unit)
                VALUES ($1, $2, $3, $4)
                """,
                product_id,
                ingredient_row["id"],
                amt_val,
                unit.strip() if unit else None,
            )
            imported += 1
        except Exception:
            # Skip duplicates or invalid data
            pass
            
    return {"importedCount": imported}


@router.get("/products/{product_id}/claims")
async def list_product_claims(
    product_id: str, _: Any = Depends(require_user)
) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        "SELECT * FROM v_claim_list WHERE product_id = $1 "
        "ORDER BY last_reviewed_at DESC NULLS LAST, text",
        product_id,
    )
    return [serializers.claim(row) for row in rows]


@router.get("/products/{product_id}/documents")
async def list_product_documents(
    product_id: str, _: Any = Depends(require_user)
) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        "SELECT * FROM v_document_list WHERE product_id = $1 ORDER BY uploaded_at DESC",
        product_id,
    )
    return [serializers.document(row) for row in rows]


@router.get("/products/{product_id}/findings")
async def list_product_findings(
    product_id: str, _: Any = Depends(require_user)
) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        """
        SELECT * FROM v_finding_list
        WHERE product_id = $1 AND is_current
        ORDER BY
            CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
            detected_at DESC
        """,
        product_id,
    )
    return [serializers.finding(row) for row in rows]


@router.get("/products/{product_id}/compliance-items")
async def list_compliance_items(
    product_id: str, _: Any = Depends(require_user)
) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        "SELECT * FROM v_compliance_item_list WHERE product_id = $1 ORDER BY category NULLS LAST, label",
        product_id,
    )
    return [serializers.compliance_item(row) for row in rows]


@router.get("/products/{product_id}/reports")
async def list_reports(product_id: str, _: Any = Depends(require_user)) -> list[dict[str, Any]]:
    await load_owned_product_row(product_id)
    rows = await db.fetch_all(
        # v_report_list has no created_at; generated_at is nullable, so NULLS
        # LAST keeps queued reports (not yet generated) at the end.
        "SELECT * FROM v_report_list WHERE product_id = $1 "
        "ORDER BY generated_at DESC NULLS LAST, title",
        product_id,
    )
    return [serializers.report(row) for row in rows]


@router.post("/products/{product_id}/reports", status_code=status.HTTP_202_ACCEPTED)
async def request_report(
    product_id: str, _: Any = Depends(require_user)
) -> dict[str, Any]:
    """Queue a report for the latest completed analysis."""
    import asyncio
    from app.api.pdf_report import generate_audit_report_pdf

    product = await load_owned_product_row(product_id)
    
    # 1. Find the latest COMPLETED analysis
    analysis = await db.fetch_one(
        """
        SELECT id FROM analyses 
        WHERE product_id = $1 AND is_current AND status = 'COMPLETED'
        """,
        product_id
    )
    if not analysis:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No completed analysis is available for this product. Run Start Analysis first."
        )
    
    analysis_id = str(analysis["id"])
    report_title = f"Compliance report — {product['name']}"

    # 2. Check if a report for this analysis is already in progress/ready
    existing = await db.fetch_one(
        "SELECT id, status FROM reports WHERE analysis_id = $1",
        analysis_id
    )
    
    if existing:
        if existing["status"] == "FAILED":
            # Delete the failed one so we can regenerate
            await db.execute("DELETE FROM reports WHERE id = $1", existing["id"])
        else:
            # Just return the existing report to prevent duplicates for the same analysis
            record = await db.fetch_one("SELECT * FROM v_report_list WHERE id = $1", existing["id"])
            return serializers.report(record)

    # 3. Create a PENDING/GENERATING report row
    row = await db.fetch_one(
        """
        INSERT INTO reports (product_id, analysis_id, title, status)
        VALUES ($1, $2, $3, 'GENERATING')
        RETURNING id
        """,
        product_id,
        analysis_id,
        report_title,
    )
    report_id = str(row["id"])

    # 4. Generate the PDF asynchronously
    async def _generate_pdf():
        try:
            storage_key = await generate_audit_report_pdf(product_id, analysis_id, report_title)
            await db.execute(
                """
                UPDATE reports 
                SET status = 'READY', storage_key = $2, generated_at = now(), updated_at = now()
                WHERE id = $1
                """,
                report_id, storage_key
            )
        except Exception as exc:
            import logging
            logging.error("Failed to generate PDF: %s", exc, exc_info=True)
            await db.execute(
                """
                UPDATE reports 
                SET status = 'FAILED', failure_reason = $2, updated_at = now()
                WHERE id = $1
                """,
                report_id, str(exc)[:500]
            )

    asyncio.create_task(_generate_pdf())

    record = await db.fetch_one("SELECT * FROM v_report_list WHERE id = $1", report_id)
    return serializers.report(record)


from fastapi.responses import FileResponse
from app.api.config import get_settings
import os

@router.get("/reports/{report_id}/download")
async def download_report(report_id: str) -> FileResponse:
    """Download a generated audit report PDF."""
    row = await db.fetch_one(
        "SELECT storage_key, title FROM reports WHERE id = $1 AND status = 'READY'", 
        report_id
    )
    if not row or not row["storage_key"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Report is not ready or does not exist."
        )
        
    settings = get_settings()
    file_path = os.path.join(settings.upload_dir, row["storage_key"])
    
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Report file not found on disk."
        )
        
    filename = f"{row['title'].replace(' ', '_')}.pdf"
    return FileResponse(
        path=file_path, 
        filename=filename, 
        media_type="application/pdf"
    )


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
@router.get("/products/{product_id}/analysis")
async def get_analysis(product_id: str, _: Any = Depends(require_user)) -> dict[str, Any]:
    """The current analysis for a product.

    Always returns 200 with a valid object, including for a brand-new product,
    because the bootstrap trigger seeds a NOT_STARTED analysis row.
    """
    await load_owned_product_row(product_id)
    row = await db.fetch_one(
        "SELECT * FROM analyses WHERE product_id = $1 AND is_current", product_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No current analysis row for this product.",
        )
    return serializers.analysis(row)


@router.post("/products/{product_id}/analysis", status_code=status.HTTP_202_ACCEPTED)
async def start_analysis(product_id: str, _: Any = Depends(require_user)) -> dict[str, Any]:
    """Run a regulatory analysis on the product's actual dossier.

    Validates the product, creates a new analysis run (superseding the previous
    one), and executes the analysis engine which examines real ingredients,
    claims, and documents.
    """
    import asyncio
    from app.api.analysis_engine import run_analysis

    product = await load_owned_product_row(product_id)

    # --- Prevent duplicate concurrent analyses ---
    existing = await db.fetch_one(
        """
        SELECT id, status FROM analyses
        WHERE product_id = $1 AND is_current AND status IN ('QUEUED', 'PROCESSING')
        """,
        product_id,
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An analysis is already in progress for this product.",
        )

    # --- Validate: at least one ingredient ---
    ingredient_count = await db.fetch_value(
        "SELECT count(*) FROM product_ingredients WHERE product_id = $1 AND is_active",
        product_id,
    )
    if ingredient_count == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add at least one ingredient before starting regulatory analysis.",
        )

    # --- Supersede previous analysis ---
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE analyses SET is_current = FALSE WHERE product_id = $1 AND is_current",
                product_id,
            )
            new_row = await conn.fetchrow(
                """
                INSERT INTO analyses (product_id, status, progress, is_current, started_at)
                VALUES ($1, 'QUEUED', 0, TRUE, now())
                RETURNING *
                """,
                product_id,
            )

    analysis_id = str(new_row["id"])

    # --- Run the analysis (inline for now; can be moved to a task queue) ---
    async def _run_in_background():
        try:
            await run_analysis(product_id, analysis_id)
        except Exception:
            pass  # Errors are recorded in the analyses table by the engine

    asyncio.create_task(_run_in_background())

    return serializers.analysis(new_row)
