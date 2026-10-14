"""Typed records for every stage of the pipeline.

Pydantic is used so a malformed record fails at the boundary with a precise
error, rather than surfacing three stages later as a confusing key error.

Two invariants are enforced by the models themselves because they are cheap to
check and expensive to get wrong:

* a page number is >= 1, and ``page_end >= page_start``
* a chunk's text is non-empty
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Enumerations mirroring the contract vocabulary
# --------------------------------------------------------------------------
ValidationStatus = Literal[
    "VALID",
    "HTML_DISGUISED_AS_PDF",
    "ZERO_BYTE",
    "UNREADABLE",
    "TOO_SMALL",
    "NOT_A_PDF",
    "ENCRYPTED",
    "ERROR",
]

FileType = Literal["PDF", "HTML", "TEXT", "BINARY", "EMPTY", "UNKNOWN"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------
# Phase 3 — PDF validation
# --------------------------------------------------------------------------
class PdfFileRecord(_Base):
    """One discovered ``.pdf`` file and its validation verdict."""

    local_path: str
    status: ValidationStatus
    file_type: FileType
    size_bytes: int = 0
    sha256: Optional[str] = None
    page_count: Optional[int] = None
    is_quarantined: bool = False
    category: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Phase 4 — manifest
# --------------------------------------------------------------------------
class ManifestEntry(_Base):
    """One manifest record. Unknown keys are preserved, never dropped."""

    authority: Optional[str] = None
    jurisdiction: Optional[str] = None
    title: Optional[str] = None
    source_type: Optional[str] = None
    category: Optional[str] = None
    local_path: Optional[str] = None
    source_url: Optional[str] = None
    document_url: Optional[str] = None
    version: Optional[str] = None
    effective_date: Optional[str] = None
    retrieved_at: Optional[str] = None

    @field_validator(
        "authority", "jurisdiction", "title", "source_type", "category",
        "local_path", "source_url", "document_url", "version",
        "effective_date", "retrieved_at",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """An empty string is treated as absent, not as a value.

        This distinguishes "we do not know the version" from "the version is
        the empty string", which is what the contract means by ``null``.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if stripped == "":
                return None
            return stripped
        return value


# --------------------------------------------------------------------------
# Phase 5/6 — page-level extraction
# --------------------------------------------------------------------------
class PageRecord(_Base):
    """A single extracted page.

    ``document_id`` and ``page_number`` plus ``text`` is the internal
    representation the pipeline works in. The remaining fields carry the OCR
    verdict and extraction diagnostics; a page with empty text is retained with
    ``needs_ocr`` set rather than being discarded.
    """

    document_id: str
    page_number: int = Field(ge=1)
    text: str = ""
    text_length: int = 0
    char_count: int = 0
    word_count: int = 0
    needs_ocr: bool = False
    ocr_reasons: list[str] = Field(default_factory=list)
    image_count: int = 0
    image_area_ratio: float = 0.0
    extraction_error: Optional[str] = None

    @field_validator("page_number")
    @classmethod
    def _page_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("page_number is 1-based and must be >= 1")
        return value


# --------------------------------------------------------------------------
# Phase 7/8 — sections and chunks
# --------------------------------------------------------------------------
class DetectedSection(_Base):
    """A detected section heading.

    ``section`` is ``None`` whenever the source text did not contain a reliable
    section marker. It is never inferred, completed or guessed.
    """

    section: Optional[str] = None
    subsection: Optional[str] = None
    heading_text: Optional[str] = None
    heading_kind: Optional[str] = None  # CHAPTER | SCHEDULE | NUMBERED | ...
    page_number: int = Field(ge=1)
    confidence: Literal["high", "medium", "low"] = "low"


class ChunkRecord(_Base):
    """A section-aware chunk with full regulatory provenance.

    Field order follows the Phase 9 specification. Every provenance field is
    nullable and populated strictly from the manifest; nothing is inferred.
    """

    chunk_id: str
    document_id: str
    authority: Optional[str] = None
    jurisdiction: Optional[str] = None
    document_title: Optional[str] = None
    source_type: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None
    effective_date: Optional[str] = None
    source_url: Optional[str] = None
    document_url: Optional[str] = None
    section: Optional[str] = None
    subsection: Optional[str] = None
    heading_text: Optional[str] = None
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    text: str
    local_path: str
    source_sha256: Optional[str] = None
    token_estimate: int = 0
    char_count: int = 0
    word_count: int = 0
    overlaps_previous: bool = False
    ocr_page_numbers: list[int] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("chunk text must not be empty or whitespace-only")
        return value

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id_present(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("chunk_id is required")
        return value

    def validate_page_order(self) -> "ChunkRecord":
        if self.page_end < self.page_start:
            raise ValueError(
                f"page_end ({self.page_end}) must be >= page_start ({self.page_start})"
            )
        return self


# --------------------------------------------------------------------------
# Phase 11/12 — document-level record
# --------------------------------------------------------------------------
class DocumentRecord(_Base):
    """Per-document record written to ``processed/documents/<document_id>.json``."""

    document_id: str
    local_path: str
    title: Optional[str] = None
    authority: Optional[str] = None
    jurisdiction: Optional[str] = None
    source_type: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None
    effective_date: Optional[str] = None
    retrieved_at: Optional[str] = None
    source_url: Optional[str] = None
    document_url: Optional[str] = None

    source_sha256: Optional[str] = None
    file_size: Optional[int] = None
    page_count: Optional[int] = None
    extracted_at: Optional[str] = None

    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    pages_with_text: int = 0
    pages_needing_ocr: int = 0
    section_count: int = 0
    chunk_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None
