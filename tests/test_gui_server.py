"""Tests for job_scraper/gui/server.py — FastAPI GUI server."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from job_scraper.gui.server import (
    DEFAULT_PORT,
    _RunState,
    _run_pipeline,
    app,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# _RunState unit tests (no HTTP needed)
# ---------------------------------------------------------------------------

class TestRunState(unittest.TestCase):
    def test_initial_status_is_idle(self):
        s = _RunState()
        self.assertEqual(s.status, "idle")

    def test_reset_sets_running(self):
        s = _RunState()
        s.reset("20250115T120000Z", "senior")
        self.assertEqual(s.status, "running")
        self.assertEqual(s.run_id, "20250115T120000Z")
        self.assertEqual(s.seniority, "senior")
        self.assertEqual(s.log, [])
        self.assertEqual(s.results, [])

    def test_reset_clears_previous_log(self):
        s = _RunState()
        s.reset("id1", None)
        s.append_log("some message")
        s.reset("id2", None)
        self.assertEqual(s.log, [])

    def test_append_log(self):
        s = _RunState()
        s.append_log("hello")
        s.append_log("world")
        self.assertEqual(s.log, ["hello", "world"])

    def test_finish_sets_done(self):
        s = _RunState()
        s.reset("id", None)
        s.finish([{"fit_score": 80}])
        self.assertEqual(s.status, "done")
        self.assertEqual(s.new_count, 1)
        self.assertEqual(len(s.results), 1)

    def test_fail_sets_error(self):
        s = _RunState()
        s.reset("id", None)
        s.fail("something broke")
        self.assertEqual(s.status, "error")
        self.assertTrue(any("something broke" in l for l in s.log))

    def test_is_running_true_while_running(self):
        s = _RunState()
        s.reset("id", None)
        self.assertTrue(s.is_running())

    def test_is_running_false_when_idle(self):
        s = _RunState()
        self.assertFalse(s.is_running())

    def test_is_running_false_after_finish(self):
        s = _RunState()
        s.reset("id", None)
        s.finish([])
        self.assertFalse(s.is_running())

    def test_snapshot_returns_copy(self):
        s = _RunState()
        snap = s.snapshot()
        snap["status"] = "mutated"
        self.assertEqual(s.status, "idle")

    def test_snapshot_keys(self):
        s = _RunState()
        snap = s.snapshot()
        for key in ("status", "log", "results", "run_id", "new_count", "seniority"):
            self.assertIn(key, snap)

    def test_thread_safety_append_log(self):
        import threading
        s = _RunState()
        s.reset("id", None)

        def _worker():
            for _ in range(50):
                s.append_log("x")

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(s.log), 500)


# ---------------------------------------------------------------------------
# FastAPI Endpoints Tests
# ---------------------------------------------------------------------------

class TestServerEndpoints(unittest.TestCase):
    def setUp(self):
        from job_scraper.gui import server as srv
        srv._STATE.status = "idle"
        srv._STATE.log = []
        srv._STATE.results = []
        srv._STATE.run_id = ""

    # ── Seen count ─────────────────────────────────────────────────────────

    def test_seen_count_returns_zero_when_no_file(self):
        with patch("job_scraper.gui.server._SEEN_PATH", Path("/nonexistent/seen.json")):
            r = client.get("/api/seen-count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 0)

    def test_seen_count_returns_count(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "seen_jobs.json"
            p.write_text(json.dumps({"seen": {"a": {}, "b": {}, "c": {}}}))
            with patch("job_scraper.gui.server._SEEN_PATH", p):
                r = client.get("/api/seen-count")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 3)

    # ── Profile ───────────────────────────────────────────────────────────

    def test_profile_404_when_missing(self):
        with patch("job_scraper.gui.server._PROFILE_PATH", Path("/nonexistent/profile.json")):
            r = client.get("/api/profile")
        self.assertEqual(r.status_code, 404)

    def test_profile_returns_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "profile.json"
            p.write_text(json.dumps({"skills": ["Python"], "years_experience": 5}))
            with patch("job_scraper.gui.server._PROFILE_PATH", p):
                r = client.get("/api/profile")
        self.assertEqual(r.status_code, 200)
        self.assertIn("skills", r.json())

    # ── Latest results ────────────────────────────────────────────────────

    def test_results_latest_404_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("job_scraper.gui.server._RESULTS_DIR", Path(d)):
                r = client.get("/api/results/latest")
        self.assertEqual(r.status_code, 404)

    def test_results_latest_returns_newest(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d)
            (rdir / "20250110T100000Z.json").write_text(json.dumps([{"fit_score": 50}]))
            (rdir / "20250115T100000Z.json").write_text(json.dumps([{"fit_score": 80}]))
            with patch("job_scraper.gui.server._RESULTS_DIR", rdir):
                r = client.get("/api/results/latest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["fit_score"], 80)

    # ── Scrape & Status ───────────────────────────────────────────────────

    def test_status_returns_idle_initially(self):
        r = client.get("/api/scrape/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "idle")

    def test_scrape_starts_and_returns_202(self):
        with patch("job_scraper.gui.server._run_pipeline") as mock_run:
            mock_run.side_effect = lambda *a, **k: None
            r = client.post("/api/scrape", json={"limit": 5})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "running")
        self.assertIn("run_id", r.json())

    def test_scrape_409_when_already_running(self):
        from job_scraper.gui import server as srv
        srv._STATE.status = "running"
        r = client.post("/api/scrape", json={})
        self.assertEqual(r.status_code, 409)

    # ── Settings & Rubric ─────────────────────────────────────────────────

    def test_get_settings(self):
        r = client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("api_key_configured", data)
        self.assertIn("rubric", data)
        self.assertIsInstance(data["api_key_configured"], bool)

    def test_post_rubric_saves_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            with patch("job_scraper.rank._CONFIG_PATH", cfg_path):
                payload = {
                    "location": "Mumbai, India",
                    "language": "English",
                    "seniority_default": "senior",
                    "deal_breakers": ["REJECT unpaid"],
                    "score_boosts": ["+5 for AI"],
                }
                r = client.post("/api/settings/rubric", json=payload)
                self.assertEqual(r.status_code, 200)
                self.assertTrue(cfg_path.is_file())
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["location"], "Mumbai, India")

    @patch("openai.OpenAI")
    def test_post_api_key_valid(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.models.list.return_value = []

        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            with patch("job_scraper.gui.server._ENV_PATH", env_file):
                r = client.post("/api/settings/api-key", json={"api_key": "nvapi-test-key-1234"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "ok")
                self.assertTrue(env_file.is_file())
                self.assertIn("NVIDIA_API_KEY=nvapi-test-key-1234", env_file.read_text())

    @patch("openai.OpenAI")
    def test_post_api_key_invalid_fails(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.models.list.side_effect = Exception("401 Unauthorized")

        r = client.post("/api/settings/api-key", json={"api_key": "invalid-key"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Validation failed", r.json()["detail"])

    # ── Profile Upload ────────────────────────────────────────────────────

    def test_upload_rejects_disallowed_extension(self):
        r = client.post(
            "/api/profile/upload",
            files={"file": ("malicious.exe", b"binarycontent", "application/octet-stream")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Unsupported file extension", r.json()["detail"])

    def test_upload_rejects_invalid_pdf(self):
        r = client.post(
            "/api/profile/upload",
            files={"file": ("fake.pdf", b"not a real pdf content", "application/pdf")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("missing %PDF- header", r.json()["detail"])

    @patch("job_scraper.gui.server.load_profile")
    def test_upload_valid_resume(self, mock_load):
        mock_load.return_value = {"skills": ["Python", "FastAPI"], "years_experience": 4}
        with tempfile.TemporaryDirectory() as d:
            cv_dir = Path(d) / "cv"
            with patch("job_scraper.gui.server._DOCUMENTS_CV", cv_dir):
                r = client.post(
                    "/api/profile/upload",
                    files={"file": ("resume.txt", b"Vikas V - Python Developer with 4 years experience", "text/plain")},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "ok")
                self.assertEqual(r.json()["profile"]["skills"], ["Python", "FastAPI"])
                self.assertTrue((cv_dir / "resume.txt").is_file())

    def test_reset_all(self):
        with tempfile.TemporaryDirectory() as d:
            seen_p = Path(d) / "seen_jobs.json"
            res_dir = Path(d) / "results"
            res_dir.mkdir()
            (res_dir / "test.json").write_text("{}", encoding="utf-8")
            prof_p = Path(d) / "profile.json"
            prof_p.write_text("{}", encoding="utf-8")
            cv_dir = Path(d) / "cv"
            cv_dir.mkdir()
            (cv_dir / "my_cv.pdf").write_text("dummy", encoding="utf-8")
            (cv_dir / ".gitkeep").write_text("", encoding="utf-8")

            with patch("job_scraper.gui.server._SEEN_PATH", seen_p), \
                 patch("job_scraper.gui.server._RESULTS_DIR", res_dir), \
                 patch("job_scraper.gui.server._PROFILE_PATH", prof_p), \
                 patch("job_scraper.gui.server._DOCUMENTS_CV", cv_dir), \
                 patch("job_scraper.gui.server._update_env_file"), \
                 patch("job_scraper.gui.server.reset_client"):
                r = client.post("/api/reset-all")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "ok")
                self.assertTrue(seen_p.is_file())
                self.assertIn('"seen": {}', seen_p.read_text())
                self.assertFalse((res_dir / "test.json").exists())
                self.assertFalse(prof_p.exists())
                self.assertFalse((cv_dir / "my_cv.pdf").exists())
                self.assertTrue((cv_dir / ".gitkeep").exists())


if __name__ == "__main__":
    unittest.main()

