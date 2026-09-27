"""LinkedIn job source connector.

Wraps the existing Bun/TypeScript CLI at
``.agents/skills/linkedin-search/cli/src/cli.ts`` as a subprocess and
normalises its output to the pipeline's common ``Listing`` shape.

No LinkedIn credentials or API key are required — the CLI uses LinkedIn's
public ``jobs-guest`` endpoints. Personal use only; keep request volume
low (LinkedIn ToS).

Public API::

    from job_scraper.sources.linkedin import fetch

    listings = fetch({"role": "data engineer", "location": "Berlin, Germany"})
    # -> list[Listing]

    # Or let it derive the query from job_scraper/profile.json automatically:
    listings = fetch()
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from job_scraper.sources import Listing

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_ENTRY = _REPO_ROOT / ".agents" / "skills" / "linkedin-search" / "cli" / "src" / "cli.ts"
_DEFAULT_PROFILE = _REPO_ROOT / "job_scraper" / "profile.json"

SOURCE_NAME = "linkedin"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LinkedInError(Exception):
    """Raised when the LinkedIn CLI exits with a non-zero code or returns
    unparseable output."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_bun() -> str:
    """Return the path to the ``bun`` executable, or raise clearly."""
    bun = shutil.which("bun")
    if bun is None:
        raise LinkedInError(
            "bun is not installed or not on PATH. "
            "Install it from https://bun.sh and re-run."
        )
    return bun


def _build_query(profile: dict) -> dict:
    """Derive a ``{role, location}`` query from a loaded profile dict.

    Uses ``titles_held[0]`` as the role keyword and ``location_pref`` as the
    location string.  Both fall back to empty string when absent so the caller
    can always pass the result straight to ``_run_cli``.
    """
    titles = profile.get("titles_held") or []
    role = titles[0] if titles else ""
    location = profile.get("location_pref") or ""
    return {"role": role, "location": location}



def _run_cli(
    role: str,
    location: str,
    *,
    jobage: int = 30,
    limit: int = 25,
) -> list[dict]:
    """Invoke the LinkedIn Bun CLI and return the raw list of job cards.

    Args:
        role:     Keyword query (job title or skill).
        location: LinkedIn place string, e.g. ``"Berlin, Germany"``.
        jobage:   Posted within N days (passed as ``--jobage``).
        limit:    Maximum cards returned (passed as ``--limit``).

    Returns:
        List of raw dicts from the CLI's JSON output.

    Raises:
        LinkedInError: CLI not found, exits non-zero, or returns bad JSON.
    """
    bun = _require_bun()

    cmd = [
        bun, "run", str(_CLI_ENTRY),
        "search",
        "--location", location or "Remote",
        "--format", "json",
        "--jobage", str(jobage),
        "--limit", str(limit),
    ]
    if role:
        cmd += ["--query", role]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise LinkedInError("LinkedIn CLI timed out after 60 seconds") from exc
    except FileNotFoundError as exc:
        raise LinkedInError(f"Could not launch bun: {exc}") from exc

    if result.returncode != 0:
        # stderr carries { "error": "...", "code": "..." } on failure
        stderr = result.stderr.strip()
        try:
            err = json.loads(stderr)
            msg = err.get("error", stderr)
            code = err.get("code", "CLI_ERROR")
        except (json.JSONDecodeError, AttributeError):
            msg = stderr or "LinkedIn CLI exited with non-zero status"
            code = "CLI_ERROR"
        raise LinkedInError(f"[{code}] {msg}")

    stdout = result.stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LinkedInError(
            f"LinkedIn CLI returned non-JSON output: {exc}\n\nRaw output:\n{stdout[:500]}"
        ) from exc

    # CLI returns { meta: {...}, results: [...] }
    if isinstance(data, dict):
        return data.get("results") or []
    if isinstance(data, list):
        return data
    return []


def _normalise(card: dict) -> Listing:
    """Convert a raw CLI job card to the pipeline's Listing shape."""
    raw_date = card.get("date")
    # The CLI emits ISO date strings (e.g. "2025-01-15") or null; pass through.
    posted_date: Optional[str] = raw_date if isinstance(raw_date, str) and raw_date else None

    return Listing(
        id=str(card.get("id") or ""),
        title=str(card.get("title") or ""),
        company=card.get("company") or None,
        url=str(card.get("url") or ""),
        description=None,  # search results carry no description; use detail fetch for that
        posted_date=posted_date,
        source=SOURCE_NAME,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch(
    query: Optional[dict] = None,
    *,
    profile_path: Optional[Path] = None,
    jobage: int = 30,
    limit: int = 25,
) -> list[Listing]:
    """Fetch job listings from LinkedIn and return normalised ``Listing`` dicts.

    If *query* is ``None`` (the default), the function loads
    ``job_scraper/profile.json`` and derives the search query automatically
    from ``titles_held[0]`` and ``location_pref``.

    Args:
        query:        Optional ``{"role": str, "location": str}`` override.
                      Either key may be omitted or empty.
        profile_path: Override for ``job_scraper/profile.json``.
                      Ignored when *query* is provided explicitly.
        jobage:       Posted within N days (default 30).
        limit:        Maximum listings to return (default 25).

    Returns:
        List of ``Listing`` dicts, one per job card.  May be empty if the
        search returns no results or the board is temporarily unavailable.

    Raises:
        LinkedInError:   CLI not found, exits non-zero, or returns bad JSON.
        FileNotFoundError: ``profile.json`` is missing and *query* was not given.
        json.JSONDecodeError: ``profile.json`` exists but is corrupt.
    """
    if query is None:
        profile_path = Path(profile_path) if profile_path is not None else _DEFAULT_PROFILE
        if not profile_path.is_file():
            raise FileNotFoundError(
                f"profile.json not found at {profile_path}. "
                "Run load_profile() first to generate it."
            )
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        query = _build_query(profile)

    role = query.get("role", "")
    location = query.get("location", "")
    if location and location.lower() in ("unknown", "none"):
        location = ""

    raw_cards = _run_cli(role, location, jobage=jobage, limit=limit)
    return [_normalise(card) for card in raw_cards]


def fetch_detail(job_id_or_url: str) -> Optional[dict]:
    """Fetch full detail (including description) for a LinkedIn job card."""
    bun = _require_bun()
    cmd = [bun, "run", str(_CLI_ENTRY), "detail", str(job_id_or_url), "--format", "json"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout.strip())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None

