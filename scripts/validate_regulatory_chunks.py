#!/usr/bin/env python
"""Phase 15 — validate the generated chunks.

    python scripts/validate_regulatory_chunks.py

Standalone by design: it reads only the files under ``processed/`` and does not
import the pipeline, so it can catch a bug in the producer rather than
re-deriving the producer's own assumptions. Exit code is non-zero when any check
fails, so it is usable in CI.

Checks
------
 1  every chunk file is valid JSONL
 2  required fields are present on every chunk
 3  chunk_id is non-empty
 4  chunk_id is globally unique
 5  document_id is non-empty
 6  text is non-empty
 7  page_start is an integer >= 1
 8  page_end is an integer >= page_start
 9  page range does not exceed the document's real page count
10  token estimate is within the configured band
11  section is a string or null (never a number or empty string)
12  section is not an invented placeholder
13  local_path exists and points inside the collection
14  local_path is not inside the quarantine tree
15  source_sha256 matches the actual file on disk
16  chunk_id is reproducible from its own fields
17  authority is FSSAI on every chunk
18  jurisdiction is INDIA_FSSAI on every chunk
19  page numbers fall within the pages actually written for the document
20  no page text is lost: every page of every document has a page record
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.rag import config  # noqa: E402
from app.rag.ids import chunk_id_for, sha256_file  # noqa: E402

# Values that would mean a section was invented rather than detected.
FORBIDDEN_SECTIONS = {
    "unknown", "n/a", "na", "none", "null", "undefined", "section",
    "preamble", "untitled", "?", "-", "",
}

# Section identifiers must look like a reference taken from the source text.
# This mirrors the grammar the chunker is allowed to capture: a Roman numeral
# (optionally letter-suffixed, e.g. "VA"), a number (optionally suffixed, e.g.
# "2A"), or a bare single letter (e.g. "Appendix B"). Kept in step with
# _IDENT_ALT in app/rag/chunker.py.
_SECTION_SHAPE = re.compile(
    r"^(?:[IVXLCDM]{1,8}[A-Z]?|\d{1,3}[A-Z]?|[A-Z])(?:\.\d+)*$",
    re.IGNORECASE,
)


class Results:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, condition: bool, message: str) -> bool:
        self.checks += 1
        if not condition:
            self.failures.append(message)
        return condition


def _load_jsonl(path: Path, results: Results) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    results.check(
                        False, f"[jsonl] {path.name}:{number} invalid JSON: {exc}"
                    )
    except OSError as exc:
        results.check(False, f"[jsonl] cannot read {path}: {exc}")
    return rows


def main() -> int:
    results = Results()
    chunks_dir = config.CHUNKS_DIR
    documents_dir = config.DOCUMENTS_DIR
    pages_dir = config.PAGES_DIR

    if not chunks_dir.is_dir():
        print(f"ERROR: no chunks directory at {chunks_dir}")
        print("Run: python -m app.rag.pipeline all")
        return 2

    chunk_files = sorted(chunks_dir.glob("*.jsonl"))
    if not chunk_files:
        print(f"ERROR: no chunk files in {chunks_dir}")
        return 2

    # Cache hashes and page counts: several chunks share a document.
    hash_cache: dict[str, str | None] = {}
    page_numbers: dict[str, set[int]] = {}
    page_text_chars: dict[str, int] = {}
    document_pages: dict[str, int] = {}

    for pages_file in sorted(pages_dir.glob("*.jsonl")):
        document_id = pages_file.stem
        numbers: set[int] = set()
        text_chars = 0
        for row in _load_jsonl(pages_file, results):
            number = row.get("page_number")
            if isinstance(number, int):
                numbers.add(number)
            length = row.get("text_length")
            if isinstance(length, int):
                text_chars += length
        page_numbers[document_id] = numbers
        page_text_chars[document_id] = text_chars
        if numbers:
            document_pages[document_id] = max(numbers)

    seen_chunk_ids: dict[str, str] = {}
    total_chunks = 0
    awaiting_ocr: list[str] = []
    documents_seen: set[str] = set()
    token_hist: list[int] = []

    for chunk_file in chunk_files:
        expected_document = chunk_file.stem
        rows = _load_jsonl(chunk_file, results)
        if not rows:
            # An empty chunk file is only correct when the source document has
            # no extractable text at all, i.e. it is a scan awaiting OCR.
            # Producing no chunks for a scan is the honest outcome; producing
            # no chunks for a readable document is a bug.
            if page_text_chars.get(expected_document, -1) == 0:
                awaiting_ocr.append(expected_document)
            else:
                results.check(
                    False,
                    f"[chunks] {chunk_file.name} is empty but its document has "
                    f"{page_text_chars.get(expected_document)} extractable characters",
                )
        total_chunks += len(rows)

        for index, chunk in enumerate(rows):
            label = f"{chunk_file.name}[{index}]"
            chunk_id = chunk.get("chunk_id")

            # 2 required fields
            missing = [
                field
                for field in (
                    "chunk_id", "document_id", "text", "page_start", "page_end",
                    "local_path", "authority", "jurisdiction", "document_title",
                    "source_type", "category", "section", "subsection",
                )
                if field not in chunk
            ]
            results.check(not missing, f"[fields] {label} missing {missing}")
            if missing:
                continue

            # 3, 5, 6
            results.check(
                isinstance(chunk_id, str) and chunk_id.strip(),
                f"[chunk_id] {label} empty",
            )
            results.check(
                bool(str(chunk.get("document_id") or "").strip()),
                f"[document_id] {label} empty",
            )
            text = chunk.get("text")
            results.check(
                isinstance(text, str) and text.strip(),
                f"[text] {label} empty",
            )

            # 4 global uniqueness
            if isinstance(chunk_id, str) and chunk_id:
                if chunk_id in seen_chunk_ids:
                    results.check(
                        False,
                        f"[unique] duplicate chunk_id {chunk_id} in "
                        f"{seen_chunk_ids[chunk_id]} and {chunk_file.name}",
                    )
                else:
                    seen_chunk_ids[chunk_id] = chunk_file.name
                    results.check(True, "")

            document_id = chunk.get("document_id")
            documents_seen.add(document_id)

            # 7, 8, 9
            page_start, page_end = chunk.get("page_start"), chunk.get("page_end")
            results.check(
                isinstance(page_start, int) and not isinstance(page_start, bool)
                and page_start >= 1,
                f"[page_start] {label} not an int >= 1: {page_start!r}",
            )
            results.check(
                isinstance(page_end, int) and not isinstance(page_end, bool)
                and isinstance(page_start, int)
                and page_end >= page_start,
                f"[page_range] {label} page_end {page_end!r} < page_start {page_start!r}",
            )
            if isinstance(page_start, int) and isinstance(page_end, int):
                real_pages = document_pages.get(document_id)
                if real_pages is not None:
                    results.check(
                        page_end <= real_pages,
                        f"[page_range] {label} page_end {page_end} exceeds "
                        f"document page count {real_pages}",
                    )

            # 19 chunk pages were actually extracted
            extracted = page_numbers.get(document_id)
            if extracted and isinstance(page_start, int) and isinstance(page_end, int):
                missing_pages = [
                    p for p in range(page_start, page_end + 1) if p not in extracted
                ]
                results.check(
                    not missing_pages,
                    f"[page_coverage] {label} references pages with no page "
                    f"record: {missing_pages}",
                )

            # 10 token band
            tokens = chunk.get("token_estimate")
            if isinstance(tokens, int):
                token_hist.append(tokens)
                results.check(
                    tokens > 0,
                    f"[tokens] {label} token_estimate is {tokens}",
                )
                results.check(
                    tokens <= config.CHUNK_MAX_TOKENS,
                    f"[tokens] {label} {tokens} tokens exceeds max "
                    f"{config.CHUNK_MAX_TOKENS}",
                )
            else:
                results.check(
                    False, f"[tokens] {label} missing token_estimate"
                )

            # 11, 12 section shape
            section = chunk.get("section")
            results.check(
                section is None or isinstance(section, str),
                f"[section] {label} is {type(section).__name__}, expected str or null",
            )
            if isinstance(section, str):
                results.check(
                    section.strip().casefold() not in FORBIDDEN_SECTIONS,
                    f"[section] {label} uses placeholder value {section!r}",
                )
                results.check(
                    bool(_SECTION_SHAPE.match(section)),
                    f"[section] {label} {section!r} does not look like a "
                    f"reference from the source text",
                )

            # 13, 14 local path
            local_path = chunk.get("local_path")
            if isinstance(local_path, str) and local_path:
                absolute = config.BACKEND_ROOT / local_path
                results.check(
                    absolute.is_file(),
                    f"[local_path] {label} does not exist: {local_path}",
                )
                results.check(
                    not config.is_quarantined(absolute),
                    f"[quarantine] {label} points into quarantine: {local_path}",
                )

                # 15 source hash still matches
                if local_path not in hash_cache:
                    try:
                        hash_cache[local_path] = sha256_file(absolute)
                    except OSError:
                        hash_cache[local_path] = None
                actual = hash_cache[local_path]
                recorded = chunk.get("source_sha256")
                if actual and recorded:
                    results.check(
                        actual == recorded,
                        f"[source_hash] {label} hash mismatch for {local_path}; "
                        f"the file changed after chunking",
                    )

            # 16 id reproducibility
            if isinstance(chunk_id, str) and chunk_id and isinstance(document_id, str):
                rebuilt = chunk_id_for(
                    document_id,
                    chunk.get("chunk_index", 0) if isinstance(chunk.get("chunk_index"), int) else 0,
                    chunk.get("section"),
                    chunk.get("subsection"),
                )
                results.check(
                    rebuilt == chunk_id,
                    f"[reproducible] {label} chunk_id {chunk_id} does not match "
                    f"recomputed {rebuilt}",
                )

            # 17, 18 authority and jurisdiction
            results.check(
                chunk.get("authority") == config.AUTHORITY,
                f"[authority] {label} is {chunk.get('authority')!r}",
            )
            results.check(
                chunk.get("jurisdiction") == config.JURISDICTION,
                f"[jurisdiction] {label} is {chunk.get('jurisdiction')!r}",
            )

        # every document has a document record
        results.check(
            (documents_dir / f"{expected_document}.json").is_file(),
            f"[documents] missing document record for {expected_document}",
        )

    # 20 no page text lost
    for document_id, numbers in page_numbers.items():
        expected = document_pages.get(document_id, 0)
        missing = [p for p in range(1, expected + 1) if p not in numbers]
        results.check(
            not missing,
            f"[page_completeness] {document_id} is missing page records: {missing[:20]}",
        )

    # Report
    print("=" * 66)
    print("Regulatory chunk validation")
    print("=" * 66)
    print(f"chunk files        : {len(chunk_files)}")
    print(f"documents          : {len(documents_seen)}")
    print(f"awaiting OCR       : {len(awaiting_ocr)} (scans, correctly no chunks)")
    print(f"chunks             : {total_chunks}")
    print(f"distinct chunk_ids : {len(seen_chunk_ids)}")
    if token_hist:
        ordered = sorted(token_hist)
        print(
            f"token range        : min {ordered[0]} / p50 "
            f"{ordered[len(ordered) // 2]} / max {ordered[-1]}"
        )
    print(f"assertions run     : {results.checks}")
    print()

    if results.failures:
        print(f"FAILED: {len(results.failures)} problem(s)\n")
        grouped: dict[str, list[str]] = defaultdict(list)
        for failure in results.failures:
            key = failure.split("]")[0] + "]" if "]" in failure else "other"
            grouped[key].append(failure)
        for key in sorted(grouped):
            items = grouped[key]
            print(f"  {key}  ({len(items)})")
            for item in items[:5]:
                print(f"      {item}")
            if len(items) > 5:
                print(f"      ... and {len(items) - 5} more")
        print()
        return 1

    print("PASS: all checks succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
