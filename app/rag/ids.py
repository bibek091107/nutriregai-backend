"""Deterministic identity for documents and chunks (Phase 10).

Every identifier is derived from stable metadata by hashing. Running the
pipeline twice over unchanged files must produce byte-identical identifiers,
otherwise a re-run would appear to create new documents and orphan the old
chunks.

The original PDF's SHA-256 is deliberately *not* part of the document ID. A
document's identity must survive an edited or re-downloaded copy of the same
regulation; the file hash is the change detector, not the identity.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .config import AUTHORITY

# Separator used when joining identity components. Cannot appear in a path or
# a URL, so the concatenation is unambiguous.
_SEP = "\x1f"


def sha256_file(path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file's bytes, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_path_component(local_path: str) -> str:
    """Normalise a local path for identity purposes.

    Lowercased, forward slashes, no leading ``./``. The filename is kept
    verbatim otherwise, because two regulations that differ only by extension
    case are still the same document.
    """
    if not local_path:
        return ""
    value = local_path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.lower()


def normalize_url_for_identity(url: str | None) -> str:
    """Normalise a URL for identity purposes.

    Trailing slashes and fragment identifiers are dropped because they address
    the same resource. Query strings are preserved: two FSSAI documents
    differing only by query string are different documents.
    """
    if not url:
        return ""
    value = url.strip()
    value = value.split("#", 1)[0]
    if value.endswith("/"):
        value = value[:-1]
    return value.lower()


def normalize_title_for_identity(title: str | None) -> str:
    """Casefold and collapse whitespace in a title."""
    if not title:
        return ""
    return re.sub(r"\s+", " ", title).strip().casefold()


def document_id_for(
    local_path: str,
    title: str | None = None,
    document_url: str | None = None,
    source_url: str | None = None,
) -> str:
    """Build a deterministic document ID.

    Identity components, in order:

    1. normalised ``local_path`` — the authoritative pointer to the file
    2. ``title`` — a re-download under a corrected name changes the path but
       should keep the same document
    3. ``document_url`` — the upstream source of record
    4. ``source_url`` — the portal page, as a final discriminator

    Any single component can be missing; the hash is over whatever is present.
    At least ``local_path`` is always present for a discovered file, so the ID
    is always defined.
    """
    parts = [
        normalize_path_component(local_path),
        normalize_title_for_identity(title),
        normalize_url_for_identity(document_url),
        normalize_url_for_identity(source_url),
    ]
    payload = _SEP.join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{AUTHORITY.lower()}_{digest[:20]}"


def _section_token(section: str | None, subsection: str | None = None) -> str:
    """A filesystem- and ID-safe token for a section reference.

    ``None`` becomes ``nosec``. A section number is never fabricated here: the
    caller passes through exactly what was detected in the source text.
    """
    raw = (section or "").strip()
    if subsection and raw:
        raw = f"{raw}.{subsection}"
    if not raw:
        return "nosec"
    # Keep the readable part of the number so IDs stay human-inspectable.
    token = re.sub(r"[^0-9A-Za-z]+", "_", raw).strip("_").lower()
    return token or "nosec"


def chunk_id_for(
    document_id: str,
    chunk_index: int,
    section: str | None = None,
    subsection: str | None = None,
) -> str:
    """Build a deterministic chunk ID.

    ``chunk_index`` is the chunk's ordinal within its document, assigned in
    reading order. Because reading order is derived from the extracted text, the
    same PDF always yields the same ordinals, hence the same IDs.

    The page range is intentionally *not* part of the ID: inserting or removing
    a page would otherwise renumber every chunk in the document, which would look
    like wholesale churn to a downstream index.
    """
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    return f"{document_id}_{_section_token(section, subsection)}_{chunk_index:05d}"


def short_hash(text: str, length: int = 8) -> str:
    """A short stable hash, for filenames and report keys."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def slugify(text: str | None, max_length: int = 80) -> str:
    """Filesystem-safe slug. Never used for identity — only for display."""
    if not text:
        return "untitled"
    value = unicodedata.normalize("NFKD", text)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return (value or "untitled")[:max_length]
