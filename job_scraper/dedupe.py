"""Deduplication layer for the job_scraper pipeline.

Takes a batch of normalised ``Listing`` dicts from any source connector
and returns only the ones that are genuinely new — filtering against
both the persistent seen-jobs ledger and duplicates within the current
batch itself.

Two dedup passes are applied in sequence:

1. **ID dedup** — any listing whose ``id`` already appears as a key in
   ``seen_jobs.json`` is dropped immediately.

2. **Fingerprint dedup** — the remaining listings are checked against
   each other (and against the stored ledger) using a normalised
   ``(company, title)`` fingerprint: lowercase, whitespace-collapsed,
   common corporate suffixes stripped.  This catches the same job
   re-posted with a different ID/URL.

After filtering, every surviving listing is written into
``seen_jobs.json`` with ``status: "new"`` so it will be picked up by
Phase 5 (ranking) and excluded on future runs.

Public API::

    from job_scraper.dedupe import dedup

    new_listings = dedup(listings)           # uses default seen_jobs.json
    new_listings = dedup(listings, path=p)   # explicit path (testing / CI)

The file is also compatible with ``tools/rank_state.py``: entries written
here carry the same fields (``status``, ``title``, ``company``, ``url``,
``portal``, ``posted_date``) that ``rank_state.py candidates`` reads.
"""

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from job_scraper.sources import Listing

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SEEN = _REPO_ROOT / "job_scraper" / "seen_jobs.json"

if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import tempfile
    _TMP_BASE = Path(tempfile.gettempdir()) / "autojobapply"
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    _DEFAULT_SEEN = _TMP_BASE / "seen_jobs.json"

# ---------------------------------------------------------------------------
# Corporate suffix stripping (order matters — longer first)
# ---------------------------------------------------------------------------
_SUFFIX_RE = re.compile(
    r"\b("
    r"incorporated|corporation|limited|company|group|holding|holdings|"
    r"international|global|solutions|services|consulting|technologies|"
    r"technology|systems|software|partners|ventures|"
    r"inc\.?|ltd\.?|llc\.?|llp\.?|plc\.?|a/s|aps|gmbh|s\.a\.?|b\.v\.?|"
    r"ag|nv|sa|srl|oy|ab"
    r")\b\.?",
    re.IGNORECASE,
)


def _fingerprint(company: str | None, title: str | None) -> str:
    """Return a normalised ``(company, title)`` fingerprint for fuzzy dedup.

    Steps:
    1. NFC-normalise Unicode so accented chars compare correctly.
    2. Casefold.
    3. Strip corporate suffixes.
    4. Collapse all non-alphanumeric runs to a single space.
    5. Strip leading/trailing whitespace.
    """
    def _norm(text: str) -> str:
        text = unicodedata.normalize("NFC", text).casefold()
        text = _SUFFIX_RE.sub(" ", text)
        text = re.sub(r"[^a-z0-9\u00c0-\u024f]+", " ", text)
        return text.strip()

    c = _norm(company or "")
    t = _norm(title or "")
    return f"{c}|{t}"


# ---------------------------------------------------------------------------
# Ledger I/O  (schema matches tools/rank_state.py exactly)
# ---------------------------------------------------------------------------

def _load_ledger(path: Path) -> tuple[dict, dict]:
    """Return ``(document, seen_map)``.

    Creates ``{ "seen": {} }`` if the file does not exist.
    Raises ``json.JSONDecodeError`` if the file exists but is corrupt.
    """
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict = {"seen": {}}
        _save_ledger(path, doc)
        return doc, doc["seen"]

    doc = json.loads(path.read_text(encoding="utf-8"))
    # Support both the wrapped {"seen": {...}} form and a bare dict
    seen = doc.get("seen") if isinstance(doc, dict) and "seen" in doc else doc
    if not isinstance(seen, dict):
        raise ValueError(f"{path}: expected a JSON object under 'seen'")
    return doc, seen


def _save_ledger(path: Path, doc: dict) -> None:
    """Atomic write — matches the pattern in ``tools/rank_state.py``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".seen_jobs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _entry_from_listing(listing: Listing, seen_at: str) -> dict:
    """Build a ``seen_jobs.json`` entry from a ``Listing``.

    Fields match what ``tools/rank_state.py`` reads:
    - ``status``      — always ``"new"`` on first insertion
    - ``title``       — from listing
    - ``company``     — from listing
    - ``url``         — from listing
    - ``portal``      — mapped from ``listing["source"]``
    - ``posted_date`` — from listing
    - ``seen_at``     — ISO timestamp of when this pipeline run recorded it
    """
    return {
        "status": "new",
        "title": listing["title"],
        "company": listing["company"],
        "url": listing["url"],
        "portal": listing["source"],   # rank_state.py reads "portal", not "source"
        "posted_date": listing["posted_date"],
        "seen_at": seen_at,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dedup(
    listings: Sequence[Listing],
    *,
    path: Path | None = None,
) -> list[Listing]:
    """Filter *listings* to only genuinely new jobs and persist their IDs.

    Pass 1 — ID dedup against the ledger:
        Any listing whose ``id`` is already a key in ``seen_jobs.json`` is
        dropped.

    Pass 2 — Fingerprint dedup within the surviving batch (and against the
        ledger):
        Compute a normalised ``(company, title)`` fingerprint.  If two
        listings in the batch share a fingerprint, keep only the first.
        Also drop any listing whose fingerprint matches one already stored
        in the ledger (catches re-posts that received a new ID).

    After both passes, every surviving listing is written into the ledger
    with ``status: "new"`` and a UTC ``seen_at`` timestamp.

    Args:
        listings: Normalised ``Listing`` dicts from any source connector.
        path:     Path to ``seen_jobs.json``.  Defaults to
                  ``job_scraper/seen_jobs.json`` at the repo root.

    Returns:
        List of ``Listing`` dicts that are new — safe to pass to the
        ranking phase.
    """
    ledger_path = Path(path) if path is not None else _DEFAULT_SEEN
    doc, seen = _load_ledger(ledger_path)

    # Build the fingerprint index for everything already in the ledger so
    # Pass 2 can check the batch against stored entries too.
    stored_fingerprints: set[str] = {
        _fingerprint(entry.get("company"), entry.get("title"))
        for entry in seen.values()
        if isinstance(entry, dict)
    }

    seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_listings: list[Listing] = []
    batch_fingerprints: set[str] = set()

    for listing in listings:
        lid = listing["id"]

        # Pass 1: ID already in ledger
        if lid in seen:
            continue

        # Pass 2: fingerprint match — against both stored and current batch
        fp = _fingerprint(listing.get("company"), listing.get("title"))
        if fp in stored_fingerprints or fp in batch_fingerprints:
            continue

        # Survived both passes — record it
        new_listings.append(listing)
        batch_fingerprints.add(fp)
        seen[lid] = _entry_from_listing(listing, seen_at)

    if new_listings:
        _save_ledger(ledger_path, doc)

    return new_listings
