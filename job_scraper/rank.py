"""LLM-based job ranking for the job_scraper pipeline.

Takes the deduped ``Listing`` dicts from Phase 4 and the loaded profile
dict from Phase 2, scores each listing against the candidate's profile
using the shared LLM client (gpt-oss-20b via NVIDIA NIM), and saves a
timestamped results file under ``job_scraper/results/``.

Batching
--------
Listings are grouped into batches of ``BATCH_SIZE`` (default 5) and each
batch is sent as a single LLM call with the full profile included once.
This avoids N individual calls while keeping the input token budget well
within gpt-oss-20b's context window — 5 listings × ~1,000 tokens each
plus ~700 tokens of system/profile context stays safely under 8k tokens,
which is the conservative floor for NIM-hosted models.  Going to 10
would halve the call count but doubles the retry blast radius and risks
hitting the context ceiling on description-heavy batches.

Retry policy
------------
If the LLM returns malformed JSON for a batch, one retry is made with a
stricter "return ONLY valid JSON" reminder prepended to the user message.
If the retry also fails, the batch is marked as errored and the run
continues — one bad batch does not abort the whole ranking run.

Public API::

    from job_scraper.rank import rank

    scored = rank(listings, profile)
    # -> list[ScoredListing], sorted by fit_score descending

    # With optional seniority filter:
    scored = rank(listings, profile, seniority="senior")
"""

import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from job_scraper.lib.llm import chat
from job_scraper.sources import Listing

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_RESULTS_DIR = _REPO_ROOT / "job_scraper" / "results"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BATCH_SIZE = 5   # listings per LLM call — see module docstring for rationale

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ScoredListing(Listing, total=False):
    """A ``Listing`` augmented with ranking fields."""
    job_id: str          # mirrors listing["id"] for clarity in results JSON
    fit_score: int       # 0-100
    reasoning: str       # one-sentence justification from the LLM
    missing_skills: list # skills in the job that the profile lacks
    error: str           # set instead of the above four when a batch fails


_CONFIG_PATH = _REPO_ROOT / "job_scraper" / "config.json"

if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import tempfile
    _TMP_BASE = Path(tempfile.gettempdir()) / "autojobapply"
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    _RESULTS_DIR = _TMP_BASE / "results"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH = _TMP_BASE / "config.json"

DEFAULT_CONFIG = {
    "location": "Bangalore, India",
    "language": "English",
    "seniority_default": "mid",
    "deal_breakers": [
        "REJECT if the posting explicitly requires a language other than English.",
        "REJECT if compensation is unpaid or equity-only (no base salary).",
    ],
    "score_boosts": [
        "+5 bonus (capped at 20) if the company is explicitly AI/ML-focused or remote-first.",
    ],
}


def load_rubric_config(path: Optional[Path] = None) -> dict:
    """Load rubric configuration from config.json or return defaults."""
    cfg_file = path or _CONFIG_PATH
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_rubric_config(config: dict, path: Optional[Path] = None) -> None:
    """Persist rubric configuration to config.json."""
    cfg_file = path or _CONFIG_PATH
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a precise job-fit scoring assistant.
    Given a candidate profile and a batch of job listings, score each listing
    against the profile using the rubric below.

    Return ONLY a valid JSON array — no markdown fences, no prose, no trailing
    text. The array must contain exactly one object per listing, in the same
    order the listings were provided, each with these keys:
      "job_id"        – string, the listing's id field
      "fit_score"     – integer 0-100
      "reasoning"     – string, one sentence explaining the score
      "missing_skills"– array of strings, skills the job requires that the
                        candidate lacks (empty array if none)

    ── SCORING RUBRIC ──────────────────────────────────────────────────────

    Compute fit_score as a weighted sum of four dimensions (max 100):

    1. Skills overlap (40 pts)
       How many of the candidate's skills appear in the posting, weighted by
       how central they are to the role description. If a job listing does not
       include a full description, evaluate skills and domain fit based on the
       job title, tech keywords in the title, and company. Do NOT reject solely
       because the description is omitted.

    2. Seniority match (20 pts)
       {seniority_clause}

    3. Domain fit (20 pts)
       Full 20 pts if the role is in the candidate's primary skill domain.
       {boosts_clause}

    4. Location / remote fit (10 pts)
       {location_clause}

    ── HARD GATES (set fit_score = 0 if triggered) ─────────────────────────

    {deal_breakers_clause}

    ── FLAGS (do not reject; note in reasoning) ────────────────────────────

    • FLAG if the posting requires relocation to a different city with no
      remote or hybrid option.
    • FLAG if a language the candidate has is required at a higher level than
      the candidate's stated proficiency.

    ── END RUBRIC ──────────────────────────────────────────────────────────
""")

_RETRY_PREFIX = (
    "IMPORTANT: Your previous response was not valid JSON. "
    "Return ONLY a valid JSON array — no markdown fences, no prose, "
    "no explanation. Start your response with [ and end it with ].\n\n"
)

_SENIORITY_ACTIVE = (
    "Reject (score 0) if the role is explicitly labeled junior, intern, "
    "entry-level, or graduate — target seniority for this run is: {level}. "
    "Score 20 pts for a match; 10 pts for one level above or below; "
    "0 pts for a clear mismatch."
)
_SENIORITY_NEUTRAL = (
    "No seniority filter is active for this run — score 20 pts neutrally "
    "(i.e. give all listings the full 20 pts on this dimension)."
)


def _build_system_prompt(seniority: Optional[str] = None, config: Optional[dict] = None) -> str:
    cfg = config if config is not None else load_rubric_config()

    if seniority:
        clause = _SENIORITY_ACTIVE.format(level=seniority.strip().lower())
    else:
        clause = _SENIORITY_NEUTRAL

    loc = cfg.get("location", DEFAULT_CONFIG["location"])
    location_clause = (
        f"10 pts: role is in {loc} OR is remote/hybrid with no\n"
        f"        geographic restriction beyond India.\n"
        f" 5 pts: role requires relocation to a different city with no\n"
        f"        remote or hybrid option — FLAG this in reasoning.\n"
        f" 0 pts: role is on-site outside India with no remote option."
    )

    deal_breakers = cfg.get("deal_breakers") or DEFAULT_CONFIG["deal_breakers"]
    deal_breakers_clause = "\n".join(
        f"• {db}" if not db.strip().startswith("•") else db
        for db in deal_breakers
    )

    boosts = cfg.get("score_boosts") or DEFAULT_CONFIG["score_boosts"]
    boosts_clause = "\n".join(boosts)

    return _SYSTEM_PROMPT.format(
        seniority_clause=clause,
        location_clause=location_clause,
        deal_breakers_clause=deal_breakers_clause,
        boosts_clause=boosts_clause,
    )


def _build_user_message(profile: dict, batch: list[Listing]) -> str:
    """Serialise profile + listings batch into the user turn."""
    profile_block = json.dumps({
        "skills":          profile.get("skills", []),
        "years_experience": profile.get("years_experience", 0),
        "titles_held":     profile.get("titles_held", []),
        "industries":      profile.get("industries", []),
        "seniority":       profile.get("seniority", ""),
        "location_pref":   profile.get("location_pref", ""),
        "summary":         profile.get("summary", ""),
    }, ensure_ascii=False)

    listings_block = json.dumps(
        [
            {
                "id":          l["id"],
                "title":       l["title"],
                "company":     l.get("company") or "",
                "location":    l.get("location") if "location" in l else "",  # type: ignore[typeddict-item]
                "description": l.get("description") or "(no description provided)",
            }
            for l in batch
        ],
        ensure_ascii=False,
        indent=2,
    )

    return (
        f"CANDIDATE PROFILE:\n{profile_block}\n\n"
        f"JOB LISTINGS TO SCORE ({len(batch)} total):\n{listings_block}"
    )


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_scores(response: str) -> list[dict]:
    """Parse the LLM response into a list of score objects.

    Strips accidental markdown fences and validates that each object has
    the required keys.

    Raises:
        ValueError: If response is not a JSON array or objects are malformed.
    """
    text = response.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```"))

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got {type(data).__name__}")

    validated = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item {i} is not an object")
        # Coerce / default each required key
        validated.append({
            "job_id":         str(item.get("job_id") or ""),
            "fit_score":      max(0, min(100, int(item.get("fit_score") or 0))),
            "reasoning":      str(item.get("reasoning") or ""),
            "missing_skills": list(item.get("missing_skills") or []),
        })

    return validated


# ---------------------------------------------------------------------------
# Batch scoring
# ---------------------------------------------------------------------------

def _score_batch(
    batch: list[Listing],
    profile: dict,
    system_prompt: str,
) -> list[dict]:
    """Send one batch to the LLM; retry once on parse failure.

    Returns a list of validated score dicts (same length as batch) or,
    if both attempts fail, a list of error dicts.
    """
    user_msg = _build_user_message(profile, batch)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_msg},
    ]

    # First attempt
    try:
        response = chat(messages, temperature=0.0, max_tokens=BATCH_SIZE * 300)
        scores = _parse_scores(response)
        if len(scores) == len(batch):
            return scores
        raise ValueError(
            f"LLM returned {len(scores)} score(s) for a batch of {len(batch)}"
        )
    except (ValueError, Exception) as first_err:
        pass

    # Single retry with stricter instruction
    retry_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": _RETRY_PREFIX + user_msg},
    ]
    try:
        response = chat(retry_messages, temperature=0.0, max_tokens=BATCH_SIZE * 300)
        scores = _parse_scores(response)
        if len(scores) == len(batch):
            return scores
        raise ValueError(
            f"Retry: LLM returned {len(scores)} score(s) for a batch of {len(batch)}"
        )
    except Exception as retry_err:
        # Both attempts failed — return error sentinels for each listing in batch
        error_msg = str(retry_err)
        return [
            {
                "job_id":         l["id"],
                "fit_score":      0,
                "reasoning":      "",
                "missing_skills": [],
                "error":          error_msg,
            }
            for l in batch
        ]


# ---------------------------------------------------------------------------
# Results persistence
# ---------------------------------------------------------------------------

def _save_results(scored: list[dict], results_dir: Path) -> Path:
    """Write the scored results to a timestamped JSON file."""
    results_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"{ts}.json"
    out_path.write_text(
        json.dumps(scored, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank(
    listings: Sequence[Listing],
    profile: dict,
    *,
    seniority: Optional[str] = None,
    results_dir: Optional[Path] = None,
    batch_size: int = BATCH_SIZE,
    config: Optional[dict] = None,
) -> list[dict]:
    """Score and rank a list of job listings against a candidate profile.

    Listings are grouped into batches of *batch_size* (default 5) and each
    batch is scored in a single LLM call.  Results are sorted by
    ``fit_score`` descending and saved to ``job_scraper/results/<ts>.json``.

    Args:
        listings:    Normalised ``Listing`` dicts, typically from ``dedup()``.
        profile:     Candidate profile dict from ``load_profile()``.
        seniority:   Optional target seniority level for this run
                     (e.g. ``"mid"``, ``"senior"``, ``"lead"``).
                     When ``None``, seniority is scored neutrally.
        results_dir: Override for the results output directory.
        batch_size:  Listings per LLM call.  Override in tests or to tune
                     for a different model's context window.
        config:      Optional rubric config dictionary override.

    Returns:
        List of dicts, each being the original ``Listing`` fields merged with
        the scoring fields (``job_id``, ``fit_score``, ``reasoning``,
        ``missing_skills``).  Sorted by ``fit_score`` descending.
        Items where the LLM failed will carry an ``"error"`` key instead.
    """
    if not listings:
        return []

    system_prompt = _build_system_prompt(seniority, config=config)
    out_dir = Path(results_dir) if results_dir is not None else _RESULTS_DIR

    listing_list = list(listings)
    scored: list[dict] = []

    for i in range(0, len(listing_list), batch_size):
        batch = listing_list[i : i + batch_size]
        print(
            f"[rank] Scoring batch {i // batch_size + 1}/"
            f"{(len(listing_list) + batch_size - 1) // batch_size} "
            f"({len(batch)} listing(s))..."
        )
        batch_scores = _score_batch(batch, profile, system_prompt)

        for listing, score in zip(batch, batch_scores):
            # Merge listing fields with score fields; score keys win on conflict
            entry: dict = dict(listing)
            entry.update(score)
            # Ensure job_id always mirrors the listing id (LLM may return wrong id)
            entry["job_id"] = listing["id"]
            scored.append(entry)

    scored.sort(key=lambda r: r.get("fit_score", 0), reverse=True)

    out_path = _save_results(scored, out_dir)
    print(f"[rank] Results saved to {out_path}")

    return scored
