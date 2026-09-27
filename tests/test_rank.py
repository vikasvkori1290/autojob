"""Tests for job_scraper/rank.py — LLM-based ranking."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_scraper.rank import (
    BATCH_SIZE,
    _build_system_prompt,
    _build_user_message,
    _parse_scores,
    _score_batch,
    rank,
)
from job_scraper.sources import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listing(**kwargs) -> Listing:
    base: Listing = {
        "id": "111",
        "title": "Data Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/111",
        "description": "Looking for a data engineer with Python and SQL skills.",
        "posted_date": "2025-01-15",
        "source": "linkedin",
    }
    base.update(kwargs)
    return base


def _profile(**kwargs) -> dict:
    base = {
        "skills": ["Python", "SQL", "dbt", "Spark"],
        "years_experience": 5,
        "titles_held": ["Data Engineer", "Analytics Engineer"],
        "industries": ["fintech", "e-commerce"],
        "seniority": "mid",
        "location_pref": "Bangalore",
        "summary": "Data engineer with 5 years experience.",
    }
    base.update(kwargs)
    return base


def _valid_score_response(job_ids: list[str]) -> str:
    items = [
        {
            "job_id": jid,
            "fit_score": 75,
            "reasoning": "Good skills overlap.",
            "missing_skills": ["Kafka"],
        }
        for jid in job_ids
    ]
    return json.dumps(items)


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt(unittest.TestCase):
    def test_no_seniority_has_neutral_clause(self):
        prompt = _build_system_prompt(None)
        self.assertIn("No seniority filter is active", prompt)
        self.assertNotIn("{seniority_clause}", prompt)

    def test_seniority_has_active_clause(self):
        prompt = _build_system_prompt("senior")
        self.assertIn("senior", prompt)
        self.assertNotIn("No seniority filter", prompt)

    def test_seniority_lowercased_in_prompt(self):
        prompt = _build_system_prompt("SENIOR")
        self.assertIn("senior", prompt)

    def test_rubric_dimensions_present(self):
        prompt = _build_system_prompt(None)
        for phrase in ("Skills overlap", "Domain fit", "Location", "HARD GATES", "FLAGS"):
            self.assertIn(phrase, prompt)

    def test_bangalore_encoded_in_prompt(self):
        prompt = _build_system_prompt(None)
        self.assertIn("Bangalore", prompt)

    def test_language_hard_gate_encoded(self):
        prompt = _build_system_prompt(None)
        self.assertIn("language other than English", prompt)

    def test_equity_only_hard_gate_encoded(self):
        prompt = _build_system_prompt(None)
        self.assertIn("equity-only", prompt)

    def test_relocation_flag_encoded(self):
        prompt = _build_system_prompt(None)
        self.assertIn("relocation", prompt.lower())


# ---------------------------------------------------------------------------
# _build_user_message
# ---------------------------------------------------------------------------

class TestBuildUserMessage(unittest.TestCase):
    def test_contains_profile_fields(self):
        msg = _build_user_message(_profile(), [_listing()])
        self.assertIn("Python", msg)
        self.assertIn("Data Engineer", msg)

    def test_contains_listing_id(self):
        msg = _build_user_message(_profile(), [_listing(id="999")])
        self.assertIn("999", msg)

    def test_contains_all_listing_ids_in_batch(self):
        batch = [_listing(id="1"), _listing(id="2"), _listing(id="3")]
        msg = _build_user_message(_profile(), batch)
        for lid in ("1", "2", "3"):
            self.assertIn(f'"id": "{lid}"', msg)

    def test_description_included(self):
        msg = _build_user_message(_profile(), [_listing(description="Need Python expert")])
        self.assertIn("Need Python expert", msg)

    def test_missing_description_replaced_with_placeholder(self):
        msg = _build_user_message(_profile(), [_listing(description=None)])
        self.assertIn("no description provided", msg)


# ---------------------------------------------------------------------------
# _parse_scores
# ---------------------------------------------------------------------------

class TestParseScores(unittest.TestCase):
    def test_parses_valid_array(self):
        raw = _valid_score_response(["111"])
        scores = _parse_scores(raw)
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["job_id"], "111")
        self.assertEqual(scores[0]["fit_score"], 75)

    def test_strips_markdown_fences(self):
        raw = "```json\n" + _valid_score_response(["1"]) + "\n```"
        scores = _parse_scores(raw)
        self.assertEqual(len(scores), 1)

    def test_strips_plain_fences(self):
        raw = "```\n" + _valid_score_response(["1"]) + "\n```"
        scores = _parse_scores(raw)
        self.assertEqual(len(scores), 1)

    def test_raises_on_invalid_json(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_scores("not json {")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_raises_on_json_object_not_array(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_scores('{"job_id": "1"}')
        self.assertIn("array", str(ctx.exception))

    def test_fit_score_clamped_to_0_100(self):
        raw = json.dumps([{"job_id": "1", "fit_score": 150, "reasoning": "", "missing_skills": []}])
        scores = _parse_scores(raw)
        self.assertEqual(scores[0]["fit_score"], 100)

    def test_negative_fit_score_clamped_to_0(self):
        raw = json.dumps([{"job_id": "1", "fit_score": -5, "reasoning": "", "missing_skills": []}])
        scores = _parse_scores(raw)
        self.assertEqual(scores[0]["fit_score"], 0)

    def test_missing_keys_default_to_zero_values(self):
        raw = json.dumps([{"job_id": "1"}])
        scores = _parse_scores(raw)
        self.assertEqual(scores[0]["fit_score"], 0)
        self.assertEqual(scores[0]["missing_skills"], [])

    def test_all_required_keys_present(self):
        scores = _parse_scores(_valid_score_response(["1"]))
        for key in ("job_id", "fit_score", "reasoning", "missing_skills"):
            self.assertIn(key, scores[0])


# ---------------------------------------------------------------------------
# _score_batch
# ---------------------------------------------------------------------------

class TestScoreBatch(unittest.TestCase):
    def _prompt(self):
        return _build_system_prompt(None)

    def test_returns_scores_on_success(self):
        batch = [_listing(id="1"), _listing(id="2")]
        response = _valid_score_response(["1", "2"])
        with patch("job_scraper.rank.chat", return_value=response):
            scores = _score_batch(batch, _profile(), self._prompt())
        self.assertEqual(len(scores), 2)
        self.assertEqual(scores[0]["job_id"], "1")

    def test_retries_on_bad_json(self):
        batch = [_listing(id="1")]
        good = _valid_score_response(["1"])
        call_count = 0
        def mock_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not json at all"
            return good
        with patch("job_scraper.rank.chat", side_effect=mock_chat):
            scores = _score_batch(batch, _profile(), self._prompt())
        self.assertEqual(call_count, 2)
        self.assertEqual(scores[0]["fit_score"], 75)

    def test_returns_error_sentinel_when_both_attempts_fail(self):
        batch = [_listing(id="1"), _listing(id="2")]
        with patch("job_scraper.rank.chat", return_value="this is not json"):
            scores = _score_batch(batch, _profile(), self._prompt())
        self.assertEqual(len(scores), 2)
        for score in scores:
            self.assertIn("error", score)

    def test_error_sentinel_has_fit_score_zero(self):
        batch = [_listing(id="x")]
        with patch("job_scraper.rank.chat", return_value="bad"):
            scores = _score_batch(batch, _profile(), self._prompt())
        self.assertEqual(scores[0]["fit_score"], 0)

    def test_retry_uses_stricter_instruction(self):
        batch = [_listing(id="1")]
        good = _valid_score_response(["1"])
        retry_messages_seen = []
        call_count = 0
        def mock_chat(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "bad json"
            retry_messages_seen.extend(messages)
            return good
        with patch("job_scraper.rank.chat", side_effect=mock_chat):
            _score_batch(batch, _profile(), self._prompt())
        user_content = next(m["content"] for m in retry_messages_seen if m["role"] == "user")
        self.assertIn("IMPORTANT", user_content)


# ---------------------------------------------------------------------------
# rank — integration (LLM mocked)
# ---------------------------------------------------------------------------

class TestRank(unittest.TestCase):
    def _make_results_dir(self, tmp: str) -> Path:
        d = Path(tmp) / "results"
        d.mkdir()
        return d

    def test_returns_scored_listings(self):
        listings = [_listing(id="1"), _listing(id="2")]
        response = _valid_score_response(["1", "2"])
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", return_value=response):
                result = rank(listings, _profile(), results_dir=Path(d) / "r")
        self.assertEqual(len(result), 2)

    def test_sorted_by_fit_score_descending(self):
        listings = [_listing(id="1"), _listing(id="2"), _listing(id="3")]
        response = json.dumps([
            {"job_id": "1", "fit_score": 40, "reasoning": "", "missing_skills": []},
            {"job_id": "2", "fit_score": 90, "reasoning": "", "missing_skills": []},
            {"job_id": "3", "fit_score": 60, "reasoning": "", "missing_skills": []},
        ])
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", return_value=response):
                result = rank(listings, _profile(), results_dir=Path(d) / "r")
        scores = [r["fit_score"] for r in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_empty_listings_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat") as mock_chat:
                result = rank([], _profile(), results_dir=Path(d) / "r")
                mock_chat.assert_not_called()
        self.assertEqual(result, [])

    def test_saves_results_file(self):
        listings = [_listing(id="1")]
        response = _valid_score_response(["1"])
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "results"
            with patch("job_scraper.rank.chat", return_value=response):
                rank(listings, _profile(), results_dir=rdir)
            files = list(rdir.glob("*.json"))
        self.assertEqual(len(files), 1)

    def test_results_file_is_valid_json(self):
        listings = [_listing(id="1")]
        response = _valid_score_response(["1"])
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "results"
            with patch("job_scraper.rank.chat", return_value=response):
                rank(listings, _profile(), results_dir=rdir)
            content = next(rdir.glob("*.json")).read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertIsInstance(data, list)

    def test_results_file_timestamp_in_name(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "results"
            with patch("job_scraper.rank.chat", return_value=_valid_score_response(["1"])):
                rank([_listing(id="1")], _profile(), results_dir=rdir)
            fname = next(rdir.glob("*.json")).name
        # Timestamp format: 20250115T103045Z.json
        import re
        self.assertRegex(fname, r"^\d{8}T\d{6}Z\.json$")

    def test_result_contains_listing_fields(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", return_value=_valid_score_response(["1"])):
                result = rank([_listing(id="1", title="ML Engineer", company="Corp")],
                              _profile(), results_dir=Path(d) / "r")
        self.assertEqual(result[0]["title"], "ML Engineer")
        self.assertEqual(result[0]["company"], "Corp")

    def test_job_id_always_mirrors_listing_id(self):
        """LLM may return wrong job_id — we must always override with listing id."""
        wrong_id_response = json.dumps([
            {"job_id": "WRONG", "fit_score": 80, "reasoning": "", "missing_skills": []}
        ])
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", return_value=wrong_id_response):
                result = rank([_listing(id="correct-id")], _profile(), results_dir=Path(d) / "r")
        self.assertEqual(result[0]["job_id"], "correct-id")

    def test_batches_into_batch_size_calls(self):
        """7 listings with batch_size=3 should require ceil(7/3)=3 LLM calls."""
        listings = [_listing(id=str(i)) for i in range(7)]
        call_count = 0
        def mock_chat(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            # Figure out how many listings are in this batch from the user message
            user_msg = next(m["content"] for m in messages if m["role"] == "user")
            # Count ids in the message
            import re
            ids = re.findall(r'"id":\s*"(\d+)"', user_msg)
            return _valid_score_response(ids)
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", side_effect=mock_chat):
                result = rank(listings, _profile(), batch_size=3, results_dir=Path(d) / "r")
        self.assertEqual(call_count, 3)
        self.assertEqual(len(result), 7)

    def test_seniority_passed_to_prompt(self):
        """When seniority='senior', the prompt sent to the LLM must contain 'senior'."""
        captured = []
        def mock_chat(messages, **kwargs):
            captured.extend(messages)
            return _valid_score_response(["1"])
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", side_effect=mock_chat):
                rank([_listing(id="1")], _profile(), seniority="senior", results_dir=Path(d) / "r")
        system_content = next(m["content"] for m in captured if m["role"] == "system")
        self.assertIn("senior", system_content)

    def test_no_seniority_prompt_is_neutral(self):
        captured = []
        def mock_chat(messages, **kwargs):
            captured.extend(messages)
            return _valid_score_response(["1"])
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", side_effect=mock_chat):
                rank([_listing(id="1")], _profile(), seniority=None, results_dir=Path(d) / "r")
        system_content = next(m["content"] for m in captured if m["role"] == "system")
        self.assertIn("No seniority filter", system_content)

    def test_partial_batch_failure_does_not_abort_run(self):
        """A batch that fails both attempts must produce error sentinels, not raise."""
        listings = [_listing(id=str(i)) for i in range(6)]
        call_count = 0
        def mock_chat(messages, **kwargs):
            nonlocal call_count
            call_count += 1
            user_msg = next(m["content"] for m in messages if m["role"] == "user")
            import re
            ids = re.findall(r'"id":\s*"(\d+)"', user_msg)
            # First batch (ids 0-4): always return bad JSON
            if "0" in ids:
                return "broken"
            # Second batch (id 5): return valid
            return _valid_score_response(ids)
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.rank.chat", side_effect=mock_chat):
                result = rank(listings, _profile(), batch_size=5, results_dir=Path(d) / "r")
        self.assertEqual(len(result), 6)
        error_items = [r for r in result if "error" in r]
        good_items  = [r for r in result if "error" not in r]
        self.assertEqual(len(error_items), 5)
        self.assertEqual(len(good_items), 1)


# ---------------------------------------------------------------------------
# Fix: patch return_value used as callable in test_returns_error_sentinel
# ---------------------------------------------------------------------------
# The test above uses `return_value(...)` by mistake — patch it properly here.

class TestScoreBatchErrorSentinel(unittest.TestCase):
    def test_returns_error_sentinels_when_both_fail(self):
        batch = [_listing(id="1"), _listing(id="2")]
        with patch("job_scraper.rank.chat", return_value="this is not json"):
            scores = _score_batch(batch, _profile(), _build_system_prompt(None))
        self.assertEqual(len(scores), 2)
        for score in scores:
            self.assertIn("error", score)
            self.assertEqual(score["fit_score"], 0)


if __name__ == "__main__":
    unittest.main()
