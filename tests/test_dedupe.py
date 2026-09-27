"""Tests for job_scraper/dedupe.py — deduplication layer."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from job_scraper.dedupe import _fingerprint, _load_ledger, _save_ledger, dedup
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


def _read_seen(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")).get("seen", {})


# ---------------------------------------------------------------------------
# _fingerprint
# ---------------------------------------------------------------------------

class TestFingerprint(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(_fingerprint("Acme Corp", "Data Engineer"),
                         _fingerprint("acme corp", "data engineer"))

    def test_strips_inc(self):
        self.assertEqual(_fingerprint("Acme Inc", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_strips_ltd(self):
        self.assertEqual(_fingerprint("Acme Ltd", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_strips_llc(self):
        self.assertEqual(_fingerprint("Acme LLC", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_strips_a_s(self):
        self.assertEqual(_fingerprint("Acme A/S", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_strips_gmbh(self):
        self.assertEqual(_fingerprint("Acme GmbH", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_strips_trailing_dot_on_suffix(self):
        self.assertEqual(_fingerprint("Acme Inc.", "Engineer"),
                         _fingerprint("Acme", "Engineer"))

    def test_collapses_whitespace(self):
        self.assertEqual(_fingerprint("Acme  Corp", "  Engineer  "),
                         _fingerprint("Acme Corp", "Engineer"))

    def test_none_company(self):
        fp = _fingerprint(None, "Engineer")
        self.assertIsInstance(fp, str)
        self.assertIn("|", fp)

    def test_none_title(self):
        fp = _fingerprint("Acme", None)
        self.assertIsInstance(fp, str)

    def test_both_none(self):
        fp = _fingerprint(None, None)
        self.assertEqual(fp, "|")

    def test_different_companies_differ(self):
        self.assertNotEqual(_fingerprint("Acme", "Engineer"),
                            _fingerprint("GlobalCorp", "Engineer"))

    def test_different_titles_differ(self):
        self.assertNotEqual(_fingerprint("Acme", "Engineer"),
                            _fingerprint("Acme", "Manager"))

    def test_unicode_nfc_normalisation(self):
        # café composed vs decomposed — should produce the same fingerprint
        composed = _fingerprint("Café Corp", "Engineer")
        decomposed = _fingerprint("Cafe\u0301 Corp", "Engineer")
        self.assertEqual(composed, decomposed)


# ---------------------------------------------------------------------------
# _load_ledger / _save_ledger
# ---------------------------------------------------------------------------

class TestLedgerIO(unittest.TestCase):
    def test_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            self.assertFalse(path.exists())
            _load_ledger(path)
            self.assertTrue(path.exists())

    def test_new_file_has_empty_seen(self):
        with tempfile.TemporaryDirectory() as d:
            doc, seen = _load_ledger(Path(d) / "seen_jobs.json")
            self.assertEqual(seen, {})

    def test_loads_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"abc": {"status": "new"}}}), encoding="utf-8")
            _, seen = _load_ledger(path)
            self.assertIn("abc", seen)

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            doc = {"seen": {"xyz": {"status": "ranked"}}}
            _save_ledger(path, doc)
            _, seen = _load_ledger(path)
            self.assertEqual(seen["xyz"]["status"], "ranked")

    def test_save_is_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            _save_ledger(path, {"seen": {}})
            json.loads(path.read_text(encoding="utf-8"))  # must not raise

    def test_no_temp_file_left_after_save(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            _save_ledger(path, {"seen": {}})
            leftovers = list(Path(d).glob(".seen_jobs.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_raises_on_corrupt_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                _load_ledger(path)


# ---------------------------------------------------------------------------
# dedup — Pass 1: ID dedup
# ---------------------------------------------------------------------------

class TestDedupIdPass(unittest.TestCase):
    def test_new_id_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            result = dedup([_listing(id="abc")], path=path)
        self.assertEqual(len(result), 1)

    def test_known_id_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"abc": {"status": "new"}}}), encoding="utf-8")
            result = dedup([_listing(id="abc")], path=path)
        self.assertEqual(result, [])

    def test_mix_of_new_and_known_ids(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"old": {"status": "ranked"}}}), encoding="utf-8")
            batch = [_listing(id="old"), _listing(id="new")]
            result = dedup(batch, path=path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "new")

    def test_empty_batch_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            result = dedup([], path=Path(d) / "seen_jobs.json")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# dedup — Pass 2: fingerprint dedup within batch
# ---------------------------------------------------------------------------

class TestDedupFingerprintBatch(unittest.TestCase):
    def test_exact_duplicate_in_batch_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            batch = [
                _listing(id="1", company="Acme", title="Engineer"),
                _listing(id="2", company="Acme", title="Engineer"),
            ]
            result = dedup(batch, path=path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "1")

    def test_suffix_variant_in_batch_dropped(self):
        """'Acme Inc' and 'Acme' with same title — second must be dropped."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            batch = [
                _listing(id="1", company="Acme Inc", title="Engineer"),
                _listing(id="2", company="Acme", title="Engineer"),
            ]
            result = dedup(batch, path=path)
        self.assertEqual(len(result), 1)

    def test_different_companies_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            batch = [
                _listing(id="1", company="Acme", title="Engineer"),
                _listing(id="2", company="GlobalCorp", title="Engineer"),
            ]
            result = dedup(batch, path=path)
        self.assertEqual(len(result), 2)

    def test_different_titles_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            batch = [
                _listing(id="1", company="Acme", title="Engineer"),
                _listing(id="2", company="Acme", title="Manager"),
            ]
            result = dedup(batch, path=path)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# dedup — Pass 2: fingerprint dedup against stored ledger
# ---------------------------------------------------------------------------

class TestDedupFingerprintLedger(unittest.TestCase):
    def test_repost_with_new_id_dropped_by_fingerprint(self):
        """Same company+title already in ledger under a different ID."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            # Prime ledger with old ID
            path.write_text(json.dumps({
                "seen": {"old_id": {"status": "new", "company": "Acme", "title": "Engineer"}}
            }), encoding="utf-8")
            # New run gives a fresh ID for the same job
            result = dedup([_listing(id="new_id", company="Acme", title="Engineer")], path=path)
        self.assertEqual(result, [])

    def test_repost_with_suffix_variant_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({
                "seen": {"old": {"status": "new", "company": "Acme Inc", "title": "Engineer"}}
            }), encoding="utf-8")
            result = dedup([_listing(id="new", company="Acme Ltd", title="Engineer")], path=path)
        self.assertEqual(result, [])

    def test_genuinely_new_job_passes_fingerprint_check(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({
                "seen": {"old": {"status": "new", "company": "Acme", "title": "Engineer"}}
            }), encoding="utf-8")
            result = dedup([_listing(id="new", company="Other Corp", title="Designer")], path=path)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# dedup — persistence
# ---------------------------------------------------------------------------

class TestDedupPersistence(unittest.TestCase):
    def test_new_listings_written_to_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc")], path=path)
            seen = _read_seen(path)
        self.assertIn("abc", seen)

    def test_written_entry_has_status_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc")], path=path)
            seen = _read_seen(path)
        self.assertEqual(seen["abc"]["status"], "new")

    def test_written_entry_has_portal_field(self):
        """rank_state.py reads 'portal', not 'source'."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc", source="linkedin")], path=path)
            seen = _read_seen(path)
        self.assertEqual(seen["abc"]["portal"], "linkedin")

    def test_written_entry_has_seen_at_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc")], path=path)
            seen = _read_seen(path)
        self.assertIn("seen_at", seen["abc"])

    def test_written_entry_has_title_company_url(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc", title="ML Engineer", company="Acme",
                            url="https://example.com/job/1")], path=path)
            seen = _read_seen(path)
        entry = seen["abc"]
        self.assertEqual(entry["title"], "ML Engineer")
        self.assertEqual(entry["company"], "Acme")
        self.assertEqual(entry["url"], "https://example.com/job/1")

    def test_dropped_listings_not_written_to_ledger(self):
        """Listings dropped by either pass must not appear in the ledger."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"known": {"status": "ranked"}}}), encoding="utf-8")
            dedup([_listing(id="known")], path=path)
            seen = _read_seen(path)
        # No new key beyond the pre-existing "known"
        self.assertEqual(set(seen.keys()), {"known"})

    def test_file_not_written_when_no_new_listings(self):
        """Avoid touching the file if nothing survived dedup (preserve mtime)."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"abc": {"status": "new"}}}), encoding="utf-8")
            mtime_before = path.stat().st_mtime
            time.sleep(0.05)
            dedup([_listing(id="abc")], path=path)
            mtime_after = path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after)

    def test_second_run_excludes_first_run_results(self):
        """IDs written in run 1 must be excluded in run 2."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            dedup([_listing(id="abc")], path=path)
            result2 = dedup([_listing(id="abc")], path=path)
        self.assertEqual(result2, [])

    def test_creates_seen_jobs_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "seen_jobs.json"
            self.assertFalse(path.exists())
            dedup([_listing(id="abc")], path=path)
            self.assertTrue(path.exists())

    def test_existing_entries_not_modified(self):
        """dedup must not mutate entries that already exist in the ledger."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen_jobs.json"
            original = {"status": "ranked", "rank_score": 87, "title": "Old Job"}
            path.write_text(json.dumps({"seen": {"existing": original}}), encoding="utf-8")
            dedup([_listing(id="new")], path=path)
            seen = _read_seen(path)
        self.assertEqual(seen["existing"]["status"], "ranked")
        self.assertEqual(seen["existing"]["rank_score"], 87)


if __name__ == "__main__":
    unittest.main()
