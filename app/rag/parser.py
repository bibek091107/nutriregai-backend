"""Phase 5 and 6 — page-level text extraction and OCR-candidate detection.

Extraction contract
-------------------
* Every page of a valid PDF produces exactly one :class:`PageRecord`.
  A page is never dropped, not even when it yields no text at all; an empty
  page is a *finding* (``needs_ocr``), not a failure.
* Extracted text is preserved as-is apart from whitespace normalisation of line
  endings. No words are added, removed, reordered or "corrected".
* A page that raises during extraction still yields a record carrying
  ``extraction_error``, so a single bad page cannot lose the other 152.

OCR detection
-------------
This stage only *detects*. It does not call tesseract, does not shell out, and
does not attempt to recover text from a scan. It reports which pages would
benefit, so OCR can be run as a separate, auditable step over a known list.

Three independent signals are recorded, so a reviewer can see *why* a page was
flagged rather than trusting a single opaque boolean:

``low_text``
    Too little text for the page. A blank or near-blank page in a gazette is a
    scan.
``image_dominated``
    Embedded images cover a large fraction of the page area, which is what a
    scanned page looks like structurally.
``mojibake``
    Text *was* extracted, but it is not usable. FSSAI gazettes frequently embed
    Devanagari in legacy non-Unicode fonts, which extract as plausible-looking
    Latin gibberish such as ``jftLVh lao Mho ,y&33004@99``. Such a page yields
    a confident-looking non-empty string that would poison a knowledge base, and
    a naive emptiness test misses it entirely.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .models import PageRecord

log = logging.getLogger(__name__)

# Fonts whose names indicate a bold weight. Used because some generators emit
# the weight in the font name and do not set the bold flag.
_BOLD_NAME = re.compile(r"bold|black|heavy|semib|demib", re.IGNORECASE)

# Characters that are legal in English regulatory text but rare, so they cannot
# on their own prove a page is garbled. Mapped to a plain letter so the
# legacy-encoding test ignores them entirely.
_COMMON_EXTRA = (
    "°–—‘’“”•−×≥≤≈€™®© "
    "‐‑‒―−"
    "µÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜßàáâãäåæçèéêëìíîïñòóôõöøùúûüýÿ"
)
# Only the non-ASCII members of the whitelist need translating; ASCII members
# are already letters and need no special handling.
_LEGIT_TYPOGRAPHIC = {ord(ch): "A" for ch in _COMMON_EXTRA if ord(ch) > 127}

# Western code points: the range a legacy font encoding substitutes for a
# non-Latin script. A page dominated by these is mis-encoded; a page dominated
# by genuine Devanagari or Tamil is not.
_WESTERNISH = re.compile(
    r"[ -ÿĀ-ɏ -⁯₠-₿]"
)

_LATIN_SUPPLEMENT = re.compile(r"[^\x00-\x7F]")

# Scripts that have no business appearing alongside Devanagari in an FSSAI
# document. Their presence means the font substituted glyphs from an unrelated
# script, which is unrecoverable by re-mapping.
_FOREIGN_SCRIPTS = re.compile(
    r"[ᰀ-᰿᱀-᱿ᲀ-Ჿ"
    r"ក-៿ᜀ-᜿ឰ-៿"
    r"᠀-ᠿᦀ-᧿ᨀ-᩿"
    r"ᩰ-᩿᪀-᫿ᬀ-᭿"
    r"ᰀ-퟿豈-﫿]"
)


# Characters that are line breaks, but which only ``\n`` and ``\r`` express.
#
# PDFs embed these freely, and one was observed in this collection: U+0085
# (NEL) inside a legacy-font gazette. They are a trap for JSONL. Python's
# ``str.splitlines()`` treats all of them as line boundaries while iterating a
# file handle does not, so a single U+0085 inside a chunk's text silently
# splits that chunk's JSON in half for any consumer that reads the file with
# ``read_text().splitlines()``. JavaScript has the same problem with U+2028.
#
# All of them are semantically line breaks, so mapping them to "\n" preserves
# the text exactly while making the output safe for line-oriented tooling.
_LINE_BOUNDARIES = {
    0x000B: "\n",  # LINE TABULATION
    0x000C: "\n",  # FORM FEED
    0x001C: "\n",  # FILE SEPARATOR
    0x001D: "\n",  # GROUP SEPARATOR
    0x001E: "\n",  # RECORD SEPARATOR
    0x0085: "\n",  # NEXT LINE
    0x2028: "\n",  # LINE SEPARATOR
    0x2029: "\n",  # PARAGRAPH SEPARATOR
}
_LINE_BOUNDARY_TABLE = dict(_LINE_BOUNDARIES)


def normalize_text(text: str) -> str:
    """Normalise line endings without altering any other content.

    Applied to every extracted page and every line that feeds the chunker, so
    a Unicode line separator can never reach a JSONL file.
    """
    if not text:
        return ""
    return text.translate(_LINE_BOUNDARY_TABLE)


# Devanagari-dependent vowel signs (matras) and the two danda-adjacent marks.
# A correct Devanagari syllable always begins with an independent vowel or a
# consonant, never with one of these.
_MATRA_CHARS = "".join(chr(code) for code in range(0x093A, 0x094E)) + "ऺऻ"
# Zero-width joiners, which appear in this collection only as artefacts of
# broken glyph reordering.
_JOINERS = "‌‍"

# A matra appearing where a syllable should start. The boundary class is
# whitespace plus the punctuation and digits that legitimately precede a new
# token, so a correctly formed sentence can never match.
_DETACHED_MATRA = re.compile(
    rf"(?:^|[\s\d.,;:()\[\]{{}}/\-–—।॥])[{re.escape(_MATRA_CHARS)}]"
)
_JOINER_PRESENT = re.compile(f"[{re.escape(_JOINERS)}]")


def has_broken_devanagari(text: str) -> tuple[bool, str | None]:
    """Detect Devanagari whose glyph ordering was destroyed by font encoding.

    FSSAI gazettes embed Hindi in legacy fonts that drop or displace the
    dependent vowel signs, producing text such as "कॅप्पन टुना" rendered as
    "कᳱपजैक टुना", and "शुद्ध" as " ुगिन्ध" with the matra detached and sitting
    after the space. The result is real Unicode that looks like Hindi to a
    casual reader and is meaningless to a search index or an embedding model.

    The decisive signal is a matra in word-initial position. That is impossible
    in well-formed Devanagari, so this test has no false positives on correct
    text, and it catches corruption that a character-set or frequency test
    misses because the codepoints are all legitimate.

    Returns ``(broken, reason)``.
    """
    if not text:
        return False, None

    devanagari = sum(1 for ch in text if 0x0900 <= ord(ch) <= 0x097F)
    # Require some Devanagari so an isolated stray glyph in English prose is
    # not mistaken for a broken-script document.
    if devanagari < 3:
        return False, None

    match = _DETACHED_MATRA.search(text)
    if match:
        start = max(match.start() - 12, 0)
        return True, (
            f"detached matra {match.group(0).strip()!r} at word start "
            f"(context: {text[start:match.end() + 12]!r})"
        )

    if _JOINER_PRESENT.search(text):
        return True, "zero-width joiner present, indicating broken glyph reordering"

    # A font that cannot map a glyph substitutes one from an unrelated script,
    # so correct Devanagari comes back peppered with Balinese or Javanese
    # characters. Re-mapping the encoding cannot undo this.
    foreign = _FOREIGN_SCRIPTS.search(text)
    if foreign:
        return True, (
            f"character {foreign.group(0)!r} from an unrelated script "
            f"(U+{ord(foreign.group(0)):04X}) substituted into Devanagari"
        )

    return False, None


def is_legacy_encoded(text: str) -> bool:
    """Heuristic detector for legacy-font gibberish.

    Conservative by design: it fires only when non-ASCII characters are both
    reasonably frequent and surrounded by ASCII letters, which is the signature
    of a mis-mapped non-Latin script rather than of a legitimately multilingual
    document (an actual Hindi or Tamil page extracts as real Unicode).

    Legitimate typographic characters are neutralised first. Without that, a
    short line such as "PART III—Section 4" trips the detector purely because
    an em dash is non-ASCII, and the surrounding letters make it look embedded
    in a word.
    """
    if not text:
        return False

    # Treat em dashes, curly quotes, degree signs and friends as ordinary
    # letters for the purpose of this test, so only genuinely foreign or
    # mis-mapped characters count towards the ratio.
    probe = text.translate(_LEGIT_TYPOGRAPHIC)

    non_ascii = [ch for ch in probe if ord(ch) > 127]
    if not non_ascii:
        return False
    ratio = len(non_ascii) / max(len(probe), 1)
    if ratio < 0.02:
        return False

    # Require the non-ASCII characters to sit inside runs of letters, i.e. they
    # are *inside* words rather than being standalone punctuation.
    inside_word = 0
    for match in re.finditer(r"[A-Za-z]+(?:[^\x00-\x7F][A-Za-z]?)+", probe):
        inside_word += len(match.group(0))
    if inside_word and inside_word / max(len(probe), 1) >= 0.02:
        return True

    # High overall non-ASCII density indicates a legacy encoding only when the
    # non-ASCII characters are *Western* code points standing in for another
    # script, which is how Krutidev and similar pre-Unicode Devanagari
    # encodings actually corrupt: "जमली" becomes "jml;" style Latin-1 text.
    #
    # A high density of genuine non-Latin script is not corruption. A page of
    # correct Hindi is entirely non-ASCII and perfectly readable, and flagging
    # it would be wrong. Genuine non-Latin scripts are assessed by
    # has_broken_devanagari instead, which tests for actual broken structure.
    if ratio < 0.35:
        return False
    western = sum(1 for ch in non_ascii if _WESTERNISH.match(ch))
    return western / max(len(non_ascii), 1) >= 0.5


@dataclass
class PageLine:
    """One visual text line, with the style signals used for heading detection.

    Carried alongside the page text rather than inside it, so the stored page
    text stays exactly what the PDF contains.
    """

    text: str
    is_bold: bool = False
    is_italic: bool = False
    font_size: float = 0.0
    page_max_font_size: float = 0.0

    @property
    def relative_size(self) -> float:
        """Font size as a fraction of the page's largest, for prominence tests."""
        if self.page_max_font_size <= 0:
            return 1.0
        return self.font_size / self.page_max_font_size


@dataclass
class DocumentParse:
    """Result of parsing one PDF."""

    document_id: str
    local_path: str
    page_count: int
    pages: list[PageRecord] = field(default_factory=list)
    lines_by_page: dict[int, list[PageLine]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def pages_needing_ocr(self) -> list[int]:
        return [p.page_number for p in self.pages if p.needs_ocr]

    @property
    def ok(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------
# Per-page extraction
# --------------------------------------------------------------------------
def _extract_page(page, page_number: int, document_id: str) -> tuple[PageRecord, list[PageLine]]:
    """Extract one page into a record plus its styled lines.

    Never raises: an extraction failure is reported on the record so the page
    is still represented and still counted.
    """
    record = PageRecord(
        document_id=document_id, page_number=page_number, text=""
    )
    lines: list[PageLine] = []

    try:
        # "text" mode gives reading order, which is what the chunker needs.
        record.text = page.get_text("text", sort=True) or ""
    except Exception as exc:
        record.extraction_error = f"text extraction failed: {type(exc).__name__}: {exc}"
        return record, lines

    # Normalise only line endings and strip trailing whitespace on each line.
    # Interior spacing, punctuation and wording are left untouched.
    text = normalize_text(record.text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    record.text = text

    try:
        raw = page.get_text("dict", sort=True)
    except Exception as exc:
        record.extraction_error = (
            f"style extraction failed: {type(exc).__name__}: {exc}"
        )
        return record, lines

    image_blocks = 0
    try:
        image_area = 0.0
        page_area = float(abs(page.rect.get_area())) or 1.0
    except Exception:
        image_area = 0.0
        page_area = 1.0

    max_font_size = 0.0
    raw_lines: list[PageLine] = []

    for block in raw.get("blocks", []):
        if block.get("type") == 1:  # image block
            image_blocks += 1
            try:
                x0, y0, x1, y1 = block["bbox"]
                image_area += abs((x1 - x0) * (y1 - y0))
            except Exception:
                pass
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = normalize_text(
                "".join(span.get("text", "") for span in spans)
            ).rstrip()
            if not line_text.strip():
                continue
            sizes = [float(span.get("size", 0.0)) for span in spans]
            font_size = max(sizes) if sizes else 0.0
            max_font_size = max(max_font_size, font_size)
            bold = any(
                bool(span.get("flags", 0) & 2 ** 4)
                or bool(_BOLD_NAME.search(str(span.get("font", ""))))
                for span in spans
            )
            italic = any(
                bool(span.get("flags", 0) & 2 ** 1) for span in spans
            )
            raw_lines.append(
                PageLine(
                    text=line_text,
                    is_bold=bold,
                    is_italic=italic,
                    font_size=font_size,
                )
            )

    for line in raw_lines:
        line.page_max_font_size = max_font_size
    lines.extend(raw_lines)

    record.image_count = image_blocks
    record.image_area_ratio = min(image_area / page_area, 1.0) if image_area else 0.0
    record.text_length = len(text)
    record.char_count = len(text)
    record.word_count = len(text.split())

    return record, lines


def _flag_ocr_candidate(record: PageRecord) -> PageRecord:
    """Apply the three OCR signals. Mutates and returns the record."""
    reasons: list[str] = []
    stripped = record.text.strip()

    if record.text_length < config.OCR_MIN_TEXT_CHARS:
        reasons.append(
            f"low_text: {record.text_length} characters "
            f"< {config.OCR_MIN_TEXT_CHARS}"
        )

    if record.image_count >= config.OCR_IMAGE_COUNT_WITHOUT_TEXT and (
        record.image_area_ratio >= config.OCR_IMAGE_AREA_RATIO
    ):
        reasons.append(
            f"image_dominated: {record.image_count} image(s) covering "
            f"{record.image_area_ratio:.0%} of the page"
        )

    if is_legacy_encoded(stripped):
        non_ascii = sum(1 for ch in stripped if ord(ch) > 127)
        reasons.append(
            f"mojibake: {non_ascii} non-ASCII characters "
            f"({non_ascii / max(len(stripped), 1):.1%}) consistent with "
            f"legacy-font encoding rather than readable text"
        )

    broken, broken_reason = has_broken_devanagari(stripped)
    if broken:
        reasons.append(f"broken_devanagari: {broken_reason}")

    if record.extraction_error:
        reasons.append(f"extraction_error: {record.extraction_error}")

    record.ocr_reasons = reasons
    record.needs_ocr = bool(reasons)
    return record


# --------------------------------------------------------------------------
# Document-level entry point
# --------------------------------------------------------------------------
def parse_document(
    path: Path | str,
    document_id: str,
    local_path: str | None = None,
) -> DocumentParse:
    """Parse every page of one PDF.

    A document-level failure (unopenable file, encryption) is returned as an
    ``error`` on the result rather than raised, so the caller can record it and
    continue with the next document.
    """
    import pymupdf

    path = Path(path)
    local_path = local_path or config.knowledge_relative(path)
    result = DocumentParse(
        document_id=document_id, local_path=local_path, page_count=0
    )

    if config.is_quarantined(path):
        result.error = "refusing to parse a file inside the quarantine tree"
        return result

    try:
        document = pymupdf.open(path)
    except Exception as exc:
        result.error = f"cannot open PDF: {type(exc).__name__}: {exc}"
        return result

    try:
        if document.needs_pass:
            result.error = "PDF is password protected"
            return result

        result.page_count = document.page_count
        for index in range(document.page_count):
            page_number = index + 1
            try:
                page = document.load_page(index)
            except Exception as exc:
                # The page is preserved as a record so the page count in the
                # document record still matches the real PDF.
                record = PageRecord(
                    document_id=document_id,
                    page_number=page_number,
                    extraction_error=f"cannot load page: {type(exc).__name__}: {exc}",
                )
                result.pages.append(_flag_ocr_candidate(record))
                result.warnings.append(f"page {page_number} could not be loaded")
                continue

            record, lines = _extract_page(page, page_number, document_id)
            record = _flag_ocr_candidate(record)
            result.pages.append(record)
            result.lines_by_page[page_number] = lines

            if record.extraction_error:
                result.warnings.append(
                    f"page {page_number}: {record.extraction_error}"
                )
    finally:
        try:
            document.close()
        except Exception:
            pass

    return result


def pages_to_jsonl_records(pages: list[PageRecord]) -> list[dict]:
    """Serialise page records for ``processed/pages/<document_id>.jsonl``.

    Only the page-level fields are written. Styled lines are an extraction
    intermediate and are not persisted; the chunker consumes them in memory and
    the chunk text already carries the content.
    """
    return [page.model_dump() for page in pages]
