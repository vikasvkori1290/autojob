"""Standalone CLI runner for the LinkedIn connector.

Run without building the rest of the pipeline:

    python -m job_scraper.sources.linkedin_cli
    python -m job_scraper.sources.linkedin_cli --role "data engineer" --location "Berlin, Germany"
    python -m job_scraper.sources.linkedin_cli --role "ML engineer" --location "Remote" --limit 10 --jobage 7

If --role / --location are omitted, the query is derived from
job_scraper/profile.json automatically.
"""

import argparse
import json
import sys
from pathlib import Path


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch LinkedIn job listings and print normalised JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--role", help="Job title / keyword query (overrides profile)")
    p.add_argument("--location", help='LinkedIn location string, e.g. "Berlin, Germany" or "Remote" (overrides profile)')
    p.add_argument("--limit", type=int, default=25, help="Max listings to return (default: 25)")
    p.add_argument("--jobage", type=int, default=30, help="Posted within N days (default: 30)")
    p.add_argument("--profile", type=Path, help="Path to profile.json (default: job_scraper/profile.json)")
    return p


def main(argv=None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)

    # Inline import so module-level errors surface cleanly
    from job_scraper.sources.linkedin import LinkedInError, fetch

    # Build explicit query only when the user supplied at least one flag
    query = None
    if args.role is not None or args.location is not None:
        query = {
            "role": args.role or "",
            "location": args.location or "",
        }

    try:
        listings = fetch(
            query,
            profile_path=args.profile,
            jobage=args.jobage,
            limit=args.limit,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except LinkedInError as exc:
        print(f"LinkedIn error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(listings, indent=2, ensure_ascii=False))
    print(f"\n# {len(listings)} listing(s) returned.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
