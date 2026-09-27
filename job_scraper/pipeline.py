"""AutoJobApply pipeline entry point.

Runs the full scrape → dedup → rank flow in order:

  1. Load (or refresh) the candidate profile from documents/cv/
  2. Fetch raw listings from LinkedIn via the Phase 3 connector
  3. Deduplicate against seen_jobs.json (Phase 4)
  4. Score and rank surviving listings with the LLM rubric (Phase 5)
  5. Print a sorted fit table to the console

Usage::

    python -m job_scraper.pipeline [options]

    Options:
      --seniority LEVEL   Target seniority for this run: junior, mid, senior,
                          lead, principal. Omit for neutral scoring on that
                          dimension.
      --limit N           Max listings to fetch per source (default: 25).
      --jobage N          Only include postings published in the last N days
                          (default: 30).
      --role ROLE         Override the search query role derived from profile.
      --location LOC      Override the search query location derived from
                          profile.
      --dry-run           Fetch and dedup but skip the LLM ranking call.
      --no-save           Skip writing results to job_scraper/results/.

    Environment:
      NVIDIA_API_KEY      Required for ranking. Copy .env.example to .env.
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from job_scraper.dedupe import dedup
from job_scraper.profile import ProfileSourceError, load_profile
from job_scraper.rank import rank
from job_scraper.sources import fetch_from_sources, fetch_detail_for_listing


def fetch(query=None, *, jobage=30, limit=25, source="all"):
    """Fetch job listings from one or all configured sources."""
    return fetch_from_sources(source, query, jobage=jobage, limit=limit)



# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _render_table(scored: list[dict]) -> str:
    """Render a sorted fit table for console output.

    Columns: Rank | Score | Company | Title | Date | URL
    """
    col_rank  = 4
    col_score = 5
    col_co    = 22
    col_title = 34
    col_date  = 10
    # URL gets the remainder; we don't truncate it.

    header = (
        f"{'#':<{col_rank}} "
        f"{'Score':>{col_score}} "
        f"{'Company':<{col_co}} "
        f"{'Title':<{col_title}} "
        f"{'Posted':<{col_date}} "
        f"URL"
    )
    sep = "-" * len(header)

    lines = [header, sep]
    for i, item in enumerate(scored, start=1):
        score   = item.get("fit_score", 0)
        company = _truncate(item.get("company") or "—", col_co)
        title   = _truncate(item.get("title") or "—", col_title)
        date    = item.get("posted_date") or "—"
        url     = item.get("url") or "—"
        error   = item.get("error")

        score_str = f"{score:3d}" if not error else "err"
        lines.append(
            f"{i:<{col_rank}} "
            f"{score_str:>{col_score}} "
            f"{company:<{col_co}} "
            f"{title:<{col_title}} "
            f"{date:<{col_date}} "
            f"{url}"
        )
        if error:
            lines.append(f"     ↳ scoring error: {_truncate(error, 90)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    *,
    seniority: str | None = None,
    limit: int = 25,
    jobage: int = 30,
    role: str | None = None,
    location: str | None = None,
    source: str = "all",
    dry_run: bool = False,
    no_save: bool = False,
) -> int:
    """Execute the full pipeline. Returns an exit code (0 = success)."""

    # ── Step 1: profile ────────────────────────────────────────────────────
    print("[pipeline] Step 1/4 — loading candidate profile...")
    try:
        profile = load_profile()
    except ProfileSourceError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nError loading profile: {exc}", file=sys.stderr)
        return 1

    # ── Step 2: fetch ──────────────────────────────────────────────────────
    src_label = source.upper() if source != "all" else "all sources (LinkedIn, Internshala)"
    print(f"[pipeline] Step 2/4 — fetching listings from {src_label}...")
    query = None
    if role is not None or location is not None:
        query = {"role": role or "", "location": location or ""}

    try:
        if source == "all":
            raw_listings = fetch(query, jobage=jobage, limit=limit)
        else:
            raw_listings = fetch(query, jobage=jobage, limit=limit, source=source)
    except Exception as exc:
        print(f"\nUnexpected error fetching listings: {exc}", file=sys.stderr)
        return 1

    print(f"         {len(raw_listings)} listing(s) fetched.")

    # ── Step 3: dedup ──────────────────────────────────────────────────────
    print("[pipeline] Step 3/4 — deduplicating against seen_jobs.json...")
    new_listings = dedup(raw_listings)
    print(f"         {len(new_listings)} new listing(s) after dedup "
          f"({len(raw_listings) - len(new_listings)} already seen).")

    if not new_listings:
        print("\nNo new matching jobs found.")
        return 0

    for item in new_listings:
        if not item.get("description"):
            try:
                detail = fetch_detail_for_listing(item)
                if detail and detail.get("description"):
                    item["description"] = detail["description"]
            except Exception:
                pass

    # ── Step 4: rank ───────────────────────────────────────────────────────
    if dry_run:
        print("[pipeline] Step 4/4 — dry-run: skipping LLM ranking.")
        # Still print the unscored listings so the run is useful
        scored = [dict(l) | {"fit_score": 0, "job_id": l["id"]} for l in new_listings]
    else:
        print(f"[pipeline] Step 4/4 — ranking {len(new_listings)} listing(s)...")
        rank_kwargs: dict = {"seniority": seniority}
        if no_save:
            # Pass a temp dir so rank() doesn't write to job_scraper/results/
            import tempfile
            rank_kwargs["results_dir"] = Path(tempfile.mkdtemp())
        scored = rank(new_listings, profile, **rank_kwargs)

    # ── Output ─────────────────────────────────────────────────────────────
    label = "new job(s) found" if not dry_run else "new listing(s) (unscored — dry-run)"
    print(f"\n{'─' * 80}")
    print(f"  {len(scored)} {label}"
          + (f"  [seniority filter: {seniority}]" if seniority else ""))
    print(f"{'─' * 80}\n")
    print(_render_table(scored))
    print()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m job_scraper.pipeline",
        description="AutoJobApply: scrape → dedup → rank in one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--seniority",
        metavar="LEVEL",
        help="Target seniority: junior, mid, senior, lead, principal. "
             "Omit for neutral scoring.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=25,
        metavar="N",
        help="Max listings to fetch per source (default: 25).",
    )
    p.add_argument(
        "--jobage",
        type=int,
        default=30,
        metavar="N",
        help="Only include postings from the last N days (default: 30).",
    )
    p.add_argument(
        "--role",
        metavar="ROLE",
        help="Override search query role (default: derived from profile).",
    )
    p.add_argument(
        "--location",
        metavar="LOC",
        help="Override search location (default: derived from profile).",
    )
    p.add_argument(
        "--source",
        choices=["all", "linkedin", "internshala"],
        default="all",
        help="Job platform: all (default), linkedin, internshala.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and dedup but skip the LLM ranking call.",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="Skip writing results to job_scraper/results/.",
    )
    return p


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def main(argv=None) -> int:
    _force_utf8_output()
    args = _build_parser().parse_args(argv)
    return run(
        seniority=args.seniority,
        limit=args.limit,
        jobage=args.jobage,
        role=args.role,
        location=args.location,
        source=args.source,
        dry_run=args.dry_run,
        no_save=args.no_save,
    )


if __name__ == "__main__":
    sys.exit(main())
