"""Phases 7 to 10 — section detection, section-aware chunking, provenance.

Design rules, in priority order
-------------------------------
1. **Never invent a section number.** A section is a regex capture taken from
   the document's own text. When no reliable marker exists the field is
   ``None``. There is no "preamble" fallback, no sequential numbering, and no
   inference from page position.
2. **Section boundaries are hard chunk boundaries.** A chunk never spans two
   sections, so a retrieved chunk cannot silently mix two unrelated provisions.
3. **Never combine everything into one giant chunk.** A section longer than the
   target is split, and the split points are real line/sentence boundaries.
4. **Deterministic.** Same PDF in, same chunk IDs and identical text out.
5. **Page provenance is measured, not assumed.** ``page_start``/``page_end``
   come from the actual page each retained line came from, which is what makes
   the "quote the page back to a user" promise verifiable.

Overlap is applied only *within* a section. Overlapping across a section
boundary would duplicate a heading into the following chunk and, worse, would
place text under a section number it does not belong to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import config
from .ids import chunk_id_for
from .models import ChunkRecord
from .parser import PageLine, has_broken_devanagari, is_legacy_encoded

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Heading patterns
# --------------------------------------------------------------------------
# Every pattern captures the identifier it matched. Nothing is synthesised, so
# if a document has no section numbers, neither will its chunks.
#
# Each pattern requires an explicit keyword. A bare line containing only "I" or
# "12" is deliberately *not* treated as a section marker: in regulation prose
# those are running headers and enumeration artifacts far more often than they
# are section references, and a false section is worse than a missing one.
# A structural reference identifier. FSSAI schedules are not only pure Roman
# numerals: they run Schedule I..VI and then Schedule VA, VB, VC, VD, VE, so a
# letter suffix is part of the identifier and not part of a following title.
# Appendices are also commonly lettered (Appendix A, Appendix B), which no
# Roman-numeral class covers, so a bare single letter is accepted too. This is
# only ever reached after a keyword such as APPENDIX has already matched, so a
# loose letter class cannot turn arbitrary text into a section.
_IDENT_ALT = r"[IVXLCDM]{1,8}[A-Z]?|\d{1,2}[A-Z]?|[A-Z]"
# Lookahead prevents stopping mid-token, e.g. reading "V" out of "VA".
_NOT_MORE_WORD = r"(?![A-Za-z0-9])"
_SEP = r"\s*[-–—.:]*\s*"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CHAPTER", re.compile(rf"^CHAPTER{_SEP}({_IDENT_ALT}){_NOT_MORE_WORD}", re.I)),
    (
        "SCHEDULE",
        re.compile(rf"^(?:THE\s+)?SCHEDULE(?:E|S)?{_SEP}({_IDENT_ALT})?{_NOT_MORE_WORD}", re.I),
    ),
    (
        "ANNEXURE",
        re.compile(rf"^(?:ANNEXURE|APPENDIX){_SEP}({_IDENT_ALT})?{_NOT_MORE_WORD}", re.I),
    ),
    ("PART", re.compile(rf"^PART{_SEP}({_IDENT_ALT}){_NOT_MORE_WORD}", re.I)),
    (
        "REGULATION",
        re.compile(rf"^Regulation{_SEP}(\d+(?:\.\d+)*)(?![0-9])", re.I),
    ),
    # A clause reference is either a bare number closed by a delimiter
    # ("2. COMPOSITION", "7) Short title") or a number carrying depth
    # ("16.0 Prepared foods", "2.1 Labelling", "12.9.2.3 Other").
    #
    # A bare integer followed only by a space is deliberately NOT accepted:
    # that is the shape of a table cell ("200 mg/kg]"), and reading one as a
    # section number would stamp a fabricated reference on every chunk of the
    # surrounding table.
    ("NUMBERED", re.compile(r"^(\d{1,3}(?:\.\d+)+)\s*[.)]?\s+[A-Za-z]")),
    ("NUMBERED", re.compile(r"^(\d{1,3})\s*[.)]\s+[A-Za-z]")),
)

# A line that is only digits and dashes is a page number, not a heading.
_PAGE_NUMBER = re.compile(r"^[\s\-–—]*\d{1,4}[\s\-–—]*$")

# Structural reference words. A genuine heading names its own reference once; a
# table row or a cross-reference sentence names several.
_STRUCTURAL_WORD = re.compile(
    r"\b(schedule|chapter|part|annexure|appendix|regulation)s?\b",
    re.IGNORECASE,
)

# A heading may carry a short title after its reference, but not a sentence.
# This is only a backstop: real titles such as "Standards of Packaging and
# Labelling" run to five or six words, so the word count is deliberately
# generous and the sentence-marker test below does the real work.
_MAX_TITLE_WORDS = 6

# A heading title is a noun phrase. It does not contain modals or copulas, which
# is what distinguishes "Schedule VIII - Additives" from a table row reading
# "Schedule VIII may be used in food in accordance with these regulations".
# Gazette reference markers. An amendment notification opens with a gazette
# reference such as "230 GI/2016" or "60 G.S.R. 380(E)", which the NUMBERED
# pattern would otherwise read as a clause number. These are document identity,
# not structure.
_GAZETTE_MARKER = re.compile(
    r"(?:\bGI\b|\bG\.?S\.?R\.?\b|\bS\.?O\.?\b|\bE\.?G\.?S\.?\b|\bGazette\b"
    r"|/\s*\d{4}\b)",
    re.IGNORECASE,
)

_SENTENCE_MARKER = re.compile(
    r"\b(may|shall|must|should|would|can|cannot|is|are|was|were|be|been|"
    r"being|has|have|had|does|do|did|will)\b",
    re.IGNORECASE,
)


def _has_sentence_continuation(text: str, match_end: int) -> bool:
    """True if what follows the reference reads as prose rather than a title.

    A table row such as "Schedule VIII may be used in food in accordance with
    these regulations." matches the SCHEDULE pattern but is body text. A real
    heading is "Schedule VIII" or "Schedule VIII - Additives".
    """
    remainder = text[match_end:].strip(" \t-–—.:")
    if not remainder:
        return False
    words = remainder.split()
    if len(words) > _MAX_TITLE_WORDS:
        return True
    if _SENTENCE_MARKER.search(remainder):
        return True
    # Terminal punctuation after several words means a sentence, not a title.
    if remainder.endswith((".", ";", ",")) and len(words) > 2:
        return True
    return False


def _classify_heading(text: str) -> tuple[str, str | None] | None:
    """Return ``(kind, identifier)`` for a heading, or None.

    ``identifier`` is the verbatim regex capture. It is ``None`` when the
    heading carries a keyword but no number, e.g. a bare "SCHEDULE".
    """
    if _STRUCTURAL_WORD.match(text) is None and not re.match(r"^\d", text):
        return None

    # A gazette reference is a document identifier, never a section.
    if _GAZETTE_MARKER.search(text):
        return None

    # A heading rendered in a legacy font is not a usable section reference:
    # the "number" is unverified gibberish, and recording it would put a
    # fabricated-looking identifier on every chunk beneath it.
    if is_legacy_encoded(text):
        return None

    if has_broken_devanagari(text)[0]:
        return None

    for kind, pattern in _PATTERNS:
        match = pattern.match(text)
        if not match:
            continue

        # A heading references itself once. "Schedule I and Schedule II" is a
        # table cell or a cross-reference, not a heading.
        if len(_STRUCTURAL_WORD.findall(text)) > 1:
            return None

        if _has_sentence_continuation(text, match.end()):
            return None

        groups = [g for g in match.groups() if g]
        identifier = groups[0] if groups else None

        if kind == "NUMBERED":
            if not identifier:
                continue
            # A long "1. Some continuing sentence..." is a list item.
            if len(text) > 90:
                continue

        return kind, identifier
    return None


def _looks_like_heading(line: PageLine, text: str) -> bool:
    """Decide whether a visual line is a heading.

    Combines a structural test (does it look like a reference?) with a
    typographic test (is it visually distinguished from body text?). Requiring
    both keeps numbered *lists* out of the section structure, which is the main
    risk in regulation text where almost every clause is numbered.
    """
    stripped = text.strip()
    if not stripped or _PAGE_NUMBER.match(stripped):
        return False
    if len(stripped) > config.MAX_HEADING_CHARS:
        return False
    if len(stripped) < config.MIN_HEADING_CHARS:
        return False

    typographic = (
        line.is_bold
        or line.relative_size >= 1.08
        or (stripped.isupper() and any(ch.isalpha() for ch in stripped))
    )
    if not typographic:
        return False

    return _classify_heading(stripped) is not None


# --------------------------------------------------------------------------
# Section segmentation
# --------------------------------------------------------------------------
@dataclass
class Section:
    """A contiguous run of lines sharing one detected heading."""

    section: str | None
    subsection: str | None
    heading_text: str | None
    heading_kind: str | None
    page_start: int
    page_end: int
    items: list[tuple[int, PageLine]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(line.text) for _, line in self.items)


def detect_sections(lines_by_page: dict[int, list[PageLine]]) -> list[Section]:
    """Group lines into sections, splitting on detected headings.

    Lines before the first heading form an unnamed leading section. Its
    ``section`` is ``None``: a title page or contents list is not a section,
    and labelling it as one would be an invention.
    """
    sections: list[Section] = []
    first_page = min(lines_by_page) if lines_by_page else 1
    current = Section(
        section=None,
        subsection=None,
        heading_text=None,
        heading_kind=None,
        page_start=first_page,
        page_end=first_page,
    )

    for page_number in sorted(lines_by_page):
        for line in lines_by_page[page_number]:
            classified = None
            if _looks_like_heading(line, line.text):
                classified = _classify_heading(line.text.strip())

            if classified is not None:
                kind, identifier = classified
                if current.items:
                    current.page_end = page_number
                    sections.append(current)
                current = Section(
                    section=identifier,
                    subsection=None,
                    heading_text=line.text.strip(),
                    heading_kind=kind,
                    page_start=page_number,
                    page_end=page_number,
                )
                # The heading line joins its own section, so chunk text is
                # self-describing rather than starting mid-thought.
                current.items.append((page_number, line))
                continue

            current.items.append((page_number, line))

    if current.items:
        current.page_end = max(p for p, _ in current.items)
        sections.append(current)
    return sections


# --------------------------------------------------------------------------
# Chunk construction
# --------------------------------------------------------------------------
@dataclass
class _Piece:
    """A unit of text with the page it came from and its token weight."""

    page_number: int
    text: str
    tokens: int


def _split_oversized_line(page_number: int, text: str, max_tokens: int) -> list[_Piece]:
    """Split one very long line on sentence boundaries.

    Only reached for pathological input such as a single unbroken table row.
    Splitting happens on sentence terminators, never mid-word, so no chunk ever
    contains a truncated word.
    """
    total = config.estimate_tokens(text)
    if total <= max_tokens:
        return [_Piece(page_number, text, total)]

    sentences = re.split(r"(?<=[.!?;])\s+", text)
    pieces: list[_Piece] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if config.estimate_tokens(candidate) > max_tokens and buffer:
            pieces.append(_Piece(page_number, buffer, config.estimate_tokens(buffer)))
            buffer = sentence
        else:
            buffer = candidate
    if buffer:
        pieces.append(_Piece(page_number, buffer, config.estimate_tokens(buffer)))
    return pieces


def _build_pieces(section: Section) -> list[_Piece]:
    """Flatten a section's lines into token-weighted pieces.

    Blank lines are skipped: they carry no text, so removing them cannot lose
    content, and they would otherwise inflate the token estimate.
    """
    pieces: list[_Piece] = []
    for page_number, line in section.items:
        text = line.text.rstrip()
        if not text.strip():
            continue
        pieces.extend(
            _split_oversized_line(page_number, text, config.CHUNK_MAX_TOKENS)
        )
    return pieces


def _overlap_tail(pieces: list[_Piece]) -> list[_Piece]:
    """Return the trailing pieces that form the overlap window.

    Walks backwards accumulating tokens until the budget is spent, then stops
    at a piece boundary. If even one piece exceeds the budget it is skipped, so
    a single huge line never becomes the entire overlap.
    """
    if config.CHUNK_OVERLAP_TOKENS <= 0 or not pieces:
        return []
    tail: list[_Piece] = []
    budget = config.CHUNK_OVERLAP_TOKENS
    for piece in reversed(pieces):
        if piece.tokens > budget:
            break
        tail.insert(0, piece)
        budget -= piece.tokens
        if budget <= 0:
            break
    return tail


def _make_chunk(
    document_id: str,
    provenance: dict,
    section: Section,
    pieces: list[_Piece],
    chunk_index: int,
    overlaps_previous: bool,
    ocr_pages: set[int],
    source_sha256: str | None,
) -> ChunkRecord:
    """Assemble one :class:`ChunkRecord` from pieces, validating page order."""
    text = "\n".join(piece.text for piece in pieces).strip()
    page_start = min(piece.page_number for piece in pieces)
    page_end = max(piece.page_number for piece in pieces)

    if page_end < page_start:
        # Defensive: with a non-empty piece list this cannot happen, but an
        # invalid range would be silent provenance corruption downstream.
        raise ValueError(
            f"page_end {page_end} < page_start {page_start} for chunk {chunk_index}"
        )

    covered = sorted({p for p in range(page_start, page_end + 1) if p in ocr_pages})

    fields = {k: v for k, v in provenance.items() if k != "local_path"}
    return ChunkRecord(
        chunk_id=chunk_id_for(
            document_id, chunk_index, section.section, section.subsection
        ),
        document_id=document_id,
        page_start=page_start,
        page_end=page_end,
        chunk_index=chunk_index,
        text=text,
        local_path=provenance["local_path"],
        source_sha256=source_sha256,
        section=section.section,
        subsection=section.subsection,
        heading_text=section.heading_text,
        token_estimate=config.estimate_tokens(text),
        char_count=len(text),
        word_count=len(text.split()),
        overlaps_previous=overlaps_previous,
        ocr_page_numbers=covered,
        **fields,
    ).validate_page_order()


def chunk_document(
    document_id: str,
    lines_by_page: dict[int, list[PageLine]],
    manifest_entry: dict,
    local_path: str,
    source_sha256: str | None = None,
    ocr_pages: set[int] | None = None,
) -> tuple[list[ChunkRecord], list[Section]]:
    """Chunk one document. Returns ``(chunks, sections)``."""
    ocr_pages = ocr_pages or set()
    sections = detect_sections(lines_by_page)

    provenance = {
        "authority": manifest_entry.get("authority"),
        "jurisdiction": manifest_entry.get("jurisdiction"),
        "document_title": manifest_entry.get("title"),
        "source_type": manifest_entry.get("source_type"),
        "category": manifest_entry.get("category"),
        "version": manifest_entry.get("version"),
        "effective_date": manifest_entry.get("effective_date"),
        "source_url": manifest_entry.get("source_url"),
        "document_url": manifest_entry.get("document_url"),
        "local_path": local_path,
    }

    chunks: list[ChunkRecord] = []
    chunk_index = 0
    # Carried across the section loop on purpose: a section boundary resets the
    # overlap window, so no chunk inherits text from a different section.
    carry: list[_Piece] = []

    for section in sections:
        pieces = _build_pieces(section)
        if not pieces:
            continue

        # Unnamed leading text has no section identity. Carrying an overlap
        # into a chunk that has a section number would attribute unnumbered
        # text to that section, so the carry is dropped here.
        if section.section is None:
            carry = []
        elif carry:
            pieces = carry + pieces

        start = 0
        last_window: list[_Piece] = []
        while start < len(pieces):
            end = start
            total = 0
            while end < len(pieces) and total < config.CHUNK_TARGET_TOKENS:
                total += pieces[end].tokens
                end += 1

            window = pieces[start:end]
            if not window:
                break

            chunks.append(
                _make_chunk(
                    document_id,
                    provenance,
                    section,
                    window,
                    chunk_index,
                    overlaps_previous=start > 0,
                    ocr_pages=ocr_pages,
                    source_sha256=source_sha256,
                )
            )
            chunk_index += 1
            last_window = window

            if end >= len(pieces):
                start = end
            else:
                tail = _overlap_tail(window)
                # max(start + 1, ...) guarantees forward progress even if the
                # overlap window is larger than the window it came from.
                start = max(start + 1, end - len(tail))

        carry = _overlap_tail(last_window) if last_window else []

    return chunks, sections
