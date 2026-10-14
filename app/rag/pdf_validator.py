"""Phase 3 — validate every discovered PDF.

A file is only treated as a regulatory PDF if its *bytes* say so. Filename
extension is never sufficient: this collection has previously contained HTML
error pages saved with a ``.pdf`` extension, and treating one as a regulation
would put a web page into the knowledge base as a primary source.

The quarantine tree is never read for content. Its files are counted for the
report and otherwise ignored.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .ids import sha256_file
from .models import PdfFileRecord

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def _is_pdf_name(path: Path) -> bool:
    """Extension test, case-insensitively.

    ``*.PDF`` and ``*.pdf`` are both collected. Several files in this
    collection end in ``.pdf.pdf``; that is a duplicate-looking name but still
    one document, so it is processed once under its real filename.
    """
    return path.suffix.lower() == ".pdf"


def discover_pdfs(knowledge_root: Path | None = None) -> tuple[list[Path], list[Path]]:
    """Return ``(active_pdfs, quarantined_pdfs)``.

    Quarantined files are listed so the report can state how many exist, but
    they are never returned for processing and never opened.
    """
    root = knowledge_root or config.KNOWLEDGE_ROOT
    active: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        # Prune the quarantine subtree at the parent, so nothing inside it can
        # ever be reached by the traversal below.
        dirnames[:] = [d for d in dirnames if d != config.QUARANTINED_DIRNAME]
        for filename in filenames:
            path = current / filename
            if _is_pdf_name(path) and not config.is_quarantined(path):
                active.append(path)

    active.sort(key=lambda p: config.knowledge_relative(p))
    quarantined = list_quarantined_pdfs(root)
    return active, quarantined


def list_quarantined_pdfs(knowledge_root: Path | None = None) -> list[Path]:
    """Enumerate quarantined ``.pdf`` files by name only.

    Deliberately separate from :func:`discover_pdfs`. This function touches no
    file content — it uses ``scandir`` and reads names only — so it cannot
    accidentally open, hash or extract a quarantined document. The count is
    still reported, because "14 files are excluded" is a material fact for
    anyone auditing coverage.
    """
    root = knowledge_root or config.KNOWLEDGE_ROOT
    quarantine_dir = root / config.QUARANTINED_DIRNAME
    if not quarantine_dir.is_dir():
        return []

    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(quarantine_dir):
        for filename in filenames:
            path = Path(dirpath) / filename
            if _is_pdf_name(path):
                found.append(path)
    found.sort(key=lambda p: config.knowledge_relative(p))
    return found


def category_for(path: Path, knowledge_root: Path | None = None) -> str | None:
    """Directory name relative to the knowledge root, if it is a category dir."""
    root = knowledge_root or config.KNOWLEDGE_ROOT
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if len(rel.parts) < 2:
        return None
    parent = rel.parts[-2]
    return parent if parent in config.CATEGORY_DIRNAMES else None


# --------------------------------------------------------------------------
# Content-type sniffing
# --------------------------------------------------------------------------
def sniff_file_type(head: bytes) -> tuple[str, bool]:
    """Identify a file from its leading bytes.

    Returns ``(file_type, is_html)``. HTML is checked first and against the
    head only: a valid PDF may legitimately contain ``<html`` later on, inside
    an embedded or compressed stream, which must not cause a false positive.
    """
    if not head:
        return "EMPTY", False

    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = stripped[: config.SNIFF_BYTES].lower()
    for marker in config.HTML_MARKERS:
        if lowered.startswith(marker):
            return "HTML", True

    if stripped.startswith(config.PDF_MAGIC):
        return "PDF", False

    # A PDF whose header is not at offset 0 is technically malformed but is
    # still recoverable by every real reader. Classify by type, flag separately.
    if config.PDF_MAGIC in head[:1024]:
        return "PDF", False

    if stripped.startswith((b"{", b"[")):
        return "TEXT", False
    if b"\x00" in head[:1024]:
        return "BINARY", False
    # Heuristic only: XML, plain text, error pages.
    try:
        head[:1024].decode("utf-8")
    except UnicodeDecodeError:
        return "BINARY", False
    return "TEXT", False


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_pdf(path: Path, knowledge_root: Path | None = None) -> PdfFileRecord:
    """Validate one file. Never raises: a failure is recorded on the record."""
    local_path = config.knowledge_relative(path)
    warnings: list[str] = []

    try:
        size = path.stat().st_size
    except OSError as exc:
        return PdfFileRecord(
            local_path=local_path,
            status="ERROR",
            file_type="UNKNOWN",
            error=f"cannot stat file: {exc}",
        )

    record = PdfFileRecord(
        local_path=local_path,
        status="VALID",
        file_type="UNKNOWN",
        size_bytes=size,
        category=category_for(path, knowledge_root),
    )

    # --- zero byte -----------------------------------------------------
    if size == 0:
        record.status = "ZERO_BYTE"
        record.file_type = "EMPTY"
        return record

    if size < config.MIN_PLAUSIBLE_PDF_BYTES:
        record.status = "TOO_SMALL"
        record.error = f"file is {size} bytes, below the plausible minimum"
        return record

    # --- content type --------------------------------------------------
    try:
        with open(path, "rb") as handle:
            head = handle.read(config.SNIFF_BYTES)
    except OSError as exc:
        record.status = "ERROR"
        record.error = f"cannot read file: {exc}"
        return record

    file_type, is_html = sniff_file_type(head)
    record.file_type = file_type

    if is_html:
        record.status = "HTML_DISGUISED_AS_PDF"
        record.error = "file begins with HTML markup, not a PDF header"
        return record

    if file_type != "PDF":
        record.status = "NOT_A_PDF"
        record.error = f"no %PDF- signature found; leading bytes look like {file_type}"
        return record

    if not head.startswith(config.PDF_MAGIC):
        warnings.append("%PDF- header is not at offset 0")

    # --- hash ----------------------------------------------------------
    # Hashed for every candidate, including invalid ones, so a file that
    # changes state between runs is always traceable.
    try:
        record.sha256 = sha256_file(path)
    except OSError as exc:
        record.status = "ERROR"
        record.error = f"cannot hash file: {exc}"
        return record

    # --- open with a real PDF engine -----------------------------------
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - dependency is declared
        record.status = "ERROR"
        record.error = "PyMuPDF is not installed"
        return record

    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                record.status = "ENCRYPTED"
                record.error = "PDF is password protected"
                return record
            record.page_count = document.page_count
            if document.page_count == 0:
                record.status = "UNREADABLE"
                record.error = "PDF opened but reports zero pages"
                return record
    except Exception as exc:
        record.status = "UNREADABLE"
        record.error = f"{type(exc).__name__}: {exc}"
        return record

    record.status = "VALID"
    record.warnings = warnings
    return record


def validate_collection(knowledge_root: Path | None = None) -> dict:
    """Validate the whole collection and return the report payload."""
    root = knowledge_root or config.KNOWLEDGE_ROOT
    if not root.is_dir():
        raise FileNotFoundError(f"knowledge root not found: {root}")

    active, quarantined = discover_pdfs(root)
    log.info("discovered %d active .pdf files, %d quarantined", len(active), len(quarantined))

    records: list[PdfFileRecord] = []
    for index, path in enumerate(active, start=1):
        if config.is_quarantined(path):  # defence in depth
            log.error("refusing to validate quarantined file %s", path)
            continue
        record = validate_pdf(path, root)
        records.append(record)
        if index % 50 == 0:
            log.info("  validated %d/%d", index, len(active))

    valid = [r for r in records if r.status == "VALID"]
    by_status: dict[str, int] = {}
    for record in records:
        by_status[record.status] = by_status.get(record.status, 0) + 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_root": config.knowledge_relative(root),
        "authority": config.AUTHORITY,
        "jurisdiction": config.JURISDICTION,
        "total_pdf_files": len(records),
        "valid_pdf_files": len(valid),
        "invalid_pdf_files": len(records) - len(valid),
        "html_disguised_as_pdf": by_status.get("HTML_DISGUISED_AS_PDF", 0),
        "zero_byte_files": by_status.get("ZERO_BYTE", 0),
        "unreadable_files": sum(
            by_status.get(status, 0)
            for status in ("UNREADABLE", "ENCRYPTED", "ERROR", "TOO_SMALL", "NOT_A_PDF")
        ),
        "quarantined_files": len(quarantined),
        "quarantined_paths": [config.knowledge_relative(p) for p in quarantined],
        "status_breakdown": by_status,
        "total_pages": sum(r.page_count or 0 for r in valid),
        "files": [r.model_dump() for r in records],
        "errors": [
            {"local_path": r.local_path, "status": r.status, "error": r.error}
            for r in records
            if r.status != "VALID"
        ],
    }
    return report


def write_validation_report(report: dict, path: Path | None = None) -> Path:
    target = path or config.PDF_VALIDATION_REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote validation report -> %s", config.knowledge_relative(target))
    return target
