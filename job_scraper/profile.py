"""Profile ingestion for the job_scraper pipeline.

Reads source documents from documents/cv/ (required) and
documents/linkedin/ (optional), extracts their text, calls the shared
LLM client to parse a structured candidate profile, and caches the
result in job_scraper/profile.json.

The cache is considered fresh when profile.json exists and is newer
than every source file — the LLM call is skipped on re-runs unless a
document has changed.

Public API (everything Phase 3+ should import)::

    from job_scraper.profile import load_profile, ProfileSourceError

    profile = load_profile()
    # -> {"skills": [...], "years_experience": 5, ...}
"""

import json
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from job_scraper.lib.llm import chat
from job_scraper.lib.pdf import extract_text as extract_pdf_text

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file so callers need no knowledge of layout)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CV_DIR = _REPO_ROOT / "documents" / "cv"
_DEFAULT_LINKEDIN_DIR = _REPO_ROOT / "documents" / "linkedin"
_DEFAULT_OUTPUT = _REPO_ROOT / "job_scraper" / "profile.json"

if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import tempfile
    _TMP_BASE = Path(tempfile.gettempdir()) / "autojobapply"
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    _DEFAULT_CV_DIR = _TMP_BASE / "cv"
    _DEFAULT_CV_DIR.mkdir(parents=True, exist_ok=True)
    _DEFAULT_OUTPUT = _TMP_BASE / "profile.json"

# Source file extensions the ingestion pipeline can read.
_SUPPORTED_EXTENSIONS = {".pdf", ".tex", ".txt", ".md"}

# The exact JSON schema the LLM must return.
_PROFILE_SCHEMA = {
    "skills": [],
    "years_experience": 0,
    "titles_held": [],
    "industries": [],
    "location_pref": "",
    "seniority": "",
    "summary": "",
}

_SYSTEM_PROMPT = """\
You are a structured data extractor. Given resume or LinkedIn profile text, \
extract a candidate profile and return ONLY a single valid JSON object — \
no markdown fences, no commentary, no trailing text.

The JSON object must have exactly these keys:
  "skills"           – list of strings; technical and domain skills
  "years_experience" – integer; total years of professional experience
  "titles_held"      – list of strings; job titles the candidate has held
  "industries"       – list of strings; industries/sectors worked in
  "location_pref"    – string; stated or implied location preference (city, country, or "remote")
  "seniority"        – string; one of: "junior", "mid", "senior", "lead", "principal", "unknown"
  "summary"          – string; 2–3 sentence professional summary

Return only the JSON object. Do not include any other text."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProfileSourceError(Exception):
    """Raised when no usable source documents are found under documents/cv/."""


class ProfileParseError(Exception):
    """Raised when the LLM response cannot be parsed into the expected schema."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_sources(directory: Path) -> list[Path]:
    """Return supported files in *directory*, ignoring .gitkeep and hidden files."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() in _SUPPORTED_EXTENSIONS
        and not p.name.startswith(".")
    )


def is_cache_fresh(output_path: Path, source_paths: list[Path]) -> bool:
    """Return True if *output_path* exists and is newer than every source file."""
    if not output_path.is_file():
        return False
    if not source_paths:
        return False
    cache_mtime = output_path.stat().st_mtime
    return all(cache_mtime > p.stat().st_mtime for p in source_paths)


def _read_source(path: Path) -> str:
    """Extract text from a single source file."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def _build_extraction_prompt(sources: list[tuple[Path, str]]) -> str:
    """Concatenate all source texts with clear file headers."""
    parts = []
    for path, text in sources:
        parts.append(f"=== {path.name} ===\n{text.strip()}")
    return "\n\n".join(parts)


def _parse_llm_response(response: str) -> dict:
    """Parse the LLM JSON response and validate required keys.

    Fills any missing key with its zero-value from _PROFILE_SCHEMA rather
    than crashing — the LLM may occasionally omit a key even when instructed
    not to.

    Raises:
        ProfileParseError: If *response* is not valid JSON or is not an object.
    """
    # Strip accidental markdown fences the model may emit despite instructions
    text = response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence (```json or ```) and closing fence (```)
        inner = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(inner)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileParseError(
            f"LLM response is not valid JSON: {exc}\n\nRaw response:\n{response[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise ProfileParseError(
            f"LLM response must be a JSON object, got {type(data).__name__}"
        )

    # Fill missing keys with zero-values; coerce obvious type mismatches
    result = dict(_PROFILE_SCHEMA)  # start from defaults
    result.update(data)             # overlay LLM output

    # Ensure list fields really are lists
    for key in ("skills", "titles_held", "industries"):
        if not isinstance(result[key], list):
            result[key] = [str(result[key])] if result[key] else []

    # Ensure integer field
    try:
        result["years_experience"] = int(result["years_experience"])
    except (TypeError, ValueError):
        result["years_experience"] = 0

    # Ensure string fields
    for key in ("location_pref", "seniority", "summary"):
        if not isinstance(result[key], str):
            result[key] = str(result[key]) if result[key] else ""

    return result


def _write_atomic(path: Path, data: dict) -> None:
    """Write JSON to *path* atomically using a temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".profile.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_sources(
    cv_dir: Path | None = None,
    linkedin_dir: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """Return (cv_sources, linkedin_sources).

    Raises:
        ProfileSourceError: If no supported files are found under *cv_dir*.
    """
    cv_dir = Path(cv_dir) if cv_dir is not None else _DEFAULT_CV_DIR
    linkedin_dir = Path(linkedin_dir) if linkedin_dir is not None else _DEFAULT_LINKEDIN_DIR

    cv_sources = _collect_sources(cv_dir)
    linkedin_sources = _collect_sources(linkedin_dir)

    if not cv_sources:
        raise ProfileSourceError(
            f"No CV documents found in {cv_dir}.\n"
            "Add your resume as a .pdf, .tex, or .txt file under documents/cv/ and re-run."
        )

    return cv_sources, linkedin_sources


def load_profile(
    cv_dir: Path | None = None,
    linkedin_dir: Path | None = None,
    output_path: Path | None = None,
) -> dict:
    """Load (or build) the structured candidate profile.

    1. Discovers source files under cv_dir (required) and linkedin_dir (optional).
    2. Returns the cached profile.json if it is newer than all source files.
    3. Otherwise: extracts text, calls the LLM, saves the result, returns it.

    Args:
        cv_dir:      Override for documents/cv/. Defaults to repo-relative path.
        linkedin_dir: Override for documents/linkedin/. Defaults to repo-relative path.
        output_path: Override for job_scraper/profile.json.

    Returns:
        A dict with keys: skills, years_experience, titles_held, industries,
        location_pref, seniority, summary.

    Raises:
        ProfileSourceError:  No CV file found.
        ProfileParseError:   LLM returned unparseable output.
        EnvironmentError:    NVIDIA_API_KEY not set (from llm.chat).
    """
    output_path = Path(output_path) if output_path is not None else _DEFAULT_OUTPUT

    cv_sources, linkedin_sources = discover_sources(cv_dir, linkedin_dir)
    all_sources = cv_sources + linkedin_sources

    if is_cache_fresh(output_path, all_sources):
        print(f"[profile] Cache is fresh — loading {output_path}")
        return json.loads(output_path.read_text(encoding="utf-8"))

    print(f"[profile] Extracting text from {len(all_sources)} source file(s)...")
    sources_with_text: list[tuple[Path, str]] = []
    for path in all_sources:
        print(f"  reading {path.name}")
        sources_with_text.append((path, _read_source(path)))

    prompt = _build_extraction_prompt(sources_with_text)

    print("[profile] Calling LLM to parse profile...")
    response = chat(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=1024,
    )

    profile = _parse_llm_response(response)

    _write_atomic(output_path, profile)
    print(f"[profile] Saved to {output_path}")

    return profile
