"""Job source connectors for the job_scraper pipeline.

Every connector module in this package exposes a ``fetch()`` function
that returns a list of ``Listing`` dicts — the common normalised shape
shared across all sources.

Importing the shape::

    from job_scraper.sources import Listing
"""

from typing import Optional, TypedDict


class Listing(TypedDict):
    """Normalised job listing — the common output shape for every connector."""

    id: str
    """Source-specific unique identifier (e.g. LinkedIn job ID)."""

    title: str
    """Job title as posted."""

    company: Optional[str]
    """Hiring company name, or None if not available."""

    url: str
    """Canonical link to the original posting."""

    description: Optional[str]
    """Full job description text, or None when the source does not return it
    at search time (e.g. LinkedIn search results — description requires a
    separate detail fetch)."""

    posted_date: Optional[str]
    """ISO 8601 date string (YYYY-MM-DD) when the posting was published,
    or None if the source does not provide it."""

    source: str
    """Identifier for the connector that produced this listing, e.g. ``"linkedin"``."""


AVAILABLE_SOURCES = ("all", "linkedin", "internshala")


def fetch_from_sources(
    source_name: str = "all",
    query: Optional[dict] = None,
    *,
    jobage: int = 30,
    limit: int = 25,
) -> list[Listing]:
    """Fetch listings from one or multiple configured job sources.

    Args:
        source_name: "all", "linkedin", or "internshala".
        query: Optional {"role": str, "location": str} override.
        jobage: Max posting age in days.
        limit: Max listings per source.

    Returns:
        Combined list of Listing dicts.
    """
    from job_scraper.sources.linkedin import fetch as fetch_linkedin
    from job_scraper.sources.internshala import fetch as fetch_internshala

    name = (source_name or "all").lower().strip()
    results: list[Listing] = []

    if name in ("all", "linkedin"):
        try:
            results.extend(fetch_linkedin(query, jobage=jobage, limit=limit))
        except Exception:
            pass

    if name in ("all", "internshala"):
        try:
            results.extend(fetch_internshala(query, jobage=jobage, limit=limit))
        except Exception:
            pass

    return results


def fetch_detail_for_listing(listing: Listing) -> Optional[dict]:
    """Fetch full detail (including description text) for a listing based on its source."""
    src = listing.get("source", "").lower()
    if src == "linkedin":
        from job_scraper.sources.linkedin import fetch_detail as fetch_li_detail

        return fetch_li_detail(listing["id"])
    elif src == "internshala":
        from job_scraper.sources.internshala import fetch_detail as fetch_is_detail

        return fetch_is_detail(listing.get("url") or listing["id"])
    return None

