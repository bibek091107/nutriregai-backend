"""Phase 13 and 14 — extraction reports and human-inspectable samples.

Two audiences, two formats:

* ``extraction_report.json`` is the machine-readable record. It is the thing a
  later ingestion stage reads to know what was processed, what failed, and what
  still needs OCR.
* ``extraction_report.md`` is for a human deciding whether the output can be
  trusted. It leads with the things that would silently corrupt a knowledge base
  if ignored: content collisions, garbled pages, and failures.

``sample_chunks.json`` exists so a reviewer can read actual chunks without
opening 200 files. Samples are spread across categories rather than taken from
the top of the run, because the first documents processed are not a
representative sample of the collection's quality.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_SAMPLES_PER_CATEGORY = 3


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def summarise(records: list[dict]) -> dict:
    """Aggregate per-document records into headline statistics."""
    succeeded = [r for r in records if r.get("status") == "SUCCESS"]
    failed = [r for r in records if r.get("status") != "SUCCESS"]

    token_counts: list[int] = []
    chunk_total = 0
    pages_with_text = 0
    pages_needing_ocr = 0
    sections_total = 0
    chunks_with_section = 0

    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {"documents": 0, "chunks": 0, "pages": 0, "ocr_pages": 0}
    )
    by_source_type: Counter[str] = Counter()
    ocr_documents: list[dict] = []

    for record in records:
        category = record.get("category") or "(uncategorised)"
        bucket = by_category[category]
        bucket["documents"] += 1
        bucket["chunks"] += record.get("chunk_count", 0)
        bucket["pages"] += record.get("page_count") or 0
        bucket["ocr_pages"] += record.get("pages_needing_ocr", 0)

        if record.get("source_type"):
            by_source_type[record["source_type"]] += 1

        if record.get("status") != "SUCCESS":
            continue

        chunk_total += record.get("chunk_count", 0)
        pages_with_text += record.get("pages_with_text", 0)
        pages_needing_ocr += record.get("pages_needing_ocr", 0)
        sections_total += record.get("section_count", 0)

        for token_count in record.get("chunk_token_counts", []):
            token_counts.append(token_count)
        chunks_with_section += record.get("chunks_with_section", 0)

        if record.get("pages_needing_ocr"):
            ocr_documents.append(
                {
                    "document_id": record["document_id"],
                    "title": record.get("title"),
                    "local_path": record.get("local_path"),
                    "page_count": record.get("page_count"),
                    "pages_needing_ocr": record["pages_needing_ocr"],
                    "ocr_page_ratio": round(
                        record["pages_needing_ocr"] / max(record.get("page_count") or 1, 1), 3
                    ),
                }
            )

    ocr_documents.sort(key=lambda r: r["ocr_page_ratio"], reverse=True)

    # Documents where not a single character could be extracted. These produce
    # no chunks at all, which is the correct outcome: a scan has no text to
    # chunk, and inventing any would be fabrication. They are a coverage gap to
    # close with OCR, not a pipeline failure.
    scan_only = [
        r
        for r in records
        if r.get("status") == "SUCCESS"
        and (r.get("page_count") or 0) > 0
        and r.get("pages_with_text") == 0
    ]
    scan_only.sort(key=lambda r: -(r.get("page_count") or 0))

    return {
        "documents_total": len(records),
        "documents_succeeded": len(succeeded),
        "documents_failed": len(failed),
        "documents_awaiting_ocr": len(scan_only),
        "pages_awaiting_ocr": sum(r.get("page_count") or 0 for r in scan_only),
        "pages_total": sum(r.get("page_count") or 0 for r in records),
        "pages_with_text": pages_with_text,
        "pages_needing_ocr": pages_needing_ocr,
        "ocr_page_ratio": round(
            pages_needing_ocr / max(sum(r.get("page_count") or 0 for r in records), 1), 4
        ),
        "sections_detected": sections_total,
        "chunks_total": chunk_total,
        "chunks_per_document": round(chunk_total / max(len(succeeded), 1), 2),
        "chunks_with_section_number": chunks_with_section,
        "chunks_without_section_number": chunk_total - chunks_with_section,
        "chunk_token_stats": {
            "min": min(token_counts) if token_counts else 0,
            "p25": _percentile(token_counts, 0.25),
            "p50": int(statistics.median(token_counts)) if token_counts else 0,
            "p75": _percentile(token_counts, 0.75),
            "p90": _percentile(token_counts, 0.90),
            "p99": _percentile(token_counts, 0.99),
            "max": max(token_counts) if token_counts else 0,
            "mean": int(statistics.fmean(token_counts)) if token_counts else 0,
            "over_target_max": len([t for t in token_counts if t > config.CHUNK_MAX_TOKENS]),
            "under_min": len([t for t in token_counts if t < config.CHUNK_MIN_TOKENS]),
        },
        "by_source_type": dict(by_source_type.most_common()),
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
        "ocr_candidate_documents": ocr_documents[:25],
        "ocr_candidate_document_count": len(ocr_documents),
        "documents_awaiting_ocr_detail": [
            {
                "document_id": r["document_id"],
                "title": r.get("title"),
                "local_path": r.get("local_path"),
                "page_count": r.get("page_count"),
                "source_type": r.get("source_type"),
                "category": r.get("category"),
                "reason": "no extractable text on any page; the PDF is a scan",
            }
            for r in scan_only
        ],
    }


def write_extraction_report_json(
    summary: dict,
    records: list[dict],
    collisions: dict,
    failures: list[dict],
    duplicates: list[dict],
    path: Path | None = None,
) -> Path:
    target = path or config.EXTRACTION_REPORT_JSON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stage": "regulatory document preprocessing",
        "authority": config.AUTHORITY,
        "jurisdiction": config.JURISDICTION,
        "scope": {
            "included": "local PDFs in the collection, excluding quarantine_html",
            "excluded_by_design": [
                "embeddings",
                "pgvector",
                "vector search",
                "BM25",
                "hybrid retrieval",
                "reranking",
                "Groq / LLM calls",
                "FastAPI endpoints",
            ],
        },
        "chunking": {
            "target_tokens": config.CHUNK_TARGET_TOKENS,
            "min_tokens": config.CHUNK_MIN_TOKENS,
            "max_tokens": config.CHUNK_MAX_TOKENS,
            "overlap_tokens": config.CHUNK_OVERLAP_TOKENS,
            "token_estimator": "deterministic local word+punctuation count",
        },
        "summary": summary,
        "manifest_collisions": collisions,
        "duplicate_source_files": duplicates,
        "failures": failures,
        "documents": records,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote extraction report -> %s", config.knowledge_relative(target))
    return target


# Why each OCR signal fires, so a reader of the report knows which pages OCR
# can actually fix and which need the text layer rebuilt.
_OCR_SIGNAL_MEANING = {
    "broken_devanagari": (
        "Devanagari whose glyph ordering was destroyed by the source font: "
        "dependent vowel signs detached from their consonant, or glyphs "
        "substituted from an unrelated script. Reads as Hindi but is "
        "meaningless to search or embeddings."
    ),
    "image_dominated": "Page is mostly image area with a thin text layer.",
    "low_text": (
        "Very little extractable text, typically a near-blank or table-only page."
    ),
    "mojibake": (
        "Western code points standing in for a non-Latin script, characteristic "
        "of pre-Unicode legacy font encodings."
    ),
}


def _table(headers: list[str], rows: list[list[object]]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out) + "\n"


def write_extraction_report_md(
    summary: dict,
    records: list[dict],
    collisions: dict,
    failures: list[dict],
    duplicates: list[dict],
    path: Path | None = None,
) -> Path:
    target = path or config.EXTRACTION_REPORT_MD_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    tokens = summary["chunk_token_stats"]
    lines: list[str] = []
    add = lines.append

    add("# Regulatory Document Extraction Report")
    add("")
    add(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    add("")
    add(f"Authority: **{config.AUTHORITY}** ({config.AUTHORITY_FULL_NAME})  ")
    add(f"Jurisdiction: **{config.JURISDICTION}**")
    add("")
    add(
        "Scope is preprocessing only: PDF validation, page extraction, OCR "
        "detection, section-aware chunking and provenance. No embeddings, no "
        "pgvector, no retrieval, no LLM calls."
    )
    add("")

    add("## Run summary")
    add("")
    add(
        _table(
            ["Metric", "Value"],
            [
                ["Documents discovered", summary["documents_total"]],
                ["Documents processed", summary["documents_succeeded"]],
                ["Documents failed", summary["documents_failed"]],
                ["Pages extracted", summary["pages_total"]],
                ["Pages with usable text", summary["pages_with_text"]],
                ["Pages flagged for OCR", summary["pages_needing_ocr"]],
                ["Sections detected", summary["sections_detected"]],
                ["Chunks produced", summary["chunks_total"]],
                ["Chunks per document (mean)", summary["chunks_per_document"]],
            ],
        )
    )

    add("## Chunk size distribution")
    add("")
    add(
        _table(
            ["Statistic", "Tokens"],
            [
                ["min", tokens["min"]],
                ["p25", tokens["p25"]],
                ["p50", tokens["p50"]],
                ["p75", tokens["p75"]],
                ["p90", tokens["p90"]],
                ["p99", tokens["p99"]],
                ["max", tokens["max"]],
                ["mean", tokens["mean"]],
                ["chunks above max", tokens["over_target_max"]],
                ["chunks below min", tokens["under_min"]],
            ],
        )
    )
    add(
        f"Target band is {config.CHUNK_MIN_TOKENS}-{config.CHUNK_MAX_TOKENS} "
        f"tokens with a {config.CHUNK_OVERLAP_TOKENS}-token overlap. Chunks "
        "below the minimum are sections that are genuinely short in the source "
        "document; they are kept whole rather than padded with neighbouring "
        "sections, because padding would misattribute the text."
    )
    add("")

    add("## Sections")
    add("")
    add(
        f"- Chunks with a section number: **{summary['chunks_with_section_number']}**\n"
        f"- Chunks with `section = null`: **{summary['chunks_without_section_number']}**\n"
    )
    add(
        "A null section means no reliable section marker was present in the "
        "source. No section number is ever inferred, completed or guessed."
    )
    add("")

    add("## Content requiring attention")
    add("")
    add("### Manifest collisions (one file, several manifest entries)")
    add("")
    if collisions.get("collisions"):
        add(
            f"**{collisions['files_with_multiple_manifest_claimants']} files** are "
            f"claimed by more than one manifest entry, suppressing "
            f"{collisions['extra_claimants_suppressed']} duplicate extractions. "
            "Each file was extracted once under a canonical document ID."
        )
        add("")
        for collision in collisions["collisions"]:
            add(f"- `{collision['local_path']}`")
            add(f"  - canonical: {collision['canonical_title']}")
            add(f"  - claimants: {collision['claimant_count']}")
            for url in collision["alias_document_urls"]:
                add(f"  - suppressed alias URL: {url}")
    else:
        add("_none_")

    add("")
    add("### Duplicate source files (identical bytes, different filenames)")
    add("")
    if duplicates:
        for group in duplicates:
            add(f"- sha256 `{group['sha256'][:16]}...`")
            for path in group["local_paths"]:
                add(f"  - `{path}`")
    else:
        add("_none_")

    add("")
    add("### Documents that produced no chunks")
    add("")
    add(
        f"**{summary['documents_awaiting_ocr']} documents** "
        f"({summary['pages_awaiting_ocr']} pages) yielded no extractable text on "
        "any page. They are image scans, so the pipeline correctly produced zero "
        "chunks for them rather than inventing text. They are a coverage gap: "
        "until they are OCR'd, they cannot be retrieved from."
    )
    add("")
    awaiting = summary.get("documents_awaiting_ocr_detail", [])
    if awaiting:
        add(
            _table(
                ["Pages", "Title", "Source type"],
                [
                    [r["page_count"], (r.get("title") or "")[:70], r.get("source_type")]
                    for r in awaiting
                ],
            )
        )

    add("")
    add("### Pages flagged for OCR within otherwise readable documents")
    add("")
    add(
        f"**{summary['ocr_candidate_document_count']} documents** contain pages "
        f"that should be OCR'd before their text is trusted "
        f"({summary['ocr_page_ratio']:.1%} of all pages). Detection only — no OCR "
        "was performed."
    )
    add("")
    reasons: Counter[str] = Counter()
    for record in records:
        for reason in record.get("ocr_reason_counts", {}):
            reasons[reason] += record["ocr_reason_counts"][reason]
    if reasons:
        add("Reason breakdown across all documents:")
        add("")
        add(_table(["Signal", "Pages", "Meaning"],
                   [[k, v, _OCR_SIGNAL_MEANING.get(k, "")]
                    for k, v in reasons.most_common()]))
        add("")
        add("These counts are a detection signal, not a defect in this pipeline. "
            "OCRing a `broken_devanagari` page with a modern Hindi model is "
            "unlikely to help, because the damage is in the text layer the PDF "
            "stores rather than in the pixels; those pages need a font-recovery "
            "pass or re-sourcing. `image_dominated` and `low_text` pages are the "
            "ones OCR is expected to resolve.")

    add("")
    add("### Failures")
    add("")
    if failures:
        add(_table(
            ["Document", "Path", "Error"],
            [[f.get("title") or f.get("document_id"), f"`{f.get('local_path')}`",
              (f.get("error") or "")[:120]] for f in failures],
        ))
    else:
        add("_none_")

    add("")
    add("## Coverage by category")
    add("")
    add(
        _table(
            ["Category", "Documents", "Pages", "Chunks", "OCR pages"],
            [
                [cat, v["documents"], v["pages"], v["chunks"], v["ocr_pages"]]
                for cat, v in summary["by_category"].items()
            ],
        )
    )

    add("")
    add("## Coverage by source type")
    add("")
    add(_table(["Source type", "Documents"],
               [[k, v] for k, v in summary["by_source_type"].items()]))

    add("")
    add("## Outputs")
    add("")
    add("```")
    add("processed/documents/<document_id>.json   per-document record")
    add("processed/pages/<document_id>.jsonl      one line per page")
    add("processed/chunks/<document_id>.jsonl     one line per chunk")
    add("processed/reports/sample_chunks.json     readable samples")
    add("```")

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote extraction report -> %s", config.knowledge_relative(target))
    return target


def write_sample_chunks(records: list[dict], path: Path | None = None) -> Path:
    """Write a spread of real chunks for human inspection.

    Reads the per-document chunk files back rather than holding every chunk in
    memory, and samples evenly across categories so the output is not dominated
    by whichever document happened to run first.
    """
    target = path or config.SAMPLE_CHUNKS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    by_category: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record.get("status") != "SUCCESS" or not record.get("chunk_count"):
            continue
        by_category[record.get("category") or "(uncategorised)"].append(record)

    samples: list[dict] = []
    for category in sorted(by_category):
        documents = sorted(
            by_category[category],
            key=lambda r: (-(r.get("chunk_count") or 0), r.get("local_path") or ""),
        )
        # Take the chunk files with the most chunks: the most representative
        # of a document's internal structure.
        for document in documents[:_SAMPLES_PER_CATEGORY]:
            chunk_file = config.CHUNKS_DIR / f"{document['document_id']}.jsonl"
            if not chunk_file.is_file():
                continue
            lines = chunk_file.read_text(encoding="utf-8").splitlines()
            if not lines:
                continue
            picks = _spread(len(lines), min(3, len(lines)))
            for index in picks:
                try:
                    chunk = json.loads(lines[index])
                except json.JSONDecodeError:
                    continue
                chunk["_sample_category"] = category
                samples.append(chunk)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_count": len(samples),
        "categories_covered": sorted(by_category),
        "note": "Chunks are verbatim pipeline output, sampled across categories.",
        "chunks": samples,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "wrote %d sample chunks -> %s", len(samples), config.knowledge_relative(target)
    )
    return target


def _spread(total: int, count: int) -> list[int]:
    """Evenly spaced indices across a range, always including the first."""
    if total <= 0 or count <= 0:
        return []
    if count >= total:
        return list(range(total))
    step = total / count
    return sorted({int(i * step) for i in range(count)} | {0})


def find_duplicate_sources(records: list[dict]) -> list[dict]:
    """Group documents whose bytes are identical despite different filenames."""
    by_hash: dict[str, list[str]] = defaultdict(list)
    for record in records:
        if record.get("source_sha256"):
            by_hash[record["source_sha256"]].append(record["local_path"])
    return [
        {"sha256": digest, "local_paths": sorted(paths), "count": len(paths)}
        for digest, paths in sorted(by_hash.items())
        if len(paths) > 1
    ]
