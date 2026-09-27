"""Tests for job_scraper/pipeline.py — end-to-end pipeline entry point."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_scraper.pipeline import _render_table, _truncate, main, run
from job_scraper.sources import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listing(**kwargs) -> Listing:
    base: Listing = {
        "id": "111",
        "title": "Data Engineer",
        "company": "Acme Corp",
        "url": "https://www.linkedin.com/jobs/view/111",
        "description": None,
        "posted_date": "2025-01-15",
        "source": "linkedin",
    }
    base.update(kwargs)
    return base


def _scored(**kwargs) -> dict:
    base = dict(_listing())
    base.update({
        "job_id": "111",
        "fit_score": 75,
        "reasoning": "Good match.",
        "missing_skills": [],
    })
    base.update(kwargs)
    return base


def _profile() -> dict:
    return {
        "skills": ["Python", "SQL"],
        "years_experience": 5,
        "titles_held": ["Data Engineer"],
        "industries": ["fintech"],
        "seniority": "mid",
        "location_pref": "Bangalore",
        "summary": "Data engineer.",
    }


def _patch_profile(profile=None):
    return patch(
        "job_scraper.pipeline.load_profile",
        return_value=profile or _profile(),
    )


def _patch_fetch(listings=None):
    return patch(
        "job_scraper.pipeline.fetch",
        return_value=listings if listings is not None else [_listing()],
    )


def _patch_dedup(new_listings=None):
    return patch(
        "job_scraper.pipeline.dedup",
        return_value=new_listings if new_listings is not None else [_listing()],
    )


def _patch_rank(scored=None):
    return patch(
        "job_scraper.pipeline.rank",
        return_value=scored if scored is not None else [_scored()],
    )


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate(unittest.TestCase):
    def test_short_string_unchanged(self):
        self.assertEqual(_truncate("hello", 10), "hello")

    def test_exact_length_unchanged(self):
        self.assertEqual(_truncate("hello", 5), "hello")

    def test_long_string_truncated(self):
        result = _truncate("hello world", 8)
        self.assertEqual(len(result), 8)
        self.assertTrue(result.endswith("…"))

    def test_truncate_width_1(self):
        result = _truncate("abc", 1)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# _render_table
# ---------------------------------------------------------------------------

class TestRenderTable(unittest.TestCase):
    def test_empty_list_renders_header_only(self):
        table = _render_table([])
        self.assertIn("Score", table)
        self.assertIn("Company", table)

    def test_contains_company_and_title(self):
        table = _render_table([_scored(company="Acme", title="ML Engineer")])
        self.assertIn("Acme", table)
        self.assertIn("ML Engineer", table)

    def test_contains_fit_score(self):
        table = _render_table([_scored(fit_score=82)])
        self.assertIn("82", table)

    def test_contains_url(self):
        table = _render_table([_scored(url="https://example.com/job/1")])
        self.assertIn("https://example.com/job/1", table)

    def test_error_items_show_err_marker(self):
        table = _render_table([_scored(fit_score=0, error="Rate limited")])
        self.assertIn("err", table)
        self.assertIn("Rate limited", table)

    def test_rows_sorted_order_preserved(self):
        """_render_table doesn't sort — caller is responsible for order."""
        items = [_scored(fit_score=90, title="A"), _scored(fit_score=50, title="B")]
        table = _render_table(items)
        pos_a = table.index("A")
        pos_b = table.index("B")
        self.assertLess(pos_a, pos_b)

    def test_rank_numbers_present(self):
        table = _render_table([_scored(), _scored(id="2", title="Other")])
        self.assertIn("1", table)
        self.assertIn("2", table)

    def test_missing_company_shows_dash(self):
        table = _render_table([_scored(company=None)])
        self.assertIn("—", table)

    def test_missing_date_shows_dash(self):
        table = _render_table([_scored(posted_date=None)])
        self.assertIn("—", table)


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

class TestRunHappyPath(unittest.TestCase):
    def test_returns_zero_on_success(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), _patch_rank():
            with tempfile.TemporaryDirectory() as d:
                code = run(no_save=True)
        self.assertEqual(code, 0)

    def test_calls_all_pipeline_steps(self):
        with _patch_profile() as mp, \
             _patch_fetch() as mf, \
             _patch_dedup() as md, \
             _patch_rank() as mr:
            run(no_save=True)
        mp.assert_called_once()
        mf.assert_called_once()
        md.assert_called_once()
        mr.assert_called_once()

    def test_seniority_passed_to_rank(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), \
             _patch_rank() as mock_rank:
            run(seniority="senior", no_save=True)
        _, kwargs = mock_rank.call_args
        self.assertEqual(kwargs.get("seniority"), "senior")

    def test_no_seniority_passes_none_to_rank(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), \
             _patch_rank() as mock_rank:
            run(seniority=None, no_save=True)
        _, kwargs = mock_rank.call_args
        self.assertIsNone(kwargs.get("seniority"))

    def test_limit_and_jobage_passed_to_fetch(self):
        with _patch_profile(), \
             _patch_fetch() as mock_fetch, \
             _patch_dedup(), _patch_rank():
            run(limit=10, jobage=7, no_save=True)
        _, kwargs = mock_fetch.call_args
        self.assertEqual(kwargs.get("limit"), 10)
        self.assertEqual(kwargs.get("jobage"), 7)

    def test_explicit_role_and_location_passed_to_fetch(self):
        with _patch_profile(), \
             _patch_fetch() as mock_fetch, \
             _patch_dedup(), _patch_rank():
            run(role="ML Engineer", location="Bangalore", no_save=True)
        args, kwargs = mock_fetch.call_args
        query = args[0] if args else kwargs.get("query")
        self.assertEqual(query["role"], "ML Engineer")
        self.assertEqual(query["location"], "Bangalore")


# ---------------------------------------------------------------------------
# run() — no new listings
# ---------------------------------------------------------------------------

class TestRunNoNewListings(unittest.TestCase):
    def test_returns_zero_when_dedup_empty(self):
        with _patch_profile(), _patch_fetch(), \
             _patch_dedup(new_listings=[]):
            code = run(no_save=True)
        self.assertEqual(code, 0)

    def test_rank_not_called_when_no_new_listings(self):
        with _patch_profile(), _patch_fetch(), \
             _patch_dedup(new_listings=[]), \
             _patch_rank() as mock_rank:
            run(no_save=True)
        mock_rank.assert_not_called()

    def test_prints_no_new_message(self, capsys=None):
        output = []
        original_print = __builtins__["print"] if isinstance(__builtins__, dict) else print
        with patch("builtins.print", side_effect=lambda *a, **k: output.append(" ".join(str(x) for x in a))):
            with _patch_profile(), _patch_fetch(), _patch_dedup(new_listings=[]):
                run(no_save=True)
        combined = "\n".join(output)
        self.assertIn("No new matching jobs found", combined)


# ---------------------------------------------------------------------------
# run() — error handling
# ---------------------------------------------------------------------------

class TestRunErrorHandling(unittest.TestCase):
    def test_returns_nonzero_on_profile_source_error(self):
        from job_scraper.profile import ProfileSourceError
        with patch("job_scraper.pipeline.load_profile",
                   side_effect=ProfileSourceError("no cv")):
            code = run()
        self.assertEqual(code, 1)

    def test_returns_nonzero_on_linkedin_error(self):
        from job_scraper.sources.linkedin import LinkedInError
        with _patch_profile(), \
             patch("job_scraper.pipeline.fetch",
                   side_effect=LinkedInError("bun not found")):
            code = run()
        self.assertEqual(code, 1)

    def test_returns_nonzero_on_unexpected_fetch_error(self):
        with _patch_profile(), \
             patch("job_scraper.pipeline.fetch",
                   side_effect=RuntimeError("network error")):
            code = run()
        self.assertEqual(code, 1)

    def test_linkedin_error_does_not_raise(self):
        from job_scraper.sources.linkedin import LinkedInError
        with _patch_profile(), \
             patch("job_scraper.pipeline.fetch",
                   side_effect=LinkedInError("site down")):
            try:
                run()
            except Exception as exc:
                self.fail(f"run() raised unexpectedly: {exc}")

    def test_profile_error_does_not_raise(self):
        from job_scraper.profile import ProfileSourceError
        with patch("job_scraper.pipeline.load_profile",
                   side_effect=ProfileSourceError("empty")):
            try:
                run()
            except Exception as exc:
                self.fail(f"run() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# run() — dry-run
# ---------------------------------------------------------------------------

class TestRunDryRun(unittest.TestCase):
    def test_rank_not_called_in_dry_run(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), \
             _patch_rank() as mock_rank:
            run(dry_run=True, no_save=True)
        mock_rank.assert_not_called()

    def test_dry_run_returns_zero(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup():
            code = run(dry_run=True, no_save=True)
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# main() — CLI parsing
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    def _run_main(self, argv, *, profile=None, listings=None, new_listings=None, scored=None):
        with _patch_profile(profile), \
             _patch_fetch(listings), \
             _patch_dedup(new_listings), \
             _patch_rank(scored):
            return main(argv)

    def test_no_args_exits_zero(self):
        code = self._run_main(["--no-save"])
        self.assertEqual(code, 0)

    def test_seniority_flag_parsed(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), \
             _patch_rank() as mock_rank:
            main(["--seniority", "senior", "--no-save"])
        _, kwargs = mock_rank.call_args
        self.assertEqual(kwargs.get("seniority"), "senior")

    def test_limit_flag_parsed(self):
        with _patch_profile(), \
             _patch_fetch() as mock_fetch, \
             _patch_dedup(), _patch_rank():
            main(["--limit", "10", "--no-save"])
        _, kwargs = mock_fetch.call_args
        self.assertEqual(kwargs.get("limit"), 10)

    def test_jobage_flag_parsed(self):
        with _patch_profile(), \
             _patch_fetch() as mock_fetch, \
             _patch_dedup(), _patch_rank():
            main(["--jobage", "7", "--no-save"])
        _, kwargs = mock_fetch.call_args
        self.assertEqual(kwargs.get("jobage"), 7)

    def test_dry_run_flag(self):
        with _patch_profile(), _patch_fetch(), _patch_dedup(), \
             _patch_rank() as mock_rank:
            main(["--dry-run", "--no-save"])
        mock_rank.assert_not_called()

    def test_role_and_location_flags(self):
        with _patch_profile(), \
             _patch_fetch() as mock_fetch, \
             _patch_dedup(), _patch_rank():
            main(["--role", "ML Engineer", "--location", "Bangalore", "--no-save"])
        args, _ = mock_fetch.call_args
        query = args[0]
        self.assertEqual(query["role"], "ML Engineer")
        self.assertEqual(query["location"], "Bangalore")

    def test_invalid_flag_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--unknown-flag"])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
