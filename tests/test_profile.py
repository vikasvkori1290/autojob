"""Tests for job_scraper/profile.py — profile ingestion."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from job_scraper.profile import (
    ProfileParseError,
    ProfileSourceError,
    _build_extraction_prompt,
    _collect_sources,
    _parse_llm_response,
    discover_sources,
    is_cache_fresh,
    load_profile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str = "dummy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _valid_profile_json() -> str:
    return json.dumps({
        "skills": ["Python", "SQL"],
        "years_experience": 5,
        "titles_held": ["Data Scientist"],
        "industries": ["Finance"],
        "location_pref": "Copenhagen",
        "seniority": "mid",
        "summary": "Experienced data scientist.",
    })


# ---------------------------------------------------------------------------
# _collect_sources
# ---------------------------------------------------------------------------

class TestCollectSources(unittest.TestCase):
    def test_returns_empty_for_missing_directory(self):
        self.assertEqual(_collect_sources(Path("/nonexistent/dir")), [])

    def test_ignores_gitkeep(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / ".gitkeep")
            self.assertEqual(_collect_sources(Path(d)), [])

    def test_ignores_unsupported_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / "file.docx")
            _write(Path(d) / "file.jpg")
            self.assertEqual(_collect_sources(Path(d)), [])

    def test_returns_supported_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("cv.pdf", "cv.tex", "notes.txt", "readme.md"):
                _write(Path(d) / name)
            result = _collect_sources(Path(d))
            self.assertEqual(len(result), 4)

    def test_returns_sorted_paths(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / "b.txt")
            _write(Path(d) / "a.txt")
            names = [p.name for p in _collect_sources(Path(d))]
            self.assertEqual(names, sorted(names))

    def test_ignores_hidden_files(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d) / ".hidden.pdf")
            _write(Path(d) / "visible.pdf")
            result = _collect_sources(Path(d))
            self.assertEqual([p.name for p in result], ["visible.pdf"])


# ---------------------------------------------------------------------------
# is_cache_fresh
# ---------------------------------------------------------------------------

class TestIsCacheFresh(unittest.TestCase):
    def test_missing_output_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            src = _write(Path(d) / "cv.txt")
            self.assertFalse(is_cache_fresh(Path(d) / "profile.json", [src]))

    def test_empty_sources_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            out = _write(Path(d) / "profile.json", "{}")
            self.assertFalse(is_cache_fresh(out, []))

    def test_cache_newer_than_sources_is_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            src = _write(Path(d) / "cv.txt", "resume text")
            time.sleep(0.05)
            out = _write(Path(d) / "profile.json", "{}")
            self.assertTrue(is_cache_fresh(out, [src]))

    def test_source_newer_than_cache_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            out = _write(Path(d) / "profile.json", "{}")
            time.sleep(0.05)
            src = _write(Path(d) / "cv.txt", "resume text")
            self.assertFalse(is_cache_fresh(out, [src]))

    def test_one_newer_source_makes_whole_cache_stale(self):
        with tempfile.TemporaryDirectory() as d:
            old_src = _write(Path(d) / "old.txt", "old")
            time.sleep(0.05)
            out = _write(Path(d) / "profile.json", "{}")
            time.sleep(0.05)
            new_src = _write(Path(d) / "new.txt", "new")
            self.assertFalse(is_cache_fresh(out, [old_src, new_src]))


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

class TestParseLlmResponse(unittest.TestCase):
    def test_parses_valid_json(self):
        result = _parse_llm_response(_valid_profile_json())
        self.assertEqual(result["skills"], ["Python", "SQL"])
        self.assertEqual(result["years_experience"], 5)

    def test_strips_markdown_fences(self):
        fenced = "```json\n" + _valid_profile_json() + "\n```"
        result = _parse_llm_response(fenced)
        self.assertEqual(result["seniority"], "mid")

    def test_strips_plain_fences(self):
        fenced = "```\n" + _valid_profile_json() + "\n```"
        result = _parse_llm_response(fenced)
        self.assertIn("skills", result)

    def test_raises_on_invalid_json(self):
        with self.assertRaises(ProfileParseError) as ctx:
            _parse_llm_response("not json at all")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_raises_on_json_array(self):
        with self.assertRaises(ProfileParseError):
            _parse_llm_response("[1, 2, 3]")

    def test_fills_missing_keys_with_defaults(self):
        """A response missing some keys must get zero-value defaults, not KeyError."""
        partial = json.dumps({"skills": ["Go"], "years_experience": 3})
        result = _parse_llm_response(partial)
        self.assertEqual(result["titles_held"], [])
        self.assertEqual(result["location_pref"], "")
        self.assertEqual(result["summary"], "")

    def test_coerces_years_experience_string_to_int(self):
        data = json.loads(_valid_profile_json())
        data["years_experience"] = "7"
        result = _parse_llm_response(json.dumps(data))
        self.assertIsInstance(result["years_experience"], int)
        self.assertEqual(result["years_experience"], 7)

    def test_coerces_skills_string_to_list(self):
        data = json.loads(_valid_profile_json())
        data["skills"] = "Python"
        result = _parse_llm_response(json.dumps(data))
        self.assertIsInstance(result["skills"], list)

    def test_all_required_keys_present(self):
        result = _parse_llm_response(_valid_profile_json())
        for key in ("skills", "years_experience", "titles_held", "industries",
                    "location_pref", "seniority", "summary"):
            self.assertIn(key, result)


# ---------------------------------------------------------------------------
# _build_extraction_prompt
# ---------------------------------------------------------------------------

class TestBuildExtractionPrompt(unittest.TestCase):
    def test_includes_filename_header(self):
        prompt = _build_extraction_prompt([(Path("resume.pdf"), "Some resume text")])
        self.assertIn("resume.pdf", prompt)
        self.assertIn("Some resume text", prompt)

    def test_includes_all_sources(self):
        sources = [
            (Path("cv.pdf"), "CV content"),
            (Path("linkedin.pdf"), "LinkedIn content"),
        ]
        prompt = _build_extraction_prompt(sources)
        self.assertIn("cv.pdf", prompt)
        self.assertIn("linkedin.pdf", prompt)
        self.assertIn("CV content", prompt)
        self.assertIn("LinkedIn content", prompt)


# ---------------------------------------------------------------------------
# discover_sources
# ---------------------------------------------------------------------------

class TestDiscoverSources(unittest.TestCase):
    def test_raises_when_cv_dir_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            with self.assertRaises(ProfileSourceError) as ctx:
                discover_sources(cv_dir, linkedin_dir)
        self.assertIn("documents/cv", str(ctx.exception))

    def test_raises_when_cv_dir_has_only_gitkeep(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            _write(cv_dir / ".gitkeep")
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            with self.assertRaises(ProfileSourceError):
                discover_sources(cv_dir, linkedin_dir)

    def test_error_message_mentions_add_file(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            with self.assertRaises(ProfileSourceError) as ctx:
                discover_sources(cv_dir, linkedin_dir)
        self.assertIn("Add your resume", str(ctx.exception))

    def test_returns_cv_sources(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            _write(cv_dir / "resume.pdf")
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            cv_sources, linkedin_sources = discover_sources(cv_dir, linkedin_dir)
        self.assertEqual(len(cv_sources), 1)
        self.assertEqual(cv_sources[0].name, "resume.pdf")
        self.assertEqual(linkedin_sources, [])

    def test_linkedin_optional(self):
        """linkedin_dir being empty must not raise."""
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            _write(cv_dir / "resume.txt", "resume")
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            cv_sources, linkedin_sources = discover_sources(cv_dir, linkedin_dir)
        self.assertEqual(len(cv_sources), 1)
        self.assertEqual(linkedin_sources, [])

    def test_both_sources_returned(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            _write(cv_dir / "resume.pdf")
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            _write(linkedin_dir / "linkedin_export.pdf")
            cv_sources, linkedin_sources = discover_sources(cv_dir, linkedin_dir)
        self.assertEqual(len(cv_sources), 1)
        self.assertEqual(len(linkedin_sources), 1)


# ---------------------------------------------------------------------------
# load_profile — integration (LLM mocked)
# ---------------------------------------------------------------------------

class TestLoadProfile(unittest.TestCase):
    def _make_dirs(self, tmp: str, cv_content: str = "Senior Python developer. 8 years."):
        cv_dir = Path(tmp) / "cv"
        cv_dir.mkdir()
        _write(cv_dir / "resume.txt", cv_content)
        linkedin_dir = Path(tmp) / "linkedin"
        linkedin_dir.mkdir()
        return cv_dir, linkedin_dir

    def test_raises_when_no_cv(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            linkedin_dir = Path(d) / "linkedin"
            linkedin_dir.mkdir()
            with self.assertRaises(ProfileSourceError):
                load_profile(cv_dir, linkedin_dir, Path(d) / "profile.json")

    def test_calls_llm_and_writes_profile(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()):
                result = load_profile(cv_dir, linkedin_dir, output)
            self.assertTrue(output.is_file())
            self.assertEqual(result["skills"], ["Python", "SQL"])
            self.assertEqual(result["years_experience"], 5)

    def test_saved_json_is_valid(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()):
                load_profile(cv_dir, linkedin_dir, output)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("skills", saved)

    def test_cache_hit_skips_llm(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            # Prime the cache
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()):
                load_profile(cv_dir, linkedin_dir, output)
            # Second call — LLM must not be called again
            with patch("job_scraper.profile.chat") as mock_chat:
                result = load_profile(cv_dir, linkedin_dir, output)
                mock_chat.assert_not_called()
            self.assertEqual(result["skills"], ["Python", "SQL"])

    def test_stale_cache_calls_llm_again(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            # Write a cache file first
            _write(output, _valid_profile_json())
            time.sleep(0.05)
            # Touch the source file — now it's newer than the cache
            (cv_dir / "resume.txt").write_text("updated resume", encoding="utf-8")
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()) as mock_chat:
                load_profile(cv_dir, linkedin_dir, output)
                mock_chat.assert_called_once()

    def test_returns_all_schema_keys(self):
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()):
                result = load_profile(cv_dir, linkedin_dir, output)
        for key in ("skills", "years_experience", "titles_held", "industries",
                    "location_pref", "seniority", "summary"):
            self.assertIn(key, result)

    def test_profile_written_atomically(self):
        """Output file must exist and be valid JSON even if we check right after writing."""
        with tempfile.TemporaryDirectory() as d:
            cv_dir, linkedin_dir = self._make_dirs(d)
            output = Path(d) / "profile.json"
            with patch("job_scraper.profile.chat", return_value=_valid_profile_json()):
                load_profile(cv_dir, linkedin_dir, output)
            # No temp file should be left behind
            leftovers = list(Path(d).glob(".profile.*.tmp"))
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
