"""Local FastAPI web server for the AutoJobApply GUI.

Exposes settings, profile ingestion, and scraping pipeline endpoints,
and serves the single-page frontend from gui/index.html.
"""

import argparse
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import openai
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from job_scraper.lib.llm import reset_client
from job_scraper.profile import ProfileSourceError, load_profile
from job_scraper.rank import DEFAULT_CONFIG, load_rubric_config, save_rubric_config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUI_DIR = _REPO_ROOT / "gui"
_RESULTS_DIR = _REPO_ROOT / "job_scraper" / "results"
_PROFILE_PATH = _REPO_ROOT / "job_scraper" / "profile.json"
_SEEN_PATH = _REPO_ROOT / "job_scraper" / "seen_jobs.json"
_ENV_PATH = _REPO_ROOT / ".env"
_DOCUMENTS_CV = _REPO_ROOT / "documents" / "cv"

# Cloud/Serverless environment fallback (e.g. Vercel where repo root is read-only)
if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    import tempfile
    _TMP_BASE = Path(tempfile.gettempdir()) / "autojobapply"
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    _RESULTS_DIR = _TMP_BASE / "results"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _DOCUMENTS_CV = _TMP_BASE / "cv"
    _DOCUMENTS_CV.mkdir(parents=True, exist_ok=True)
    _PROFILE_PATH = _TMP_BASE / "profile.json"
    _SEEN_PATH = _TMP_BASE / "seen_jobs.json"

DEFAULT_PORT = 4000

# ---------------------------------------------------------------------------
# Shared run state (thread-safe for single-user local tool)
# ---------------------------------------------------------------------------

class _RunState:
    def __init__(self):
        self._lock = threading.Lock()
        self.status = "idle"   # "idle" | "running" | "done" | "error"
        self.log: list[str] = []
        self.results: list[dict] = []
        self.run_id: str = ""
        self.new_count: int = 0
        self.seniority: str | None = None

    def reset(self, run_id: str, seniority: str | None) -> None:
        with self._lock:
            self.status = "running"
            self.log = []
            self.results = []
            self.run_id = run_id
            self.new_count = 0
            self.seniority = seniority

    def append_log(self, msg: str) -> None:
        with self._lock:
            self.log.append(msg)

    def finish(self, results: list[dict]) -> None:
        with self._lock:
            self.status = "done"
            self.results = results
            self.new_count = len(results)

    def fail(self, message: str) -> None:
        with self._lock:
            self.status = "error"
            self.log.append(f"ERROR: {message}")

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status":    self.status,
                "log":       list(self.log),
                "results":   list(self.results),
                "run_id":    self.run_id,
                "new_count": self.new_count,
                "seniority": self.seniority,
            }

    def is_running(self) -> bool:
        with self._lock:
            return self.status == "running"


_STATE = _RunState()


# ---------------------------------------------------------------------------
# Pipeline runner (background thread)
# ---------------------------------------------------------------------------

class _LogCapture:
    """Redirect print() inside pipeline.run() to _STATE.log."""

    def __init__(self, state: _RunState):
        self._state = state
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

    def __enter__(self):
        sys.stdout = self  # type: ignore[assignment]
        sys.stderr = self  # type: ignore[assignment]
        return self

    def __exit__(self, *_):
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr

    def write(self, text: str) -> int:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                self._state.append_log(stripped)
        return len(text)

    def flush(self):
        pass


def _run_pipeline(
    seniority: str | None,
    limit: int,
    jobage: int,
    role: str | None,
    location: str | None,
    source: str = "all",
) -> None:
    """Execute the pipeline in a background thread, updating _STATE."""
    from job_scraper.dedupe import dedup
    from job_scraper.rank import rank
    from job_scraper.sources import fetch_from_sources, fetch_detail_for_listing

    from dotenv import load_dotenv
    load_dotenv(override=True)
    from job_scraper.lib.llm import reset_client
    reset_client()

    try:
        with _LogCapture(_STATE):
            # Step 1: profile
            _STATE.append_log("[1/4] Loading candidate profile...")
            try:
                profile = load_profile()
            except ProfileSourceError as exc:
                _STATE.fail(str(exc))
                return
            except Exception as exc:
                _STATE.fail(f"Profile error: {exc}")
                return

            # Step 2: fetch
            src_label = source.upper() if source != "all" else "all sources (LinkedIn, Internshala)"
            _STATE.append_log(f"[2/4] Fetching listings from {src_label}...")
            query = None
            if role or location:
                query = {"role": role or "", "location": location or ""}
            try:
                raw = fetch_from_sources(source, query, jobage=jobage, limit=limit)
            except Exception as exc:
                _STATE.fail(f"Fetch error: {exc}")
                return
            _STATE.append_log(f"    {len(raw)} listing(s) fetched.")

            # Step 3: dedup
            _STATE.append_log("[3/4] Deduplicating...")
            new = dedup(raw)
            _STATE.append_log(
                f"    {len(new)} new ({len(raw) - len(new)} already seen)."
            )

            if not new:
                _STATE.append_log("No new matching jobs found.")
                _STATE.finish([])
                return

            _STATE.append_log(f"    Fetching job descriptions for {len(new)} listing(s)...")
            for item in new:
                if not item.get("description"):
                    try:
                        detail = fetch_detail_for_listing(item)
                        if detail and detail.get("description"):
                            item["description"] = detail["description"]
                    except Exception:
                        pass

            # Step 4: rank
            _STATE.append_log(f"[4/4] Ranking {len(new)} listing(s)...")
            scored = rank(new, profile, seniority=seniority)
            _STATE.append_log(f"    Done. {len(scored)} result(s) scored.")

        _STATE.finish(scored)

    except Exception as exc:
        _STATE.fail(f"Unexpected error: {exc}")


def _update_env_file(key: str, value: str) -> None:
    """Set or update key=value in .env file securely (if filesystem is writable)."""
    try:
        lines = []
        found = False
        if _ENV_PATH.is_file():
            for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{key}=") or line.strip().startswith(f"export {key}="):
                    lines.append(f"{key}={value}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"{key}={value}")
        _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        # Graceful fallback for read-only cloud environments (e.g. Vercel serverless)
        pass


# ---------------------------------------------------------------------------
# FastAPI Application & Models
# ---------------------------------------------------------------------------

app = FastAPI(title="AutoJobApply GUI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def normalize_api_path(request, call_next):
    """Normalize path so routes match whether Vercel strips /api or keeps it."""
    path = request.scope.get("path", "")
    if path and not path.startswith("/api") and path not in ("/", "/docs", "/openapi.json"):
        request.scope["path"] = f"/api{path}"
    return await call_next(request)


class ApiKeyRequest(BaseModel):
    api_key: str


class RubricRequest(BaseModel):
    location: str
    language: str
    seniority_default: str
    deal_breakers: List[str]
    score_boosts: List[str]


class ScrapeRequest(BaseModel):
    seniority: Optional[str] = None
    role: Optional[str] = None
    location: Optional[str] = None
    source: str = "all"
    limit: int = 25
    jobage: int = 30


# ── Frontend Static ────────────────────────────────────────────────────────

@app.get("/")
def get_index():
    index_file = _GUI_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="GUI not found")
    return FileResponse(index_file)


# ── 1. Settings Endpoints ──────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    is_set = bool(os.environ.get("NVIDIA_API_KEY", "").strip())
    # If not in env, check if it's in .env file
    if not is_set and _ENV_PATH.is_file():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("NVIDIA_API_KEY="):
                val = line.split("=", 1)[1].strip()
                if val and val != "nvapi-your-key-here":
                    is_set = True
                    os.environ["NVIDIA_API_KEY"] = val
                    break

    return {
        "api_key_configured": is_set,
        "rubric": load_rubric_config(),
    }


@app.post("/api/settings/api-key")
def set_api_key(req: ApiKeyRequest):
    key = req.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    base_url = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = os.environ.get("NIM_MODEL", "openai/gpt-oss-20b")

    # Validate with a fast authentication test call against NIM
    try:
        client = openai.OpenAI(api_key=key, base_url=base_url, timeout=10.0)
        # Checking /v1/models validates the key in ~1s without GPU queuing delay
        client.models.list()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Validation failed against NVIDIA NIM: {exc}",
        )

    # Persist and update environment
    try:
        _update_env_file("NVIDIA_API_KEY", key)
        os.environ["NVIDIA_API_KEY"] = key
        reset_client()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save .env: {exc}")

    return {"status": "ok", "message": "NVIDIA API key verified and saved successfully"}


@app.post("/api/settings/rubric")
def update_rubric(req: RubricRequest):
    data = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    save_rubric_config(data)
    return {"status": "ok", "rubric": data}


# ── 2. Profile Endpoints ───────────────────────────────────────────────────

@app.get("/api/profile")
def get_profile():
    if not _PROFILE_PATH.is_file():
        raise HTTPException(
            status_code=404,
            detail="No candidate profile generated yet. Upload a resume first.",
        )
    try:
        data = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error reading profile.json: {exc}")


@app.post("/api/profile/upload")
async def upload_resume(file: UploadFile = File(...)):
    filename = file.filename or "resume.pdf"
    suffix = Path(filename).suffix.lower()
    allowed = {".pdf", ".txt", ".md", ".tex"}
    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '{suffix}'. Allowed: {', '.join(sorted(allowed))}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Validate PDF or text
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Invalid PDF file: missing %PDF- header")
    else:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid text file: content must be UTF-8")

    # Clear out any previous resumes so only the uploaded one remains
    _DOCUMENTS_CV.mkdir(parents=True, exist_ok=True)
    for old_file in _DOCUMENTS_CV.iterdir():
        if old_file.is_file() and not old_file.name.startswith("."):
            try:
                old_file.unlink()
            except Exception:
                pass

    target_path = _DOCUMENTS_CV / filename
    target_path.write_bytes(content)

    # Invalidate profile cache so load_profile re-parses
    if _PROFILE_PATH.is_file():
        try:
            _PROFILE_PATH.unlink()
        except Exception:
            pass

    try:
        profile = load_profile(specific_file=target_path)
        return {"status": "ok", "filename": filename, "profile": profile}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Profile extraction error: {exc}")


@app.post("/api/profile/reparse")
def reparse_profile():
    if _PROFILE_PATH.is_file():
        _PROFILE_PATH.unlink()
    try:
        profile = load_profile()
        return {"status": "ok", "profile": profile}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Profile extraction error: {exc}")


# ── 3. Pipeline Endpoints ──────────────────────────────────────────────────

@app.post("/api/scrape")
def start_scrape(req: ScrapeRequest):
    if _STATE.is_running():
        raise HTTPException(status_code=409, detail="A scrape is already running")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _STATE.reset(run_id, req.seniority)

    t = threading.Thread(
        target=_run_pipeline,
        args=(req.seniority, req.limit, req.jobage, req.role, req.location, req.source),
        daemon=True,
    )
    t.start()

    return JSONResponse(status_code=202, content={"status": "running", "run_id": run_id})


@app.get("/api/scrape/status")
def get_scrape_status():
    return _STATE.snapshot()


@app.get("/api/results/latest")
def get_latest_results():
    files = sorted(_RESULTS_DIR.glob("*.json")) if _RESULTS_DIR.is_dir() else []
    if not files:
        raise HTTPException(status_code=404, detail="No results yet")
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        return {"file": files[-1].name, "results": data}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/seen-count")
def get_seen_count():
    if not _SEEN_PATH.is_file():
        return {"count": 0}
    try:
        doc = json.loads(_SEEN_PATH.read_text(encoding="utf-8"))
        seen = doc.get("seen", doc) if isinstance(doc, dict) else (doc if isinstance(doc, list) else {})
        return {"count": len(seen)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/seen/reset")
def reset_seen():
    try:
        _SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SEEN_PATH.write_text('{\n  "seen": {}\n}\n', encoding="utf-8")
        return {"status": "ok", "message": "Seen jobs reset successfully", "count": 0}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/reset-all")
def reset_all_data():
    """Complete factory reset: seen jobs, search results, resumes, profile, and API keys."""
    # 1. Clear seen jobs ledger
    try:
        _SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SEEN_PATH.write_text('{\n  "seen": {}\n}\n', encoding="utf-8")
    except Exception:
        pass

    # 2. Delete all saved scrape results
    try:
        if _RESULTS_DIR.is_dir():
            for f in _RESULTS_DIR.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Reset in-memory run state
    _STATE.reset("", None)

    # 4. Remove active candidate profile
    try:
        if _PROFILE_PATH.is_file():
            _PROFILE_PATH.unlink()
    except Exception:
        pass

    # 5. Remove all uploaded CVs in documents/cv
    try:
        if _DOCUMENTS_CV.is_dir():
            for cv_file in _DOCUMENTS_CV.iterdir():
                if cv_file.is_file() and not cv_file.name.startswith("."):
                    try:
                        cv_file.unlink()
                    except Exception:
                        pass
    except Exception:
        pass

    # 6. Clear API keys from environment, .env file, and reset client
    os.environ.pop("NVIDIA_API_KEY", None)
    os.environ.pop("GEMINI_API_KEY", None)
    _update_env_file("NVIDIA_API_KEY", "")
    reset_client()

    # 7. Restore default rubric configuration
    save_rubric_config(DEFAULT_CONFIG)

    return {
        "status": "ok",
        "message": "All data, resumes, seen jobs, results, and API keys have been reset.",
    }



# ---------------------------------------------------------------------------
# Server CLI entry point
# ---------------------------------------------------------------------------

def serve(port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    """Start the FastAPI server via uvicorn. Blocks until stopped."""
    env_port = int(os.getenv("PORT", str(port)))
    host = os.getenv("HOST", "127.0.0.1")
    is_headless = os.getenv("HEADLESS") == "1" or host == "0.0.0.0" or os.getenv("RENDER") or os.getenv("RAILWAY_STATIC_URL")
    
    url = f"http://{'localhost' if host == '0.0.0.0' else host}:{env_port}"
    print(f"AutoJobApply GUI -> {url}")
    print("Press Ctrl-C to stop.\n")
    if open_browser and not is_headless:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    uvicorn.run(app, host=host, port=env_port, log_level="info")


def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def main(argv=None) -> None:
    _force_utf8_output()
    p = argparse.ArgumentParser(
        description="Start the AutoJobApply local GUI server."
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})"
    )
    p.add_argument(
        "--no-browser", action="store_true",
        help="Don't open a browser tab automatically"
    )
    args = p.parse_args(argv)
    serve(args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
