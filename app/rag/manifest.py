"""Phase 4 — normalise and validate the manifest.

Two rules dominate this module:

* The original ``manifest.json`` is **never** modified or overwritten. Every
  derived artefact is a new file.
* A value is only *cleaned* when cleaning is unambiguous. A URL that cannot be
  parsed is removed and reported — it is never guessed, repaired or replaced
  with a guessed one.

A value that is simply unknown stays ``null``. Missing metadata is recorded as
missing, never filled in.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import config
from .ids import document_id_for
from .models import ManifestEntry

log = logging.getLogger(__name__)

# Trailing characters that are almost always sentence punctuation that landed
# in the field, not part of the URL.
_TRAILING_JUNK = ".,;:'\")]}>"

# A date that is not ISO-8601 is reported, not rewritten: guessing an ISO date
# from "13th December 2022" would be an interpretation, not a normalisation.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# URL normalisation
# --------------------------------------------------------------------------
def normalize_url(value: object) -> tuple[str | None, list[str]]:
    """Clean a URL, returning ``(cleaned, notes)``.

    Handles the wrappers that arrive when a URL is copied out of Markdown or
    an HTML page: ``[label](url)``, ``<url>``, and backtick fences. Notes record
    what was changed so the report can show that the cleaning was mechanical.
    """
    notes: list[str] = []
    if value is None:
        return None, notes
    if not isinstance(value, str):
        return None, ["value is not a string"]

    url = value.strip()
    if not url:
        return None, ["empty value"]

    original = url

    # Markdown inline link: [label](url)
    md = re.fullmatch(r"\[[^\]]*\]\(\s*([^)]*?)\s*\)", url)
    if md:
        url = md.group(1).strip()
        notes.append("unwrapped markdown link syntax")

    # HTML autolink
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()
        notes.append("unwrapped angle brackets")

    # Markdown code fence
    if len(url) >= 2 and url[0] == "`" and url[-1] == "`":
        url = url[1:-1].strip()
        notes.append("unwrapped backticks")

    # Leading/trailing whitespace picked up from markup
    url = url.strip()

    # Sentence punctuation at the end
    stripped = url.rstrip(_TRAILING_JUNK)
    if stripped != url and stripped:
        url = stripped
        notes.append("removed trailing punctuation")

    if not url:
        return None, notes + ["empty after normalisation"]

    parsed = urlparse(url)
    if parsed.scheme.lower() not in config.ALLOWED_URL_SCHEMES:
        notes.append(f"unsupported or missing URL scheme: {parsed.scheme or '(none)'}")
        return None, notes
    if not parsed.netloc:
        notes.append("URL has no host component")
        return None, notes

    if url != original and not notes:
        notes.append("whitespace trimmed")

    return url, notes


# --------------------------------------------------------------------------
# Path normalisation
# --------------------------------------------------------------------------
def normalize_local_path(value: object) -> tuple[str | None, list[str]]:
    """Clean a local path, returning ``(cleaned, notes)``.

    Absolute paths are converted to backend-relative form so the manifest is
    portable. Paths that escape the backend root, or that point into the
    quarantine tree, are rejected outright.
    """
    notes: list[str] = []
    if value is None:
        return None, notes
    if not isinstance(value, str):
        return None, ["value is not a string"]

    raw = value.strip()
    if not raw:
        return None, ["empty value"]

    # Same Markdown wrappers a URL can carry.
    md = re.fullmatch(r"\[[^\]]*\]\(\s*([^)]*?)\s*\)", raw)
    if md:
        raw = md.group(1).strip()
        notes.append("unwrapped markdown link syntax")
    if len(raw) >= 2 and raw[0] == "`" and raw[-1] == "`":
        raw = raw[1:-1].strip()
        notes.append("unwrapped backticks")
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
        notes.append("unwrapped angle brackets")

    raw = raw.replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw:
        return None, notes + ["empty after normalisation"]

    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(config.BACKEND_ROOT.resolve())
        except ValueError:
            return None, notes + ["absolute path is outside the backend root"]
        notes.append("converted absolute path to backend-relative")
        raw = rel.as_posix()

    # Normalise away any ``..`` segments and confirm the result stays inside.
    resolved = (config.BACKEND_ROOT / raw).resolve()
    try:
        rel = resolved.relative_to(config.BACKEND_ROOT.resolve())
    except ValueError:
        return None, notes + ["path escapes the backend root"]

    if config.QUARANTINED_DIRNAME in resolved.parts:
        return None, notes + ["path points into the quarantine tree"]

    final = rel.as_posix()
    if final != raw:
        notes.append("normalised path segments")
    return final, notes


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _load_manifest(path: Path) -> list[dict]:
    """Load the manifest, tolerating either a bare list or a wrapped object."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("documents", "entries", "items", "manifest", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        raise ValueError(
            f"manifest is an object but has no recognised entry list; keys={list(data)}"
        )
    raise ValueError(f"unsupported manifest root type: {type(data).__name__}")


def validate_manifest(manifest_path: Path | None = None) -> dict:
    """Validate and clean the manifest.

    Returns a report dict that also carries the cleaned entries under
    ``clean_entries`` so the caller can persist them without re-parsing.
    """
    path = manifest_path or config.MANIFEST_PATH
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")

    raw_entries = _load_manifest(path)
    log.info("loaded %d manifest entries from %s", len(raw_entries), path.name)

    clean_entries: list[dict] = []
    dropped: list[dict] = []
    missing_files: list[dict] = []
    warnings: list[dict] = []
    counts: Counter[str] = Counter()
    urls_normalized: list[dict] = []
    paths_normalized: list[dict] = []
    field_presence: Counter[str] = Counter()
    effective_dates_non_iso: list[dict] = []
    seen_ids: dict[str, int] = {}
    duplicate_document_urls: dict[str, list[str]] = {}

    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            dropped.append(
                {"index": index, "reason": "entry is not an object", "entry": repr(raw)[:120]}
            )
            continue

        entry = ManifestEntry(**raw)
        for field in config.REQUIRED_MANIFEST_FIELDS:
            if getattr(entry, field, None):
                field_presence[field] += 1

        notes: list[str] = []
        problems: list[str] = []

        # --- URLs -------------------------------------------------------
        for field in ("source_url", "document_url"):
            cleaned, url_notes = normalize_url(getattr(entry, field, None))
            for note in url_notes:
                if "unwrapped" in note or "trailing" in note or "whitespace" in note:
                    urls_normalized.append(
                        {"index": index, "field": field, "note": note, "title": entry.title}
                    )
                else:
                    notes.append(f"{field}: {note}")

            if getattr(entry, field, None) and cleaned is None:
                problems.append(f"{field} is invalid and was removed")
            if cleaned is not None:
                raw[field] = cleaned
            elif field in raw:
                # Remove rather than keep an unusable value. No replacement is
                # invented; the entry keeps a null until a real URL is known.
                raw.pop(field, None)

        # --- local path -------------------------------------------------
        cleaned_path, path_notes = normalize_local_path(entry.local_path)
        for note in path_notes:
            paths_normalized.append(
                {"index": index, "note": note, "title": entry.title}
            )
        if entry.local_path and cleaned_path is None:
            dropped.append(
                {
                    "index": index,
                    "reason": "; ".join(note for note in path_notes if note)
                    or "invalid local_path",
                    "title": entry.title,
                    "local_path": entry.local_path,
                }
            )
            continue
        if cleaned_path is not None and cleaned_path != entry.local_path:
            notes.append("local_path normalised")
        raw["local_path"] = cleaned_path

        # --- required fields --------------------------------------------
        missing_fields = [
            field
            for field in config.REQUIRED_MANIFEST_FIELDS
            if getattr(entry, field, None) in (None, "")
        ]
        # local_path/document_url were already handled above.
        missing_fields = [f for f in missing_fields if f not in ("local_path", "document_url")]
        if missing_fields:
            notes.append("missing fields: " + ", ".join(missing_fields))
            counts["entries_with_missing_fields"] += 1

        # --- value sanity (reported, never silently rewritten) -----------
        if entry.authority and entry.authority != config.AUTHORITY:
            warnings.append(
                {"index": index, "field": "authority", "value": entry.authority,
                 "note": f"expected {config.AUTHORITY}"}
            )
            counts["unexpected_authority"] += 1
        if entry.jurisdiction and entry.jurisdiction != config.JURISDICTION:
            warnings.append(
                {"index": index, "field": "jurisdiction", "value": entry.jurisdiction,
                 "note": f"expected {config.JURISDICTION}"}
            )
            counts["unexpected_jurisdiction"] += 1
        if entry.effective_date and not _ISO_DATE.match(str(entry.effective_date)):
            effective_dates_non_iso.append(
                {"index": index, "value": str(entry.effective_date), "title": entry.title}
            )
            counts["non_iso_effective_date"] += 1

        # --- referential check ------------------------------------------
        file_exists = False
        if cleaned_path:
            absolute = config.BACKEND_ROOT / cleaned_path
            file_exists = absolute.is_file()
            if not file_exists:
                missing_files.append(
                    {"index": index, "title": entry.title, "local_path": cleaned_path}
                )
                counts["missing_file"] += 1

        # --- identity ----------------------------------------------------
        doc_id = document_id_for(
            local_path=cleaned_path or "",
            title=entry.title,
            document_url=entry.document_url,
            source_url=entry.source_url,
        )
        raw["document_id"] = doc_id
        seen_ids[doc_id] = seen_ids.get(doc_id, 0) + 1
        raw["file_exists"] = file_exists
        raw["processing_status"] = "READY" if file_exists else "PENDING_DOWNLOAD"
        raw["validation_notes"] = notes
        if problems:
            raw["validation_warnings"] = problems

        if entry.document_url:
            duplicate_document_urls.setdefault(entry.document_url, []).append(str(entry.title))

        clean_entries.append(raw)

    duplicate_ids = {k: v for k, v in seen_ids.items() if v > 1}
    shared_urls = {
        url: titles
        for url, titles in duplicate_document_urls.items()
        if url != "<missing>" and len(titles) > 1
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": config.knowledge_relative(path),
        "original_entry_count": len(raw_entries),
        "clean_entry_count": len(clean_entries),
        "dropped_entry_count": len(dropped),
        "ready_for_processing": sum(
            1 for e in clean_entries if e.get("file_exists")
        ),
        "pending_download": sum(
            1 for e in clean_entries if not e.get("file_exists")
        ),
        "urls_normalized": len(urls_normalized),
        "paths_normalized": len(paths_normalized),
        "url_normalization_detail": urls_normalized,
        "path_normalization_detail": paths_normalized,
        "field_presence": {
            field: f"{field_presence.get(field, 0)}/{len(raw_entries)}"
            for field in config.REQUIRED_MANIFEST_FIELDS
        },
        "warnings": warnings,
        "issues": {
            "dropped_entries": dropped,
            "missing_files": missing_files,
            "non_iso_effective_dates": effective_dates_non_iso,
            "duplicate_document_ids": duplicate_ids,
            "document_urls_shared_by_multiple_entries": shared_urls,
        },
        "counts": dict(counts),
        "notes": [
            "manifest.json is read-only; all output is written to new files",
            "invalid URLs and unsafe paths were removed and reported, never replaced",
            "missing files are recorded as PENDING_DOWNLOAD, not as errors",
            "source_url is a portal/landing page and is reused across entries; "
            "document_url is the per-document locator",
        ],
        "clean_entries": clean_entries,
    }
    return report


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------
def write_manifest_clean(report: dict, path: Path | None = None) -> Path:
    target = path or config.MANIFEST_CLEAN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = report.get("clean_entries", [])
    payload = {
        "generated_at": report["generated_at"],
        "source_manifest": report["manifest_path"],
        "authority": config.AUTHORITY,
        "jurisdiction": config.JURISDICTION,
        "entry_count": len(entries),
        "entries": entries,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote clean manifest -> %s", config.knowledge_relative(target))
    return target


def write_manifest_validation_report(report: dict, path: Path | None = None) -> Path:
    target = path or config.MANIFEST_VALIDATION_REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {k: v for k, v in report.items() if k != "clean_entries"}
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("wrote manifest validation report -> %s", config.knowledge_relative(target))
    return target


# --------------------------------------------------------------------------
# File <-> manifest binding
# --------------------------------------------------------------------------
# The extraction unit is the *file on disk*, not the manifest row. These two
# are not always in one-to-one correspondence, and when they are not, the file
# is authoritative for content and the manifest is advisory for provenance.
#
# Reason, observed in this collection: the downloader derives a filename from a
# slug of the title, and truncates it. Two different upstream PDFs can therefore
# collide on one filename, and the later download silently overwrites the
# earlier one. The manifest then advertises one file as several distinct
# documents, each with its own document_url. Extracting per manifest row would
# duplicate the same bytes under several document IDs while claiming several
# different upstream origins for them.
#
# Resolution used here, deliberately conservative:
#   * extract each file exactly once
#   * bind it to one canonical manifest row, chosen deterministically
#   * retain every other claimant as an alias, flagged for human review
#   * never guess which document_url is the true origin, never re-download
#     anything, and never rewrite the manifest


def _url_tokens(url: str | None) -> set[str]:
    if not url:
        return set()
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.(pdf|doc|docx)$", "", name, flags=re.IGNORECASE)
    return {t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if len(t) > 2}


def _file_tokens(local_path: str) -> set[str]:
    name = local_path.rsplit("/", 1)[-1]
    return {t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if len(t) > 2}


def _canonical_score(entry: dict, file_token_set: set[str]) -> tuple:
    """Rank a claimant for canonicity. Higher sorts first.

    The primary signal is how much of the upstream filename is reflected in the
    stored filename. This is a defensible, reproducible tie-break, not a guess
    about content: it says "the file name looks like it came from this URL".
    """
    tokens = _url_tokens(entry.get("document_url")) | _url_tokens(entry.get("source_url"))
    overlap = len(tokens & file_token_set)
    return (
        overlap,
        1 if entry.get("document_url") else 0,
        1 if entry.get("effective_date") else 0,
    )


def build_document_bindings(clean_entries: list[dict]) -> tuple[list[dict], dict]:
    """Bind each on-disk file to exactly one canonical manifest entry.

    Returns ``(bindings, collision_report)``. A binding is the unit of work for
    extraction.
    """
    ready = [e for e in clean_entries if e.get("file_exists")]
    groups: dict[str, list[dict]] = {}
    for entry in ready:
        groups.setdefault(entry["local_path"], []).append(entry)

    bindings: list[dict] = []
    collisions: list[dict] = []

    for local_path in sorted(groups):
        claimants = groups[local_path]
        file_token_set = _file_tokens(local_path)

        ordered = sorted(
            claimants,
            key=lambda e: (
                tuple(-v for v in _canonical_score(e, file_token_set)),
                e.get("document_url") or "",
                e.get("document_id") or "",
            ),
        )
        canonical = ordered[0]
        aliases = [c for c in ordered[1:]]

        binding = {
            "document_id": canonical["document_id"],
            "local_path": local_path,
            "absolute_path": config.BACKEND_ROOT / local_path,
            "manifest_entry": canonical,
            "alias_claimants": [
                {
                    "document_id": a.get("document_id"),
                    "title": a.get("title"),
                    "source_type": a.get("source_type"),
                    "category": a.get("category"),
                    "document_url": a.get("document_url"),
                    "source_url": a.get("source_url"),
                    "effective_date": a.get("effective_date"),
                }
                for a in aliases
            ],
            "has_collision": bool(aliases),
            "canonical_selection": {
                "rule": "highest filename-token overlap with document_url; "
                        "then document_url present; then effective_date present; "
                        "then lexicographic",
                "score": list(_canonical_score(canonical, file_token_set)),
            },
        }
        bindings.append(binding)

        if aliases:
            collisions.append(
                {
                    "local_path": local_path,
                    "claimant_count": len(claimants),
                    "canonical_document_id": canonical["document_id"],
                    "canonical_title": canonical.get("title"),
                    "canonical_document_url": canonical.get("document_url"),
                    "alias_document_ids": [a.get("document_id") for a in aliases],
                    "alias_document_urls": [a.get("document_url") for a in aliases],
                    "resolution": "extracted once under the canonical document_id; "
                                  "aliases retained for manual review",
                    "action_required": "human review: confirm which upstream document "
                                       "this file actually is, and re-download the "
                                       "others to distinct filenames",
                }
            )

    collision_report = {
        "files_with_multiple_manifest_claimants": len(collisions),
        "extra_claimants_suppressed": sum(c["claimant_count"] - 1 for c in collisions),
        "collisions": collisions,
    }
    log.info(
        "bound %d files; %d had multiple manifest claimants (%d aliases suppressed)",
        len(bindings),
        len(collisions),
        collision_report["extra_claimants_suppressed"],
    )
    return bindings, collision_report
