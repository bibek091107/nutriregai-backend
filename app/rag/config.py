"""Central configuration for the regulatory preprocessing pipeline.

Every path is derived from the backend package location, never from the current
working directory, so the pipeline behaves identically regardless of where it is
invoked from.

No secret is read, stored or referenced here. ``GROQ_API_KEY`` is intentionally
absent: this stage does not call Groq, and the key must never appear in a
report, a log line, or a chunk.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# app/rag/config.py -> app/rag -> app -> backend
BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent

KNOWLEDGE_ROOT = BACKEND_ROOT / "regulatory_knowledge" / "india_fssai"
METADATA_DIR = KNOWLEDGE_ROOT / "metadata"
PROCESSED_DIR = KNOWLEDGE_ROOT / "processed"

DOCUMENTS_DIR = PROCESSED_DIR / "documents"
PAGES_DIR = PROCESSED_DIR / "pages"
CHUNKS_DIR = PROCESSED_DIR / "chunks"
REPORTS_DIR = PROCESSED_DIR / "reports"

# Input directories. QUARANTINED_DIRNAME is never descended into.
QUARANTINED_DIRNAME = "quarantine_html"
CATEGORY_DIRNAMES = (
    "regulations",
    "notifications",
    "guidance",
    "health_supplements",
    "compendiums",
    "packaging",
    "product_standards",
    "core_regulations",
)

# Inputs and outputs under metadata/
MANIFEST_PATH = METADATA_DIR / "manifest.json"
PENDING_SOURCES_PATH = METADATA_DIR / "pending_sources.json"
MANIFEST_CLEAN_PATH = METADATA_DIR / "manifest_clean.json"
MANIFEST_VALIDATION_REPORT_PATH = METADATA_DIR / "manifest_validation_report.json"
PDF_VALIDATION_REPORT_PATH = METADATA_DIR / "validation_report.json"
EXTRACTION_REPORT_JSON_PATH = METADATA_DIR / "extraction_report.json"
EXTRACTION_REPORT_MD_PATH = METADATA_DIR / "extraction_report.md"

SAMPLE_CHUNKS_PATH = REPORTS_DIR / "sample_chunks.json"

# --------------------------------------------------------------------------
# Collection constants
# --------------------------------------------------------------------------
AUTHORITY = "FSSAI"
AUTHORITY_FULL_NAME = "Food Safety and Standards Authority of India"
JURISDICTION = "INDIA_FSSAI"

# The six categories the source collection is organised into. Used only for
# reporting and for spreading the sample-chunk selection across categories; it
# never overrides a category that the manifest actually provides.
CATEGORIES = (
    "General Regulatory Framework",
    "Food Products Standards and Food Additives",
    "Labelling and Display",
    "Advertising and Claims",
    "Contaminants, Toxins and Residues",
    "Health Supplements / Nutraceuticals / Functional Foods / Novel Food",
)

# --------------------------------------------------------------------------
# PDF validation
# --------------------------------------------------------------------------
PDF_MAGIC = b"%PDF-"
# Enough leading bytes to catch an HTML document served in place of a PDF.
SNIFF_BYTES = 4096
HTML_MARKERS = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<?php",
)
MIN_PLAUSIBLE_PDF_BYTES = 32

# --------------------------------------------------------------------------
# OCR candidate detection (Phase 6)
# --------------------------------------------------------------------------
# Below this many characters of extracted text a page is an OCR candidate.
OCR_MIN_TEXT_CHARS = 20
# If images cover at least this fraction of the page area, the page is very
# likely a scan even when a stray header/footer produced a few characters.
OCR_IMAGE_AREA_RATIO = 0.50
# A page is also an OCR candidate when it has many embedded images but almost
# no text, e.g. a gazette scan with a text watermark.
OCR_IMAGE_COUNT_WITHOUT_TEXT = 1

# --------------------------------------------------------------------------
# Chunking targets (Phase 8)
# --------------------------------------------------------------------------
# Tokens, not words. Regulatory targets are expressed in tokens because that is
# what an embedding model will consume later.
CHUNK_TARGET_TOKENS = 1200
CHUNK_MIN_TOKENS = 200
CHUNK_MAX_TOKENS = 1500
CHUNK_OVERLAP_TOKENS = 150

# A line longer than this is body text, not a heading.
MAX_HEADING_CHARS = 140
# A heading shorter than this is more likely a page number or stray glyph.
MIN_HEADING_CHARS = 3

# --------------------------------------------------------------------------
# Manifest validation (Phase 4)
# --------------------------------------------------------------------------
REQUIRED_MANIFEST_FIELDS = (
    "authority",
    "jurisdiction",
    "title",
    "source_type",
    "category",
    "local_path",
    "source_url",
    "document_url",
    "version",
    "effective_date",
    "retrieved_at",
)
URL_FIELDS = ("source_url", "document_url", "sourceUrl", "documentUrl")
ALLOWED_URL_SCHEMES = ("http", "https")


# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------
# Token counting is a local, deterministic approximation rather than tiktoken,
# for two reasons: tiktoken downloads BPE tables on first use (a network
# dependency in an offline preprocessing step), and a model-specific tokeniser
# would make chunk boundaries change if the embedding model changed later.
#
# The estimator splits on word characters and counts each word plus each
# standalone punctuation mark. For English regulatory prose this lands within
# roughly 10-15% of cl100k_base, which is well inside the 800-1500 target band
# and, crucially, is reproducible on any machine with no downloads.
TOKEN_RATIO = 1.0


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` deterministically."""
    if not text:
        return 0
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def configure_logging(verbose: bool = False) -> None:
    """Structured, human-readable logging on stderr.

    stdout is reserved for command output and report paths, so a caller can
    pipe results without log noise interleaved.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # These are chatty and unrelated to this pipeline.
    logging.getLogger("pymupdf").setLevel(logging.WARNING)
    logging.getLogger("pymupdf_persistent").setLevel(logging.WARNING)


def ensure_output_dirs() -> None:
    """Create the processed output tree. Safe to call repeatedly."""
    for directory in (METADATA_DIR, DOCUMENTS_DIR, PAGES_DIR, CHUNKS_DIR, REPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def is_quarantined(path: Path) -> bool:
    """True if ``path`` lives inside the quarantine tree.

    Used as a hard guard everywhere a file list is built, so quarantined files
    can never reach extraction even if a glob is wrong.
    """
    try:
        parts = path.resolve().parts
    except OSError:
        parts = path.parts
    return QUARANTINED_DIRNAME in parts


def knowledge_relative(path: Path) -> str:
    """Path relative to the backend root, using forward slashes.

    This is the form stored in the manifest and in every chunk, so chunk
    provenance is comparable across machines.
    """
    try:
        rel = path.resolve().relative_to(BACKEND_ROOT.resolve())
    except ValueError:
        rel = path
    return rel.as_posix()


def resolve_local_path(local_path: str | None) -> Path | None:
    """Resolve a manifest ``local_path`` to an absolute path.

    The manifest stores paths relative to the backend root. Absolute paths are
    accepted too, so a hand-edited manifest does not break resolution.
    """
    if not local_path:
        return None
    candidate = Path(local_path)
    if candidate.is_absolute():
        return candidate
    return (BACKEND_ROOT / candidate).resolve()


def load_env() -> None:
    """Load backend/.env if present, without ever reading secrets into logs.

    The pipeline does not need any environment variable. This exists so the
    module import stays side-effect free of configuration, and so a future
    stage can rely on ``.env`` being loaded in the same way everywhere.
    """
    env_path = BACKEND_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:  # pragma: no cover - dotenv is optional at runtime
        # A malformed .env must never break preprocessing. Nothing in this
        # stage reads configuration, so silently continuing is correct here.
        pass


os.environ.setdefault("PYTHONHASHSEED", "0")
load_env()
