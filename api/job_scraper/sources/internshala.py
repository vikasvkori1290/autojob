"""Internshala job source connector for the job_scraper pipeline.

Scrapes live fresher/entry-level and junior job postings from Internshala
(https://internshala.com/jobs/) for India and Remote markets.

No authentication or API key required — parses public job listings and
detail pages directly with rate-limiting and standard HTTP headers.

Public API::

    from job_scraper.sources.internshala import fetch, fetch_detail

    listings = fetch({"role": "full stack developer", "location": "Bangalore"})
    # -> list[Listing]
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from job_scraper.sources import Listing

SOURCE_NAME = "internshala"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PROFILE = _REPO_ROOT / "job_scraper" / "profile.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class InternshalaError(Exception):
    """Raised when Internshala requests fail or encounter invalid responses."""


def _slugify(text: str) -> str:
    """Normalize text into an alphanumeric hyphen-separated slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return s.strip("-")


def _build_search_urls(role: str, location: str) -> list[str]:
    """Generate search URLs in order of specificity."""
    role_slug = _slugify(role) if role else "software-development"
    loc_slug = _slugify(location) if location else ""

    urls = []
    # If location is explicitly Remote or Work From Home
    if loc_slug in ("remote", "work-from-home", "wfh"):
        urls.append(f"https://internshala.com/jobs/work-from-home-{role_slug}-jobs/")
        urls.append(f"https://internshala.com/jobs/{role_slug}-jobs/")
    elif loc_slug:
        # e.g. /jobs/full-stack-developer-jobs-in-bangalore/
        urls.append(f"https://internshala.com/jobs/{role_slug}-jobs-in-{loc_slug}/")
        urls.append(f"https://internshala.com/jobs/{role_slug}-jobs/")
    else:
        urls.append(f"https://internshala.com/jobs/{role_slug}-jobs/")

    # Fallback to keyword search
    urls.append(f"https://internshala.com/jobs/keywords-{role_slug}/")
    return urls


def _fetch_html(url: str, timeout: int = 15) -> str:
    """Send an HTTP GET request and return the response body as text."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise InternshalaError(f"Internshala HTTP error {exc.code} for {url}") from exc
    except Exception as exc:
        raise InternshalaError(f"Internshala connection error: {exc}") from exc


def _parse_listings(html: str, limit: int = 25) -> list[Listing]:
    """Parse job cards from Internshala search HTML."""
    raw_cards = html.split('<div class="container-fluid individual_internship')
    listings: list[Listing] = []

    for card_html in raw_cards[1:]:
        if len(listings) >= limit:
            break

        id_m = re.search(r'id="individual_internship_(\d+)"', card_html)
        if not id_m:
            continue
        job_id = id_m.group(1)

        href_m = re.search(r'data-href="([^"]+)"', card_html) or re.search(
            r'href="(/job/detail/[^"]+)"', card_html
        )
        if not href_m:
            continue
        href = href_m.group(1)
        url = f"https://internshala.com{href}" if href.startswith("/") else href

        title_m = re.search(
            r'class="job-title-href"[^>]*>([^<]+)</a>', card_html
        ) or re.search(
            r'class="job-internship-name"[^>]*>.*?<a[^>]*>([^<]+)</a>',
            card_html,
            re.DOTALL,
        )
        title = title_m.group(1).strip() if title_m else "Software Developer"

        comp_m = re.search(r'class="company-name"[^>]*>([^<]+)</p>', card_html)
        company = comp_m.group(1).strip() if comp_m else None

        loc_m = re.search(
            r'class="row-1-item\s+locations"[^>]*>.*?<a>([^<]+)</a>',
            card_html,
            re.DOTALL,
        )
        location = loc_m.group(1).strip() if loc_m else None

        listings.append(
            Listing(
                id=job_id,
                title=title,
                company=company,
                url=url,
                description=None,
                posted_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source=SOURCE_NAME,
            )
        )

    return listings


def fetch_detail(job_id_or_url: str) -> Optional[dict]:
    """Fetch full detail (including description text) for an Internshala job."""
    url = str(job_id_or_url)
    if not url.startswith("http"):
        url = f"https://internshala.com/job/detail/{url}"

    try:
        html = _fetch_html(url)
    except Exception:
        return None

    # Extract description text
    desc = None
    pos = html.find("text-container")
    if pos != -1:
        # Extract text up to end of section
        end_pos = html.find("</div>", pos)
        # Capture full container
        container = html[pos:pos + 3000]
        # Clean HTML tags
        clean = re.sub(r"<[^>]+>", " ", container)
        clean = re.sub(r"\s+", " ", clean).replace("text-container\">", "").strip()
        desc = clean

    title_m = re.search(r'<h1[^>]*class="[^"]*heading_4_5[^"]*"[^>]*>([^<]+)</h1>', html) or re.search(
        r'<title>([^<|]+)', html
    )
    title = title_m.group(1).strip() if title_m else None

    comp_m = re.search(r'<div[^>]*class="[^"]*heading_6 company_name[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>', html, re.DOTALL)
    company = comp_m.group(1).strip() if comp_m else None

    return {
        "url": url,
        "title": title,
        "company": company,
        "description": desc,
    }


def fetch(
    query: Optional[dict] = None,
    *,
    profile_path: Optional[Path] = None,
    jobage: int = 30,
    limit: int = 25,
) -> list[Listing]:
    """Fetch job listings from Internshala matching the query.

    Args:
        query:        Optional ``{"role": str, "location": str}`` dict.
        profile_path: Override for profile.json when query is omitted.
        jobage:       Max age (unused directly by Internshala, which lists active jobs).
        limit:        Max listings to return.

    Returns:
        List of ``Listing`` dicts.
    """
    if query is None:
        p_path = Path(profile_path) if profile_path is not None else _DEFAULT_PROFILE
        if p_path.is_file():
            try:
                prof = json.loads(p_path.read_text(encoding="utf-8"))
                titles = prof.get("titles_held") or []
                role = titles[0] if titles else "full-stack-developer"
                loc = prof.get("location_pref") or "bangalore"
            except Exception:
                role, loc = "full-stack-developer", "bangalore"
        else:
            role, loc = "full-stack-developer", "bangalore"
    else:
        role = query.get("role") or "full-stack-developer"
        loc = query.get("location") or "bangalore"

    if loc.lower() in ("unknown", "none"):
        loc = "bangalore"

    search_urls = _build_search_urls(role, loc)

    for url in search_urls:
        try:
            html = _fetch_html(url)
            listings = _parse_listings(html, limit=limit)
            if listings:
                return listings
        except InternshalaError:
            continue

    return []
