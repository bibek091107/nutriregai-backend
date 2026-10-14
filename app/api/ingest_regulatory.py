"""Load the preprocessed FSSAI corpus into the ``regulatory_*`` tables.

Reads the JSON and JSONL produced by ``app.rag.pipeline`` and writes:

    regulatory_sources     one row per authority+title (the citation root)
    regulatory_documents   one row per preprocessed document, keyed by content hash
    regulatory_sections    one row per detected section within a document
    regulatory_chunks      one row per chunk, with retrieval filters in metadata

Ingestion is idempotent. ``regulatory_documents.content_hash`` is unique, and the
source of that hash is the source file's SHA-256 from the pipeline, so
re-running after an unchanged extraction is a no-op and re-running after a real
change replaces the document. Evidence rows that pointed at replaced chunks keep
their ``excerpt`` snapshot, which is what preserves citation immutability.

Run:
    python -m app.api.ingest_regulatory [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from typing import Any, Optional

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.rag import config as rag_config  # noqa: E402

# Sentinel for chunks that carry no reliable section number. The column is
# NOT NULL, and a NULL would break the citation chain, so an explicit,
# self-describing placeholder is used. It is never rendered as a legal citation:
# the API returns it verbatim and the UI shows it as unnumbered.
UNNUMBERED = "unnumbered"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# The manifest carries human-readable dates such as "10th September 2021" and
# bare years such as "2011". Anything that is not an unambiguous calendar date
# is stored as NULL rather than guessed, because a wrong effective date in a
# regulatory citation is worse than an absent one.
_ORDINAL_DATE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")


def parse_effective_date(raw: Optional[str]) -> Optional[date]:
    """Parse a manifest date, returning None unless it is unambiguous."""
    if not raw:
        return None
    value = raw.strip()

    match = _ORDINAL_DATE.match(value)
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
        return None

    match = _YEAR_ONLY.match(value)
    if match:
        # A bare year is not a date. January 1st would be an invented fact.
        return None

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def source_type_for(raw: Optional[str]) -> Optional[str]:
    """Map the pipeline's source_type onto the schema's controlled vocabulary.

    The manifest uses a richer vocabulary than the schema allows, so anything
    outside ACT/REGULATION/GUIDANCE/STANDARD/NOTIFICATION is stored as NULL
    rather than forced into a value it does not mean. The original string is
    preserved in the document title and in the chunk metadata.
    """
    if not raw:
        return None
    candidate = raw.strip().upper()
    allowed = {"ACT", "REGULATION", "GUIDANCE", "STANDARD", "NOTIFICATION"}
    return candidate if candidate in allowed else None


def load_documents() -> list[dict[str, Any]]:
    records = []
    for path in sorted(rag_config.DOCUMENTS_DIR.glob("*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def load_chunks(document_id: str) -> list[dict[str, Any]]:
    path = rag_config.CHUNKS_DIR / f"{document_id}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def ingest(force: bool = False) -> dict[str, int]:
    dsn = os.environ.get(
        "DATABASE_URL",
        "postgresql://nutrireg:nutrireg_local_dev@localhost:5432/nutrireg",
    )
    connection = await asyncpg.connect(dsn)
    counters = {
        "sources": 0, "documents": 0, "sections": 0, "chunks": 0,
        "documents_skipped": 0, "chunks_flagged_for_ocr": 0,
    }

    try:
        documents = load_documents()
        for record in documents:
            document_id = record.get("document_id")
            if not document_id:
                continue

            content_hash = record.get("source_sha256")
            if not content_hash:
                print(f"  skip {document_id}: no source hash", file=sys.stderr)
                continue

            if not force:
                existing = await connection.fetchval(
                    "SELECT id FROM regulatory_documents WHERE content_hash = $1",
                    content_hash,
                )
                if existing is not None:
                    counters["documents_skipped"] += 1
                    continue

            authority = record.get("authority") or "FSSAI"
            title = (record.get("title") or document_id).strip()
            jurisdiction = record.get("jurisdiction")
            version = record.get("version")
            effective = parse_effective_date(record.get("effective_date"))
            stored_source_type = source_type_for(record.get("source_type"))
            # The schema requires an absolute HTTPS URL; a non-HTTPS value is
            # stored as NULL rather than rejected, because the citation is still
            # valid without a link.
            source_url = record.get("document_url")
            if source_url and not source_url.startswith("https://"):
                source_url = None

            async with connection.transaction():
                source_row = await connection.fetchrow(
                    """
                    INSERT INTO regulatory_sources
                        (authority, title, document_title, source_type, jurisdiction,
                         version, effective_date, source_url, last_indexed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    authority, title, title, stored_source_type, jurisdiction,
                    version, effective, source_url,
                )
                if source_row is None:
                    # No unique constraint exists on these columns, so match the
                    # existing row explicitly rather than creating a duplicate
                    # source for the same document.
                    source_row = await connection.fetchrow(
                        """
                        SELECT id FROM regulatory_sources
                        WHERE authority = $1 AND title = $2
                        LIMIT 1
                        """,
                        authority, title,
                    )
                if source_row is None:
                    continue
                counters["sources"] += 1

                # Re-ingesting the same content hash must replace, not duplicate.
                # regulatory_documents is append-only by design, so the old row is
                # removed first; evidence keeps its excerpt snapshot and its chunk
                # pointer is set to NULL by the FK's ON DELETE SET NULL.
                await connection.execute(
                    "DELETE FROM regulatory_documents WHERE content_hash = $1", content_hash
                )
                document_row = await connection.fetchrow(
                    """
                    INSERT INTO regulatory_documents
                        (source_id, title, document_version, effective_date,
                         file_storage_key, content_hash)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                    """,
                    source_row["id"], title, version, effective,
                    record.get("local_path"), content_hash,
                )
                counters["documents"] += 1

                chunks = load_chunks(document_id)
                # Group chunks into sections keyed by section number, preserving
                # first-appearance order and the document's own section index.
                grouped: dict[str, list[dict[str, Any]]] = {}
                for chunk in chunks:
                    key = str(chunk.get("section") or UNNUMBERED)
                    grouped.setdefault(key, []).append(chunk)

                for position, (section_number, section_chunks) in enumerate(grouped.items()):
                    heading = next(
                        (c.get("heading_text") for c in section_chunks if c.get("heading_text")),
                        None,
                    )
                    page_starts = [c["page_start"] for c in section_chunks if c.get("page_start")]
                    page_ends = [c["page_end"] for c in section_chunks if c.get("page_end")]
                    section_row = await connection.fetchrow(
                        """
                        INSERT INTO regulatory_sections
                            (regulatory_document_id, section_number, title,
                             page_start, page_end, content)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (regulatory_document_id, section_number)
                        DO UPDATE SET title = EXCLUDED.title,
                                      page_start = EXCLUDED.page_start,
                                      page_end = EXCLUDED.page_end,
                                      content = EXCLUDED.content
                        RETURNING id
                        """,
                        document_row["id"],
                        section_number,
                        heading,
                        min(page_starts) if page_starts else None,
                        max(page_ends) if page_ends else None,
                        "\n\n".join(c["text"] for c in section_chunks),
                    )
                    counters["sections"] += 1

                    for index, chunk in enumerate(section_chunks):
                        metadata = {
                            "authority": chunk.get("authority"),
                            "jurisdiction": chunk.get("jurisdiction"),
                            "source_type": chunk.get("source_type"),
                            "category": chunk.get("category"),
                            "version": chunk.get("version"),
                            "effective_date": chunk.get("effective_date"),
                            "heading_text": chunk.get("heading_text"),
                            "chunk_id": chunk.get("chunk_id"),
                            "token_estimate": chunk.get("token_estimate"),
                            "section_position": position,
                            # Carried so a consumer can tell trustworthy text from
                            # text the pipeline already flagged as needing OCR.
                            "ocr_page_numbers": chunk.get("ocr_page_numbers") or [],
                            "needs_ocr": bool(chunk.get("ocr_page_numbers")),
                        }
                        await connection.execute(
                            """
                            INSERT INTO regulatory_chunks
                                (section_id, chunk_index, content, page_number, metadata)
                            VALUES ($1, $2, $3, $4, $5::jsonb)
                            """,
                            section_row["id"],
                            index,
                            chunk["text"],
                            chunk.get("page_start"),
                            json.dumps(metadata),
                        )
                        counters["chunks"] += 1
                        if chunk.get("ocr_page_numbers"):
                            counters["chunks_flagged_for_ocr"] += 1
    finally:
        await connection.close()

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-ingest even when the content hash is already present",
    )
    args = parser.parse_args()

    counters = asyncio_run(ingest(force=args.force))
    print("Regulatory corpus ingested")
    for key, value in counters.items():
        print(f"  {key:26} {value}")
    return 0


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


if __name__ == "__main__":
    raise SystemExit(main())
