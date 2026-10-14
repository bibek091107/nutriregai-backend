"""Phase 16 and 17 — the command-line pipeline.

    python -m app.rag.pipeline validate
    python -m app.rag.pipeline extract
    python -m app.rag.pipeline chunk
    python -m app.rag.pipeline all

Error isolation contract
------------------------
A failure while handling one document is captured, counted and reported, and
the run continues with the next document. One corrupt PDF cannot end a run over
200 documents. A document that fails leaves no partial ``.jsonl`` behind, so a
later stage can never read half a document as if it were whole.

Idempotence
-----------
Each document record stores the SHA-256 of its source. A rerun skips a document
whose source is unchanged and whose outputs already exist, so ``all`` can be
re-run cheaply after new PDFs arrive without reprocessing the other 199. Use
``--force`` to reprocess everything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import config, manifest, parser, pdf_validator, reports
from .chunker import chunk_document
from .ids import sha256_file

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write JSONL atomically.

    Written to a temporary file and renamed, so a crash mid-write cannot leave
    a truncated file that a later stage would read as complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_bindings() -> tuple[list[dict], dict]:
    """Load the clean manifest and bind it to files on disk."""
    clean = manifest.validate_manifest()
    bindings, collisions = manifest.build_document_bindings(clean["clean_entries"])
    return bindings, collisions


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------
def run_validate() -> list[dict]:
    """Validate PDFs and the manifest; return the clean entries for reuse."""
    config.ensure_output_dirs()

    log.info("=== stage 1/2: validating PDFs ===")
    pdf_report = pdf_validator.validate_collection()
    pdf_validator.write_validation_report(pdf_report)
    print(
        f"PDF validation: {pdf_report['valid_pdf_files']}/{pdf_report['total_pdf_files']} valid, "
        f"{pdf_report['invalid_pdf_files']} invalid, "
        f"{pdf_report['quarantined_files']} quarantined and excluded, "
        f"{pdf_report['total_pages']} pages"
    )

    log.info("=== stage 2/2: validating manifest ===")
    clean = manifest.validate_manifest()
    manifest.write_manifest_clean(clean)
    manifest.write_manifest_validation_report(clean)
    bindings, collisions = manifest.build_document_bindings(clean["clean_entries"])
    print(
        f"Manifest: {clean['original_entry_count']} entries -> "
        f"{clean['clean_entry_count']} clean "
        f"({clean['dropped_entry_count']} dropped, "
        f"{clean['ready_for_processing']} ready, "
        f"{clean['pending_download']} pending download), "
        f"{clean['urls_normalized']} URLs normalized, "
        f"{collisions['files_with_multiple_manifest_claimants']} filename collisions"
    )
    print(f"  -> {config.knowledge_relative(config.MANIFEST_CLEAN_PATH)}")
    print(f"  -> {config.knowledge_relative(config.MANIFEST_VALIDATION_REPORT_PATH)}")
    print(f"  -> {config.knowledge_relative(config.PDF_VALIDATION_REPORT_PATH)}")
    return clean["clean_entries"]


def _process_document(
    binding: dict,
    want_pages: bool,
    want_chunks: bool,
    force: bool,
) -> dict:
    """Parse, and optionally emit pages and chunks, for one document.

    Parsing happens once and feeds both outputs. Never raises.
    """
    document_id = binding["document_id"]
    local_path = binding["local_path"]
    absolute = Path(binding["absolute_path"])
    entry = binding["manifest_entry"]

    document_file = config.DOCUMENTS_DIR / f"{document_id}.json"
    pages_file = config.PAGES_DIR / f"{document_id}.jsonl"
    chunks_file = config.CHUNKS_DIR / f"{document_id}.jsonl"

    try:
        source_sha = sha256_file(absolute)
    except OSError as exc:
        return {
            "document_id": document_id,
            "local_path": local_path,
            "title": entry.get("title"),
            "status": "FAILED",
            "error": f"cannot hash source: {exc}",
        }

    if not force and document_file.is_file():
        try:
            previous = json.loads(document_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous = {}
        if previous.get("source_sha256") == source_sha:
            needs_pages = want_pages and not pages_file.is_file()
            needs_chunks = want_chunks and not chunks_file.is_file()
            if not needs_pages and not needs_chunks:
                previous["_resumed"] = True
                return previous

    if config.is_quarantined(absolute):
        return {
            "document_id": document_id,
            "local_path": local_path,
            "title": entry.get("title"),
            "status": "FAILED",
            "error": "refused: path is inside the quarantine tree",
        }

    started = time.time()
    result = parser.parse_document(absolute, document_id, local_path)
    if not result.ok:
        return {
            "document_id": document_id,
            "local_path": local_path,
            "title": entry.get("title"),
            "source_type": entry.get("source_type"),
            "category": entry.get("category"),
            "source_sha256": source_sha,
            "status": "FAILED",
            "error": result.error,
        }

    ocr_pages = set(result.pages_needing_ocr)
    reason_counts = Counter()
    for page in result.pages:
        for reason in page.ocr_reasons:
            reason_counts[reason.split(":")[0]] += 1

    if want_pages:
        _write_jsonl(pages_file, parser.pages_to_jsonl_records(result.pages))

    chunks = []
    sections = []
    chunk_error: str | None = None
    if want_chunks:
        try:
            chunks, sections = chunk_document(
                document_id=document_id,
                lines_by_page=result.lines_by_page,
                manifest_entry=entry,
                local_path=local_path,
                source_sha256=source_sha,
                ocr_pages=ocr_pages,
            )
            _write_jsonl(chunks_file, [c.model_dump() for c in chunks])
        except Exception as exc:
            # A chunking failure must not discard a successful extraction.
            chunk_error = f"{type(exc).__name__}: {exc}"
            log.error("chunking failed for %s: %s", local_path, chunk_error)
            log.debug(traceback.format_exc())

    record = {
        "document_id": document_id,
        "local_path": local_path,
        "title": entry.get("title"),
        "authority": entry.get("authority"),
        "jurisdiction": entry.get("jurisdiction"),
        "source_type": entry.get("source_type"),
        "category": entry.get("category"),
        "version": entry.get("version"),
        "effective_date": entry.get("effective_date"),
        "retrieved_at": entry.get("retrieved_at"),
        "source_url": entry.get("source_url"),
        "document_url": entry.get("document_url"),
        "source_sha256": source_sha,
        "file_size": absolute.stat().st_size,
        "page_count": result.page_count,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS" if chunk_error is None else "PARTIAL",
        "pages_with_text": sum(1 for p in result.pages if not p.needs_ocr),
        "pages_needing_ocr": len(ocr_pages),
        "ocr_reason_counts": dict(reason_counts),
        "section_count": len(sections),
        "chunks_with_section": sum(1 for c in chunks if c.section is not None),
        "chunk_count": len(chunks),
        "chunk_token_counts": [c.token_estimate for c in chunks],
        "alias_claimants": binding.get("alias_claimants", []),
        "has_manifest_collision": binding.get("has_collision", False),
        "warnings": result.warnings,
        "error": chunk_error,
        "duration_seconds": round(time.time() - started, 3),
        "outputs": {
            "document": config.knowledge_relative(document_file),
            "pages": config.knowledge_relative(pages_file) if want_pages else None,
            "chunks": config.knowledge_relative(chunks_file) if want_chunks else None,
        },
    }
    _write_json(document_file, record)
    return record


def _run_processing(
    want_pages: bool,
    want_chunks: bool,
    force: bool,
    limit: int | None,
    only: str | None,
    clean_entries: list[dict] | None = None,
) -> int:
    config.ensure_output_dirs()
    if clean_entries is None:
        clean_entries = manifest.validate_manifest()["clean_entries"]
    bindings, collisions = manifest.build_document_bindings(clean_entries)

    if only:
        bindings = [b for b in bindings if only.lower() in b["local_path"].lower()]
    if limit:
        bindings = bindings[:limit]

    log.info("processing %d documents", len(bindings))
    records: list[dict] = []
    failures: list[dict] = []

    for index, binding in enumerate(bindings, start=1):
        record = _process_document(binding, want_pages, want_chunks, force)
        records.append(record)
        if record.get("status") == "FAILED":
            failures.append(record)
        elif record.get("status") == "PARTIAL":
            log.warning(
                "partial: %s (%s)", record["local_path"], record.get("error")
            )
        if index % 10 == 0 or index == len(bindings):
            ok = sum(1 for r in records if r.get("status") == "SUCCESS")
            log.info("  %d/%d processed (%d ok, %d failed)", index, len(bindings), ok, len(failures))

    resumed = sum(1 for r in records if r.get("_resumed"))
    summary = reports.summarise(records)
    duplicates = reports.find_duplicate_sources(records)
    failures = [r for r in records if r.get("status") != "SUCCESS"]

    reports.write_extraction_report_json(summary, records, collisions, failures, duplicates)
    reports.write_extraction_report_md(summary, records, collisions, failures, duplicates)
    if want_chunks:
        reports.write_sample_chunks(records)

    tokens = summary["chunk_token_stats"]
    print()
    print(f"documents   : {summary['documents_total']} "
          f"({summary['documents_succeeded']} ok, {summary['documents_failed']} failed, "
          f"{resumed} resumed unchanged)")
    print(f"pages       : {summary['pages_total']} "
          f"({summary['pages_with_text']} usable, {summary['pages_needing_ocr']} flagged for OCR)")
    print(f"sections    : {summary['sections_detected']}")
    print(f"chunks      : {summary['chunks_total']} "
          f"(p50 {tokens['p50']} tokens, max {tokens['max']}, "
          f"{tokens['over_target_max']} over max)")
    if collisions.get("collisions"):
        print(f"collisions  : {collisions['files_with_multiple_manifest_claimants']} files, "
              f"{collisions['extra_claimants_suppressed']} aliases suppressed")
    if duplicates:
        print(f"duplicates  : {len(duplicates)} identical-byte groups")
    if failures:
        print(f"failures    : {len(failures)} (see extraction_report.md)")
    print(f"  -> {config.knowledge_relative(config.EXTRACTION_REPORT_MD_PATH)}")

    return 1 if failures and not records else 0


def run_extract(force: bool, limit: int | None, only: str | None) -> int:
    return _run_processing(True, False, force, limit, only)


def run_chunk(force: bool, limit: int | None, only: str | None) -> int:
    return _run_processing(False, True, force, limit, only)


def run_all(force: bool, limit: int | None, only: str | None) -> int:
    clean_entries = run_validate()
    return _run_processing(True, True, force, limit, only, clean_entries)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser_ = argparse.ArgumentParser(
        prog="python -m app.rag.pipeline",
        description=(
            "Regulatory document preprocessing for NutriReg AI. Validates PDFs "
            "and the manifest, extracts text page by page, detects pages that "
            "need OCR, and produces section-aware chunks with full provenance."
        ),
    )
    subparsers = parser_.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--force", action="store_true",
            help="reprocess documents even when the source is unchanged",
        )
        sub.add_argument(
            "--limit", type=int, default=None,
            help="process only the first N documents (for testing)",
        )
        sub.add_argument(
            "--only", default=None,
            help="process only documents whose path contains this substring",
        )
        sub.add_argument("-v", "--verbose", action="store_true")

    subparsers.add_parser("validate", help="validate PDFs and the manifest").add_argument(
        "-v", "--verbose", action="store_true"
    )
    for name, help_text in (
        ("extract", "extract page-level text and OCR candidates"),
        ("chunk", "detect sections and build chunks"),
        ("all", "validate, then extract and chunk in one pass"),
    ):
        add_common(subparsers.add_parser(name, help=help_text))

    return parser_


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.configure_logging(getattr(args, "verbose", False))
    config.load_env()

    if args.command == "validate":
        run_validate()
        return 0
    if args.command == "extract":
        return run_extract(args.force, args.limit, args.only)
    if args.command == "chunk":
        return run_chunk(args.force, args.limit, args.only)
    if args.command == "all":
        return run_all(args.force, args.limit, args.only)
    return 2


if __name__ == "__main__":
    sys.exit(main())
