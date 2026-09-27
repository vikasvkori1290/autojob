"""Tests for job_scraper/sources/linkedin.py — LinkedIn connector."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from job_scraper.sources import Listing
from job_scraper.sources.linkedin import (
    LinkedInError,
    _build_query,
    _normalise,
    fetch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(**kwargs) -> dict:
    """Return a minimal raw CLI card, overridable via kwargs."""
    base = {
        "id": "123456789",
        "title": "Data Engineer",
        "company": "Acme Corp",
        "companyUrl": "https://www.linkedin.com/company/acme",
        "location": "Berlin, Germany",
        "date": "2025-01-15",
        "url": "https://www.linkedin.com/jobs/view/123456789",
    }
    base.update(kwargs)
    return base


def _cli_success(cards: list[dict]) -> MagicMock:
    """Return a mock CompletedProcess whose stdout is a well-formed CLI response."""
    payload = json.dumps({"meta": {"count": len(cards), "page": 1}, "results": cards})
    m = MagicMock()
    m.returncode = 0
    m.stdout = payload
    m.stderr = ""
    return m


def _cli_failure(error: str, code: str = "CLI_ERROR") -> MagicMock:
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = json.dumps({"error": error, "code": code})
    return m


def _write_profile(tmp: str, data: dict) -> Path:
    p = Path(tmp) / "profile.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------

class TestBuildQuery(unittest.TestCase):
    def test_uses_first_title_and_location(self):
        profile = {"titles_held": ["ML Engineer", "Data Scientist"], "location_pref": "Copenhagen"}
        q = _build_query(profile)
        self.assertEqual(q["role"], "ML Engineer")
        self.assertEqual(q["location"], "Copenhagen")

    def test_empty_titles_gives_empty_role(self):
        q = _build_query({"titles_held": [], "location_pref": "Remote"})
        self.assertEqual(q["role"], "")

    def test_missing_titles_gives_empty_role(self):
        q = _build_query({"location_pref": "Remote"})
        self.assertEqual(q["role"], "")

    def test_missing_location_gives_empty_string(self):
        q = _build_query({"titles_held": ["Engineer"]})
        self.assertEqual(q["location"], "")

    def test_none_location_gives_empty_string(self):
        q = _build_query({"titles_held": ["Engineer"], "location_pref": None})
        self.assertEqual(q["location"], "")


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------

class TestNormalise(unittest.TestCase):
    def test_maps_all_fields(self):
        card = _make_card()
        listing = _normalise(card)
        self.assertEqual(listing["id"], "123456789")
        self.assertEqual(listing["title"], "Data Engineer")
        self.assertEqual(listing["company"], "Acme Corp")
        self.assertEqual(listing["url"], "https://www.linkedin.com/jobs/view/123456789")
        self.assertEqual(listing["posted_date"], "2025-01-15")
        self.assertIsNone(listing["description"])
        self.assertEqual(listing["source"], "linkedin")

    def test_description_always_none(self):
        """Search results have no description — must always be None."""
        listing = _normalise(_make_card())
        self.assertIsNone(listing["description"])

    def test_null_date_maps_to_none(self):
        listing = _normalise(_make_card(date=None))
        self.assertIsNone(listing["posted_date"])

    def test_null_company_maps_to_none(self):
        listing = _normalise(_make_card(company=None))
        self.assertIsNone(listing["company"])

    def test_empty_company_maps_to_none(self):
        listing = _normalise(_make_card(company=""))
        self.assertIsNone(listing["company"])

    def test_source_is_linkedin(self):
        listing = _normalise(_make_card())
        self.assertEqual(listing["source"], "linkedin")

    def test_id_coerced_to_string(self):
        listing = _normalise(_make_card(id=987654))
        self.assertIsInstance(listing["id"], str)

    def test_returns_listing_typed_dict(self):
        listing = _normalise(_make_card())
        for key in ("id", "title", "company", "url", "description", "posted_date", "source"):
            self.assertIn(key, listing)


# ---------------------------------------------------------------------------
# fetch — bun CLI mocked
# ---------------------------------------------------------------------------

class TestFetch(unittest.TestCase):
    def _patch_run(self, mock_result):
        return patch("job_scraper.sources.linkedin.subprocess.run", return_value=mock_result)

    def _patch_bun(self, path: str = "/usr/bin/bun"):
        return patch("job_scraper.sources.linkedin.shutil.which", return_value=path)

    def test_returns_listings_from_cli(self):
        cards = [_make_card(), _make_card(id="999", title="Backend Engineer")]
        with self._patch_bun(), self._patch_run(_cli_success(cards)):
            result = fetch({"role": "engineer", "location": "Berlin, Germany"})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "Data Engineer")
        self.assertEqual(result[1]["title"], "Backend Engineer")

    def test_all_items_have_source_linkedin(self):
        cards = [_make_card(), _make_card(id="2")]
        with self._patch_bun(), self._patch_run(_cli_success(cards)):
            result = fetch({"role": "eng", "location": "Remote"})
        self.assertTrue(all(r["source"] == "linkedin" for r in result))

    def test_empty_results_returns_empty_list(self):
        with self._patch_bun(), self._patch_run(_cli_success([])):
            result = fetch({"role": "niche role", "location": "Remote"})
        self.assertEqual(result, [])

    def test_raises_linked_in_error_on_cli_failure(self):
        with self._patch_bun(), self._patch_run(_cli_failure("rate limited", "RATE_LIMIT")):
            with self.assertRaises(LinkedInError) as ctx:
                fetch({"role": "eng", "location": "Remote"})
        self.assertIn("RATE_LIMIT", str(ctx.exception))

    def test_raises_linked_in_error_when_bun_missing(self):
        with patch("job_scraper.sources.linkedin.shutil.which", return_value=None):
            with self.assertRaises(LinkedInError) as ctx:
                fetch({"role": "eng", "location": "Remote"})
        self.assertIn("bun", str(ctx.exception).lower())

    def test_raises_linked_in_error_on_bad_json(self):
        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = "not json {"
        bad.stderr = ""
        with self._patch_bun(), self._patch_run(bad):
            with self.assertRaises(LinkedInError) as ctx:
                fetch({"role": "eng", "location": "Remote"})
        self.assertIn("non-JSON", str(ctx.exception))

    def test_handles_cli_returning_bare_list(self):
        """CLI may return a bare JSON array instead of {meta, results}."""
        cards = [_make_card()]
        bare = MagicMock()
        bare.returncode = 0
        bare.stdout = json.dumps(cards)
        bare.stderr = ""
        with self._patch_bun(), self._patch_run(bare):
            result = fetch({"role": "eng", "location": "Remote"})
        self.assertEqual(len(result), 1)

    def test_passes_query_and_location_to_cli(self):
        with self._patch_bun() as mock_which, \
             patch("job_scraper.sources.linkedin.subprocess.run", return_value=_cli_success([])) as mock_run:
            fetch({"role": "data scientist", "location": "Paris, France"})
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        self.assertIn("data scientist", cmd_str)
        self.assertIn("Paris, France", cmd_str)

    def test_passes_jobage_and_limit_to_cli(self):
        with self._patch_bun(), \
             patch("job_scraper.sources.linkedin.subprocess.run", return_value=_cli_success([])) as mock_run:
            fetch({"role": "eng", "location": "Remote"}, jobage=7, limit=10)
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        self.assertIn("7", cmd_str)
        self.assertIn("10", cmd_str)

    def test_raises_file_not_found_when_profile_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                fetch(profile_path=Path(d) / "profile.json")

    def test_loads_profile_and_derives_query(self):
        """When query=None, fetch must load profile.json and derive role+location."""
        with tempfile.TemporaryDirectory() as d:
            profile = {"titles_held": ["ML Engineer"], "location_pref": "Amsterdam"}
            profile_path = _write_profile(d, profile)
            with self._patch_bun(), \
                 patch("job_scraper.sources.linkedin.subprocess.run", return_value=_cli_success([])) as mock_run:
                fetch(profile_path=profile_path)
            cmd_str = " ".join(str(c) for c in mock_run.call_args[0][0])
        self.assertIn("ML Engineer", cmd_str)
        self.assertIn("Amsterdam", cmd_str)

    def test_empty_stdout_returns_empty_list(self):
        empty = MagicMock()
        empty.returncode = 0
        empty.stdout = ""
        empty.stderr = ""
        with self._patch_bun(), self._patch_run(empty):
            result = fetch({"role": "eng", "location": "Remote"})
        self.assertEqual(result, [])

    def test_raises_linked_in_error_on_timeout(self):
        with self._patch_bun(), \
             patch("job_scraper.sources.linkedin.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="bun", timeout=60)):
            with self.assertRaises(LinkedInError) as ctx:
                fetch({"role": "eng", "location": "Remote"})
        self.assertIn("timed out", str(ctx.exception))

    def test_location_defaults_to_remote_when_empty(self):
        """An empty location string must fall back to 'Remote' in the CLI call."""
        with self._patch_bun(), \
             patch("job_scraper.sources.linkedin.subprocess.run", return_value=_cli_success([])) as mock_run:
            fetch({"role": "eng", "location": ""})
        cmd_str = " ".join(str(c) for c in mock_run.call_args[0][0])
        self.assertIn("Remote", cmd_str)


if __name__ == "__main__":
    unittest.main()
