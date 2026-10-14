"""Document vault: upload, list, delete.

Uploaded files are written to a local directory and referenced by
``documents.storage_key``. Binary content never enters PostgreSQL, matching the
schema design. The size ceiling is enforced here and again by a CHECK
constraint, so an oversized file is rejected even if it bypasses this layer.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import Response as FastAPIResponse

from app.api import db, serializers
from app.api.auth import require_user
from app.api.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["documents"])

# Mirrors MAX_UPLOAD_BYTES in src/lib/api/client.ts and the
# documents_size_bytes_check constraint.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_ACCEPTED = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_DOCUMENT_TYPES = {
    "PRODUCT_SPEC",
    "CERTIFICATE_OF_ANALYSIS",
    "LAB_REPORT",
    "SAFETY_SHEET",
    "LABEL_ARTWORK",
    "OTHER",
}


@router.get("/documents")
async def list_documents(
    product_id: Optional[str] = None,
    _: Any = Depends(require_user),
) -> list[dict[str, Any]]:
    """The document vault. ``product_id`` filters to one dossier when supplied."""
    if product_id:
        rows = await db.fetch_all(
            "SELECT * FROM v_document_list WHERE product_id = $1 ORDER BY uploaded_at DESC",
            product_id,
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM v_document_list ORDER BY uploaded_at DESC"
        )
    return [serializers.document(row) for row in rows]


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    productId: Optional[str] = Form(default=None),
    documentType: Optional[str] = Form(default=None),
    _: Any = Depends(require_user),
) -> dict[str, Any]:
    """Store an uploaded file and register it in the vault.

    ``status`` is UPLOADED and ``extraction_status`` is PENDING. Text extraction
    of uploads is not part of this stage, so the extraction row stays PENDING
    rather than being reported as complete.
    """
    original_name = os.path.basename(file.filename or "upload")
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in _ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported file type. Upload a PDF, DOCX, XLSX, PNG, or JPG file.",
        )
    if documentType is not None and documentType not in _DOCUMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"documentType must be one of {sorted(_DOCUMENT_TYPES)}.",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The selected file is empty.",
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.",
        )

    # A random storage key means an uploaded name can never overwrite another
    # file or escape the upload directory.
    storage_key = f"{uuid.uuid4().hex}{extension}"
    settings = get_settings()
    os.makedirs(settings.upload_dir, exist_ok=True)
    destination = os.path.join(settings.upload_dir, storage_key)
    with open(destination, "wb") as handle:
        handle.write(payload)

    row = await db.fetch_one(
        """
        INSERT INTO documents
            (product_id, file_name, document_type, storage_key, size_bytes,
             status, extraction_status)
        VALUES ($1, $2, $3, $4, $5, 'UPLOADED', 'PENDING')
        RETURNING id
        """,
        productId or None,
        original_name,
        documentType,
        storage_key,
        len(payload),
    )
    await db.execute(
        "INSERT INTO document_extractions (document_id, extraction_status) "
        "VALUES ($1, 'PENDING')",
        row["id"],
    )

    record = await db.fetch_one("SELECT * FROM v_document_list WHERE id = $1", row["id"])
    return serializers.document(record)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=FastAPIResponse,
)
async def delete_document(
    document_id: str, _: Any = Depends(require_user)
) -> FastAPIResponse:
    """Remove a document and the file it points at."""
    row = await db.fetch_one(
        "SELECT id, storage_key FROM documents WHERE id = $1", document_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found."
        )
    await db.execute("DELETE FROM documents WHERE id = $1", document_id)

    path = os.path.join(get_settings().upload_dir, row["storage_key"])
    # storage_key is server-generated, but resolve and check anyway so a
    # corrupted row can never delete a file outside the upload directory.
    if os.path.abspath(path).startswith(os.path.abspath(get_settings().upload_dir)):
        if os.path.exists(path):
            os.remove(path)

    return FastAPIResponse(status_code=status.HTTP_204_NO_CONTENT)
