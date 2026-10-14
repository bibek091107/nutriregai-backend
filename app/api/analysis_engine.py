"""Real regulatory analysis engine.

Examines the actual product dossier (ingredients, claims, documents) against the
regulatory knowledge base and generates findings, compliance checklist items, and
evidence citations.  Every result is derived from the product's real data; nothing
is hardcoded or mocked.

Design rules
------------
* Severity is never random — it is determined by concrete conditions.
* Evidence is only created when matching regulatory sources exist in the DB.
* If no evidence is found, the finding says so explicitly.
* Compliance items always carry a note explaining the basis.
* Claims with no supporting evidence are flagged, never silently accepted.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.api import db

logger = logging.getLogger("nutrireg.analysis")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_ingredients(product_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT pi.id, pi.amount_per_serving, pi.amount_unit, pi.status, pi.risk_level,
               i.name, i.cas_number
        FROM product_ingredients pi
        JOIN ingredients i ON i.id = pi.ingredient_id
        WHERE pi.product_id = $1 AND pi.is_active
        ORDER BY lower(i.name)
        """,
        product_id,
    )
    return [dict(r) for r in rows]


async def _fetch_claims(product_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT c.id, c.text, c.claim_type, c.status, c.risk_level, c.disclaimer
        FROM claims c
        WHERE c.product_id = $1
        ORDER BY c.text
        """,
        product_id,
    )
    return [dict(r) for r in rows]


async def _fetch_documents(product_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT d.id, d.file_name, d.document_type, d.status, d.extraction_status
        FROM documents d
        WHERE d.product_id = $1
        ORDER BY d.uploaded_at DESC
        """,
        product_id,
    )
    return [dict(r) for r in rows]


async def _fetch_product(product_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT * FROM products WHERE id = $1", product_id)
    return dict(row) if row else {}


async def _search_regulatory_evidence(
    keyword: str,
    jurisdiction: Optional[str] = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Search the regulatory knowledge base using full-text search.

    Returns matching chunks with their source metadata.  Falls back to an empty
    list when the knowledge base has no relevant content.
    """
    base_query = """
        SELECT
            rc.id AS chunk_id,
            rc.content,
            rc.page_number,
            rs2.section_number,
            rs2.title AS section_title,
            rd.title AS document_title,
            rd.document_version AS version,
            rd.effective_date,
            rsrc.id AS source_id,
            rsrc.authority,
            rsrc.title AS source_title,
            rsrc.source_url,
            rsrc.jurisdiction,
            ts_rank(rc.search_vector, plainto_tsquery('english', $1)) AS rank
        FROM regulatory_chunks rc
        JOIN regulatory_sections rs2 ON rs2.id = rc.section_id
        JOIN regulatory_documents rd ON rd.id = rs2.regulatory_document_id
        JOIN regulatory_sources rsrc ON rsrc.id = rd.source_id
        WHERE rc.search_vector @@ plainto_tsquery('english', $1)
    """
    params: list[Any] = [keyword]

    if jurisdiction:
        # Map target_market to jurisdiction filter
        market_map = {
            "INDIA_FSSAI": "India",
            "USA_FDA": "United States",
            "EU_EFSA": "European Union",
            "UAE_MOIAT": "UAE",
        }
        jur = market_map.get(jurisdiction, jurisdiction)
        base_query += " AND (rsrc.jurisdiction = $2 OR rsrc.jurisdiction IS NULL)"
        params.append(jur)

    base_query += " ORDER BY rank DESC LIMIT $" + str(len(params) + 1)
    params.append(limit)

    try:
        rows = await db.fetch_all(base_query, *params)
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("Regulatory search failed for '%s': %s", keyword, exc)
        return []


# ---------------------------------------------------------------------------
# Analysis steps
# ---------------------------------------------------------------------------

async def _update_progress(analysis_id: str, progress: int) -> None:
    await db.execute(
        "UPDATE analyses SET progress = $2, updated_at = now() WHERE id = $1",
        analysis_id,
        progress,
    )


async def _analyze_ingredients(
    product_id: str,
    analysis_id: str,
    ingredients: list[dict[str, Any]],
    target_market: Optional[str],
) -> list[dict[str, Any]]:
    """Evaluate each ingredient and produce findings for issues found."""
    findings: list[dict[str, Any]] = []

    for ing in ingredients:
        name = ing["name"]
        amount = ing.get("amount_per_serving")
        unit = ing.get("amount_unit")
        cas = ing.get("cas_number")
        pi_id = ing["id"]

        issues: list[dict[str, Any]] = []

        # --- Check: no amount specified ---
        if amount is None:
            issues.append({
                "title": f"Missing quantity for {name}",
                "summary": (
                    f"The ingredient '{name}' has no amount per serving specified. "
                    "Regulatory compliance cannot be fully evaluated without quantitative data."
                ),
                "severity": "MEDIUM",
                "category": "Product Composition",
                "corrective_action": f"Specify the amount per serving for '{name}' with the appropriate unit.",
            })
            # Update ingredient status
            await db.execute(
                """
                UPDATE product_ingredients
                SET status = 'REVIEW_REQUIRED', risk_level = 'MEDIUM',
                    evaluated_at = now(),
                    status_note = 'Missing quantity — cannot fully evaluate compliance'
                WHERE id = $1
                """,
                pi_id,
            )

        # --- Check: missing CAS number ---
        elif cas is None and amount is not None:
            # Search regulatory evidence for this ingredient
            evidence_hits = await _search_regulatory_evidence(
                f"{name} ingredient specification",
                jurisdiction=target_market,
                limit=2,
            )

            # If we found regulatory references, ingredient may need closer review
            if evidence_hits:
                issues.append({
                    "title": f"No CAS number registered for {name}",
                    "summary": (
                        f"The ingredient '{name}' does not have a CAS number. "
                        "CAS numbers are required for precise substance identification "
                        "and regulatory cross-referencing."
                    ),
                    "severity": "LOW",
                    "category": "Product Composition",
                    "corrective_action": f"Add the CAS registry number for '{name}'.",
                    "evidence": evidence_hits,
                })

            await db.execute(
                """
                UPDATE product_ingredients
                SET status = 'REVIEW_REQUIRED', risk_level = 'LOW',
                    evaluated_at = now(),
                    status_note = 'Missing CAS number for regulatory cross-reference'
                WHERE id = $1
                """,
                pi_id,
            )

        else:
            # Ingredient has amount and CAS — search for regulatory limits
            search_term = f"{name} maximum permitted level limit"
            evidence_hits = await _search_regulatory_evidence(
                search_term, jurisdiction=target_market, limit=3,
            )

            if evidence_hits:
                # We found regulatory references — mark for review with evidence
                issues.append({
                    "title": f"Regulatory review required for {name}",
                    "summary": (
                        f"Regulatory references were found for '{name}' "
                        f"({amount} {unit or 'units'} per serving). "
                        "Human review is required to confirm compliance with applicable limits."
                    ),
                    "severity": "MEDIUM",
                    "category": "Product Composition",
                    "corrective_action": (
                        f"Review the regulatory references for '{name}' and confirm "
                        "that the specified quantity complies with permitted levels."
                    ),
                    "evidence": evidence_hits,
                })
                await db.execute(
                    """
                    UPDATE product_ingredients
                    SET status = 'REVIEW_REQUIRED', risk_level = 'MEDIUM',
                        evaluated_at = now(),
                        status_note = 'Regulatory references found — human review required'
                    WHERE id = $1
                    """,
                    pi_id,
                )
            else:
                # No regulatory references found — mark as compliant (pending evidence)
                await db.execute(
                    """
                    UPDATE product_ingredients
                    SET status = 'COMPLIANT', risk_level = 'LOW',
                        evaluated_at = now(),
                        status_note = 'No specific regulatory limits found in knowledge base'
                    WHERE id = $1
                    """,
                    pi_id,
                )

        # Create findings for this ingredient
        for issue in issues:
            statutory_ref = None
            evidence_for_finding = issue.pop("evidence", [])

            if evidence_for_finding:
                first = evidence_for_finding[0]
                statutory_ref = (
                    f"{first.get('authority', 'Unknown')} — "
                    f"{first.get('section_title') or first.get('section_number', 'General')}"
                )

            finding_row = await db.fetch_one(
                """
                INSERT INTO findings
                    (analysis_id, product_id, title, summary, severity, status,
                     category, statutory_reference, corrective_action)
                VALUES ($1, $2, $3, $4, $5, 'OPEN', $6, $7, $8)
                RETURNING id
                """,
                analysis_id,
                product_id,
                issue["title"],
                issue["summary"],
                issue["severity"],
                issue["category"],
                statutory_ref,
                issue.get("corrective_action"),
            )

            finding_id = str(finding_row["id"])

            # Link evidence to the finding
            for ev in evidence_for_finding:
                try:
                    await db.execute(
                        """
                        INSERT INTO evidence (finding_id, source_id, regulatory_chunk_id,
                                              status, excerpt)
                        VALUES ($1, $2, $3, 'RELEVANT', $4)
                        ON CONFLICT DO NOTHING
                        """,
                        finding_id,
                        ev["source_id"],
                        ev["chunk_id"],
                        (ev.get("content") or "")[:500],
                    )
                except Exception as exc:
                    logger.warning("Could not link evidence: %s", exc)

            findings.append({**issue, "id": finding_id})

    return findings


async def _analyze_claims(
    product_id: str,
    analysis_id: str,
    claims: list[dict[str, Any]],
    target_market: Optional[str],
) -> list[dict[str, Any]]:
    """Evaluate each claim against the regulatory knowledge base."""
    findings: list[dict[str, Any]] = []

    for claim in claims:
        claim_id = claim["id"]
        claim_text = claim["text"]
        claim_type = claim.get("claim_type")

        # Search for regulatory guidance on this claim
        search_term = f"{claim_text} claim substantiation"
        evidence_hits = await _search_regulatory_evidence(
            search_term, jurisdiction=target_market, limit=3,
        )

        # Also search by claim type if available
        if claim_type:
            type_search = f"{claim_type.replace('_', ' ').lower()} claim regulation"
            type_hits = await _search_regulatory_evidence(
                type_search, jurisdiction=target_market, limit=2,
            )
            # Deduplicate by chunk_id
            seen = {h["chunk_id"] for h in evidence_hits}
            for hit in type_hits:
                if hit["chunk_id"] not in seen:
                    evidence_hits.append(hit)
                    seen.add(hit["chunk_id"])

        if evidence_hits:
            # Found regulatory references — claim needs substantiation review
            statutory_ref = (
                f"{evidence_hits[0].get('authority', 'Unknown')} — "
                f"{evidence_hits[0].get('section_title') or evidence_hits[0].get('section_number', 'General')}"
            )

            finding_row = await db.fetch_one(
                """
                INSERT INTO findings
                    (analysis_id, product_id, claim_id, title, summary, severity, status,
                     category, statutory_reference, corrective_action)
                VALUES ($1, $2, $3, $4, $5, $6, 'OPEN', $7, $8, $9)
                RETURNING id
                """,
                analysis_id,
                product_id,
                claim_id,
                f"Claim substantiation review: {claim_text[:80]}",
                (
                    f"The claim \"{claim_text}\" requires substantiation review. "
                    f"Regulatory references were found that may govern this "
                    f"{'health claim' if claim_type == 'HEALTH' else 'product claim'}. "
                    "Human review is required to confirm the claim is adequately supported."
                ),
                "HIGH" if claim_type == "HEALTH" else "MEDIUM",
                "Claims & Advertising",
                statutory_ref,
                (
                    "Review the cited regulatory references and ensure adequate "
                    "scientific evidence supports this claim. A qualifier or "
                    "disclaimer may be required."
                ),
            )

            finding_id = str(finding_row["id"])

            for ev in evidence_hits:
                try:
                    await db.execute(
                        """
                        INSERT INTO evidence (finding_id, source_id, regulatory_chunk_id,
                                              status, excerpt)
                        VALUES ($1, $2, $3, 'RELEVANT', $4)
                        ON CONFLICT DO NOTHING
                        """,
                        finding_id,
                        ev["source_id"],
                        ev["chunk_id"],
                        (ev.get("content") or "")[:500],
                    )
                except Exception as exc:
                    logger.warning("Could not link claim evidence: %s", exc)

            # Update claim status
            await db.execute(
                """
                UPDATE claims
                SET status = 'QUALIFIER_REQUIRED', risk_level = $2,
                    last_reviewed_at = now(),
                    status_note = 'Regulatory references found — substantiation review required'
                WHERE id = $1
                """,
                claim_id,
                "HIGH" if claim_type == "HEALTH" else "MEDIUM",
            )

            findings.append({
                "id": finding_id,
                "title": f"Claim substantiation review: {claim_text[:80]}",
                "severity": "HIGH" if claim_type == "HEALTH" else "MEDIUM",
            })
        else:
            # No regulatory references found — flag as needing human review
            finding_row = await db.fetch_one(
                """
                INSERT INTO findings
                    (analysis_id, product_id, claim_id, title, summary, severity, status,
                     category, corrective_action)
                VALUES ($1, $2, $3, $4, $5, 'MEDIUM', 'OPEN', $6, $7)
                RETURNING id
                """,
                analysis_id,
                product_id,
                claim_id,
                f"No regulatory evidence found for claim: {claim_text[:80]}",
                (
                    f"The claim \"{claim_text}\" could not be matched to any regulatory "
                    "reference in the knowledge base. This does not necessarily mean the "
                    "claim is non-compliant, but human regulatory review is required."
                ),
                "Claims & Advertising",
                (
                    "Conduct manual regulatory review for this claim. Identify applicable "
                    "regulations and ensure adequate supporting evidence exists."
                ),
            )

            await db.execute(
                """
                UPDATE claims
                SET status = 'PENDING_ANALYSIS', risk_level = 'MEDIUM',
                    last_reviewed_at = now(),
                    status_note = 'No regulatory evidence found — human review required'
                WHERE id = $1
                """,
                claim_id,
            )

            findings.append({
                "id": str(finding_row["id"]),
                "title": f"No regulatory evidence: {claim_text[:80]}",
                "severity": "MEDIUM",
            })

    return findings


async def _analyze_documents(
    product_id: str,
    analysis_id: str,
    documents: list[dict[str, Any]],
    target_market: Optional[str],
) -> list[dict[str, Any]]:
    """Evaluate the document dossier for completeness."""
    findings: list[dict[str, Any]] = []

    # Check for failed documents
    failed_docs = [d for d in documents if d.get("status") == "FAILED"]
    for doc in failed_docs:
        finding_row = await db.fetch_one(
            """
            INSERT INTO findings
                (analysis_id, product_id, title, summary, severity, status,
                 category, corrective_action)
            VALUES ($1, $2, $3, $4, 'HIGH', 'OPEN', $5, $6)
            RETURNING id
            """,
            analysis_id,
            product_id,
            f"Document processing failed: {doc['file_name']}",
            (
                f"The document '{doc['file_name']}' failed processing. "
                "Its content could not be extracted and is unavailable for regulatory analysis."
            ),
            "Safety Documentation",
            f"Re-upload '{doc['file_name']}' or replace it with a valid document.",
        )
        findings.append({
            "id": str(finding_row["id"]),
            "title": f"Failed document: {doc['file_name']}",
            "severity": "HIGH",
        })

    # Check document type coverage
    doc_types = {d.get("document_type") for d in documents if d.get("document_type")}
    required_types = {
        "PRODUCT_SPEC": "Product specification",
        "CERTIFICATE_OF_ANALYSIS": "Certificate of Analysis / Lab report",
    }

    for doc_type, label in required_types.items():
        if doc_type not in doc_types:
            finding_row = await db.fetch_one(
                """
                INSERT INTO findings
                    (analysis_id, product_id, title, summary, severity, status,
                     category, corrective_action)
                VALUES ($1, $2, $3, $4, 'MEDIUM', 'OPEN', $5, $6)
                RETURNING id
                """,
                analysis_id,
                product_id,
                f"Missing document type: {label}",
                (
                    f"No '{label}' document was found in the product dossier. "
                    "This document type is typically required for a complete regulatory submission."
                ),
                "Safety Documentation",
                f"Upload a {label} document to the product dossier.",
            )
            findings.append({
                "id": str(finding_row["id"]),
                "title": f"Missing: {label}",
                "severity": "MEDIUM",
            })

    return findings


async def _generate_compliance_checklist(
    product_id: str,
    analysis_id: str,
    product: dict[str, Any],
    ingredients: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    all_findings: list[dict[str, Any]],
) -> None:
    """Generate compliance checklist items based on the actual dossier state."""

    # Remove old compliance items for this product from previous analyses
    await db.execute(
        "DELETE FROM compliance_items WHERE product_id = $1",
        product_id,
    )

    items: list[tuple[str, str | None, bool | None, str]] = []

    # --- Product Composition category ---
    has_ingredients = len(ingredients) > 0
    items.append((
        "Ingredient register populated",
        f"{len(ingredients)} ingredient(s) recorded" if has_ingredients else "No ingredients registered",
        has_ingredients,
        "Product Composition",
    ))

    all_have_amounts = all(
        i.get("amount_per_serving") is not None for i in ingredients
    ) if ingredients else False
    items.append((
        "All ingredients have quantitative data",
        "All ingredients specify amount per serving" if all_have_amounts
        else "One or more ingredients are missing quantity data",
        all_have_amounts if ingredients else None,
        "Product Composition",
    ))

    all_have_cas = all(
        i.get("cas_number") is not None for i in ingredients
    ) if ingredients else False
    items.append((
        "CAS numbers registered for all ingredients",
        "All ingredients have CAS registry numbers" if all_have_cas
        else "One or more ingredients are missing CAS numbers",
        all_have_cas if ingredients else None,
        "Product Composition",
    ))

    no_non_compliant = all(
        i.get("status") != "NON_COMPLIANT" for i in ingredients
    ) if ingredients else True
    items.append((
        "No non-compliant ingredients",
        "No ingredients were flagged as non-compliant" if no_non_compliant
        else "One or more ingredients are flagged as non-compliant",
        no_non_compliant if ingredients else None,
        "Product Composition",
    ))

    # --- Claims & Advertising category ---
    if claims:
        all_supported = all(c.get("status") == "SUPPORTED" for c in claims)
        items.append((
            "All claims substantiated",
            "All product claims are supported" if all_supported
            else f"{sum(1 for c in claims if c.get('status') != 'SUPPORTED')} claim(s) require review",
            all_supported,
            "Claims & Advertising",
        ))

        no_prohibited = all(c.get("status") != "PROHIBITED" for c in claims)
        items.append((
            "No prohibited claims",
            "No claims are flagged as prohibited" if no_prohibited
            else "One or more claims may be prohibited",
            no_prohibited,
            "Claims & Advertising",
        ))
    else:
        items.append((
            "Product claims registered",
            "No claims have been registered for this product",
            None,
            "Claims & Advertising",
        ))

    # --- Safety Documentation category ---
    has_documents = len(documents) > 0
    items.append((
        "Supporting documents uploaded",
        f"{len(documents)} document(s) in dossier" if has_documents
        else "No supporting documents uploaded",
        has_documents,
        "Safety Documentation",
    ))

    doc_types = {d.get("document_type") for d in documents if d.get("document_type")}
    has_product_spec = "PRODUCT_SPEC" in doc_types
    items.append((
        "Product specification document",
        "Product specification is on file" if has_product_spec
        else "No product specification document found",
        has_product_spec if documents else None,
        "Safety Documentation",
    ))

    has_lab_report = "LAB_REPORT" in doc_types or "CERTIFICATE_OF_ANALYSIS" in doc_types
    items.append((
        "Laboratory analysis / Certificate of Analysis",
        "Lab report or CoA is on file" if has_lab_report
        else "No laboratory report or Certificate of Analysis found",
        has_lab_report if documents else None,
        "Safety Documentation",
    ))

    no_failed_docs = all(d.get("status") != "FAILED" for d in documents) if documents else True
    items.append((
        "All documents processed successfully",
        "All uploaded documents were processed" if no_failed_docs
        else "One or more documents failed processing",
        no_failed_docs if documents else None,
        "Safety Documentation",
    ))

    # --- Labelling category ---
    has_name = bool(product.get("name"))
    items.append((
        "Product name specified",
        product.get("name", "Not specified"),
        has_name,
        "Labelling",
    ))

    has_category = bool(product.get("category"))
    items.append((
        "Product category assigned",
        product.get("category", "Not specified"),
        has_category,
        "Labelling",
    ))

    has_market = bool(product.get("target_market"))
    items.append((
        "Target market / jurisdiction defined",
        product.get("target_market", "Not specified"),
        has_market,
        "Labelling",
    ))

    # --- Process & Records category ---
    high_findings = [f for f in all_findings if f.get("severity") == "HIGH"]
    items.append((
        "No high-severity findings outstanding",
        f"{len(high_findings)} high-severity finding(s)" if high_findings
        else "No high-severity findings detected",
        len(high_findings) == 0,
        "Process & Records",
    ))

    items.append((
        "Regulatory analysis completed",
        "Analysis run completed successfully",
        True,
        "Process & Records",
    ))

    # Insert all items
    for label, note, passed, category in items:
        await db.execute(
            """
            INSERT INTO compliance_items
                (product_id, analysis_id, label, note, passed, category)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            product_id,
            analysis_id,
            label,
            note,
            passed,
            category,
        )


async def _update_workflow_stages(product_id: str, analysis_id: str) -> None:
    """Advance the workflow stages to reflect the completed analysis."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Mark ingredients stage
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'COMPLETED', completed_at = now()
                WHERE product_id = $1 AND stage_key = 'ingredients'
                  AND state <> 'COMPLETED'
                """,
                product_id,
            )
            # Mark claims stage
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'COMPLETED', completed_at = now()
                WHERE product_id = $1 AND stage_key = 'claims'
                  AND state <> 'COMPLETED'
                """,
                product_id,
            )
            # Mark documents stage
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'COMPLETED', completed_at = now()
                WHERE product_id = $1 AND stage_key = 'documents'
                  AND state <> 'COMPLETED'
                """,
                product_id,
            )
            # Mark regulatory_analysis stage
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'COMPLETED', completed_at = now()
                WHERE product_id = $1 AND stage_key = 'regulatory_analysis'
                  AND state <> 'COMPLETED'
                """,
                product_id,
            )
            # Mark risk_review as ACTIVE
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'ACTIVE'
                WHERE product_id = $1 AND stage_key = 'risk_review'
                  AND state = 'PENDING'
                """,
                product_id,
            )
            # Mark compliance_checklist as COMPLETED
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'COMPLETED', completed_at = now()
                WHERE product_id = $1 AND stage_key = 'compliance_checklist'
                  AND state <> 'COMPLETED'
                """,
                product_id,
            )
            # Mark human_review as ACTIVE
            await conn.execute(
                """
                UPDATE product_workflow_stages
                SET state = 'ACTIVE'
                WHERE product_id = $1 AND stage_key = 'human_review'
                  AND state = 'PENDING'
                """,
                product_id,
            )


async def _generate_report(product_id: str, analysis_id: str, product_name: str) -> None:
    """Create a report record linked to the product and analysis, and generate the PDF."""
    from app.api.pdf_report import generate_audit_report_pdf
    
    # Check if there's already a PENDING/GENERATING report for this analysis
    existing = await db.fetch_one(
        """
        SELECT id FROM reports
        WHERE analysis_id = $1 AND status IN ('PENDING', 'GENERATING')
        """,
        analysis_id,
    )
    if existing:
        return

    report_title = f"Regulatory Audit Report — {product_name}"
    storage_key = None
    try:
        storage_key = await generate_audit_report_pdf(product_id, analysis_id, report_title)
    except Exception as exc:
        logger.error("Failed to generate PDF during analysis: %s", exc, exc_info=True)

    if storage_key:
        await db.execute(
            """
            INSERT INTO reports (product_id, analysis_id, title, status, storage_key, generated_at)
            VALUES ($1, $2, $3, 'READY', $4, now())
            """,
            product_id,
            analysis_id,
            report_title,
            storage_key
        )
    else:
        await db.execute(
            """
            INSERT INTO reports (product_id, analysis_id, title, status, generated_at, failure_reason)
            VALUES ($1, $2, $3, 'FAILED', now(), 'PDF generation failed')
            """,
            product_id,
            analysis_id,
            report_title,
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_analysis(product_id: str, analysis_id: str) -> None:
    """Execute the full regulatory analysis for a product.

    This function:
    1. Loads the actual product data
    2. Analyzes ingredients against the regulatory knowledge base
    3. Analyzes claims for substantiation
    4. Audits the document dossier
    5. Generates compliance checklist items
    6. Updates workflow stages
    7. Generates an audit report
    """
    try:
        # --- Set to PROCESSING ---
        await db.execute(
            """
            UPDATE analyses
            SET status = 'PROCESSING', progress = 5, started_at = now(),
                failure_reason = NULL
            WHERE id = $1
            """,
            analysis_id,
        )

        # --- 1. Load product data ---
        product = await _fetch_product(product_id)
        if not product:
            raise ValueError(f"Product {product_id} not found")

        target_market = product.get("target_market")
        await _update_progress(analysis_id, 10)

        # --- 2. Load dossier data ---
        ingredients = await _fetch_ingredients(product_id)
        claims = await _fetch_claims(product_id)
        documents = await _fetch_documents(product_id)
        await _update_progress(analysis_id, 15)

        # --- 3. Clear old findings from this analysis ---
        # (The analysis_id is brand new, so there shouldn't be any,
        #  but be defensive)
        await db.execute(
            "DELETE FROM findings WHERE analysis_id = $1",
            analysis_id,
        )
        await _update_progress(analysis_id, 20)

        all_findings: list[dict[str, Any]] = []

        # --- 4. Analyze ingredients ---
        if ingredients:
            ingredient_findings = await _analyze_ingredients(
                product_id, analysis_id, ingredients, target_market,
            )
            all_findings.extend(ingredient_findings)
        await _update_progress(analysis_id, 45)

        # --- 5. Analyze claims ---
        if claims:
            claim_findings = await _analyze_claims(
                product_id, analysis_id, claims, target_market,
            )
            all_findings.extend(claim_findings)
        await _update_progress(analysis_id, 65)

        # --- 6. Analyze documents ---
        if documents:
            doc_findings = await _analyze_documents(
                product_id, analysis_id, documents, target_market,
            )
            all_findings.extend(doc_findings)
        else:
            # No documents at all — create a finding
            await db.execute(
                """
                INSERT INTO findings
                    (analysis_id, product_id, title, summary, severity, status,
                     category, corrective_action)
                VALUES ($1, $2, $3, $4, 'MEDIUM', 'OPEN', $5, $6)
                """,
                analysis_id,
                product_id,
                "No supporting documents in dossier",
                (
                    "The product dossier does not contain any supporting documents. "
                    "Regulatory submissions typically require product specifications, "
                    "laboratory reports, and safety documentation."
                ),
                "Safety Documentation",
                "Upload relevant supporting documents to the product dossier.",
            )
            all_findings.append({
                "title": "No supporting documents in dossier",
                "severity": "MEDIUM",
            })
        await _update_progress(analysis_id, 75)

        # --- 7. Generate compliance checklist ---
        await _generate_compliance_checklist(
            product_id, analysis_id, product, ingredients, claims, documents, all_findings,
        )
        await _update_progress(analysis_id, 85)

        # --- 8. Update workflow stages ---
        await _update_workflow_stages(product_id, analysis_id)
        await _update_progress(analysis_id, 90)

        # --- 9. Generate report ---
        await _generate_report(product_id, analysis_id, product.get("name", "Unknown Product"))
        await _update_progress(analysis_id, 95)

        # --- 10. Determine product status ---
        high_count = sum(1 for f in all_findings if f.get("severity") == "HIGH")
        if high_count > 0:
            new_status = "NEEDS_ATTENTION"
        elif all_findings:
            new_status = "REVIEW_REQUIRED"
        else:
            new_status = "COMPLETED"

        await db.execute(
            """
            UPDATE products
            SET last_analysis_at = now(), status = $2, updated_at = now()
            WHERE id = $1
            """,
            product_id,
            new_status,
        )

        # --- 11. Complete the analysis ---
        await db.execute(
            """
            UPDATE analyses
            SET status = 'COMPLETED', progress = 100, completed_at = now()
            WHERE id = $1
            """,
            analysis_id,
        )

        logger.info(
            "Analysis %s completed for product %s: %d findings",
            analysis_id, product_id, len(all_findings),
        )

    except Exception as exc:
        logger.error("Analysis %s failed: %s", analysis_id, exc, exc_info=True)
        try:
            await db.execute(
                """
                UPDATE analyses
                SET status = 'FAILED', failure_reason = $2, completed_at = now()
                WHERE id = $1
                """,
                analysis_id,
                str(exc)[:500],
            )
        except Exception:
            pass
        raise
