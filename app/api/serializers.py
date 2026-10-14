"""Row -> contract serialisers.

Each function maps one database view onto exactly the object shape declared in
``src/lib/api/types.ts``. The mapping is written out field by field rather than
converted generically, so a view column renamed in SQL can never silently change
an API field name: an unrecognised column is simply not read, and a missing
column raises immediately instead of shipping a renamed field.

The frontend contract uses camelCase while PostgreSQL uses snake_case; the
conversion happens here and nowhere else.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

Row = Mapping[str, Any]


def _iso(value: Any) -> Optional[str]:
    """Render a timestamptz/date as an ISO string, or None."""
    if value is None:
        return None
    return value.isoformat()


def _int(value: Any) -> int:
    return 0 if value is None else int(value)


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
def product(row: Row, stages: Sequence[Row]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "sku": row["sku"],
        "category": row["category"],
        "targetMarket": row["target_market"],
        "description": row["description"],
        "status": row["status"],
        "stages": [
            {
                "key": stage["stage_key"],
                "label": stage["label"],
                "state": stage["state"],
                "completedAt": _iso(stage["completed_at"]),
            }
            for stage in stages
        ],
        "risks": {
            "high": _int(row["risk_high"]),
            "medium": _int(row["risk_medium"]),
            "low": _int(row["risk_low"]),
        },
        "claimCount": _int(row["claim_count"]),
        "documentCount": _int(row["document_count"]),
        "ingredientCount": _int(row["ingredient_count"]),
        "findingCount": _int(row["finding_count"]),
        "lastAnalysisAt": _iso(row["last_analysis_at"]),
        "createdAt": _iso(row["created_at"]),
    }


def ingredient(row: Row) -> dict[str, Any]:
    amount = row["amount_per_serving"]
    return {
        "id": str(row["id"]),
        "productId": str(row["product_id"]),
        "productName": row["product_name"],
        "name": row["name"],
        "casNumber": row["cas_number"],
        "amountPerServing": None if amount is None else float(amount),
        "amountUnit": row["amount_unit"],
        "isActive": bool(row["is_active"]),
        "status": row["status"],
        "riskLevel": row["risk_level"],
        "statusNote": row["status_note"],
        "evaluatedAt": _iso(row["evaluated_at"]),
    }


def claim(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "productId": str(row["product_id"]),
        "productName": row["product_name"],
        "text": row["text"],
        "claimType": row["claim_type"],
        "targetMarket": row["target_market"],
        "status": row["status"],
        "riskLevel": row["risk_level"],
        "disclaimer": row["disclaimer"],
        "evidenceCount": _int(row["evidence_count"]),
        "statusNote": row["status_note"],
        "lastReviewedAt": _iso(row["last_reviewed_at"]),
    }


def document(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "productId": None if row["product_id"] is None else str(row["product_id"]),
        "productName": row["product_name"],
        "fileName": row["file_name"],
        "documentType": row["document_type"],
        "sizeBytes": None if row["size_bytes"] is None else int(row["size_bytes"]),
        "status": row["status"],
        "extractionStatus": row["extraction_status"],
        "uploadedAt": _iso(row["uploaded_at"]),
        "failureReason": row["failure_reason"],
    }


def finding(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "analysisId": str(row["analysis_id"]),
        "productId": str(row["product_id"]),
        "productName": row["product_name"],
        "title": row["title"],
        "summary": row["summary"],
        "severity": row["severity"],
        "status": row["status"],
        "category": row["category"],
        "statutoryReference": row["statutory_reference"],
        "correctiveAction": row["corrective_action"],
        "detectedAt": _iso(row["detected_at"]),
    }


def evidence(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "findingId": None if row["finding_id"] is None else str(row["finding_id"]),
        "sourceId": str(row["source_id"]),
        "authority": row["authority"],
        "sourceTitle": row["source_title"],
        "documentTitle": row["document_title"],
        "section": row["section_number"],
        "page": None if row["page"] is None else int(row["page"]),
        "version": row["version"],
        "effectiveDate": _iso(row["effective_date"]),
        "excerpt": row["excerpt"],
        "status": row["status"],
        "sourceUrl": row["source_url"],
    }


def review_task(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "findingId": str(row["finding_id"]),
        "productId": str(row["product_id"]),
        "productName": row["product_name"],
        "title": row["title"],
        "severity": row["severity"],
        "status": row["status"],
        "assigneeId": None if row["assignee_id"] is None else str(row["assignee_id"]),
        "assigneeName": row["assignee_name"],
        "requestedAt": _iso(row["requested_at"]),
        "decidedAt": _iso(row["decided_at"]),
        "decisionNote": row["decision_note"],
    }


def compliance_item(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "productId": str(row["product_id"]),
        "label": row["label"],
        "note": row["note"],
        # Tri-state: NULL means "not yet determined" and must survive as null.
        "passed": None if row["passed"] is None else bool(row["passed"]),
        "category": row["category"],
    }


def report(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "productId": str(row["product_id"]),
        "productName": row["product_name"],
        "title": row["title"],
        "status": row["status"],
        "generatedAt": _iso(row["generated_at"]),
        "downloadUrl": f"/api/v1/reports/{row['id']}/download" if row["status"] == "READY" else None,
        "failureReason": row["failure_reason"],
    }


def analysis(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "productId": str(row["product_id"]),
        "status": row["status"],
        "progress": _int(row["progress"]),
        "startedAt": _iso(row["started_at"]),
        "completedAt": _iso(row["completed_at"]),
        "failureReason": row["failure_reason"],
    }


def statutory_source(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "authority": row["authority"],
        "title": row["title"],
        "documentTitle": row["document_title"],
        "sourceType": row["source_type"],
        "jurisdiction": row["jurisdiction"],
        "version": row["version"],
        "effectiveDate": _iso(row["effective_date"]),
        "lastIndexedAt": _iso(row["last_indexed_at"]),
        "sourceUrl": row["source_url"],
        "citationCount": _int(row["citation_count"]),
    }


def dashboard_summary(row: Row) -> dict[str, Any]:
    return {
        "productCount": _int(row["product_count"]),
        "activeAnalysisCount": _int(row["active_analysis_count"]),
        "highRiskCount": _int(row["high_risk_count"]),
        "pendingReviewCount": _int(row["pending_review_count"]),
        "readiness": {
            "ready": _int(row["readiness_ready"]),
            "reviewRequired": _int(row["readiness_review_required"]),
            "missingInformation": _int(row["readiness_missing_information"]),
        },
    }


def session_user(row: Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "displayName": row["display_name"],
        "email": row["email"],
        "organizationName": row["organization_name"],
        "role": row["role"],
    }
