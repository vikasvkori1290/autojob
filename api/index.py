import sys
import traceback
from pathlib import Path

# Add api directory to sys.path so api/job_scraper is discoverable immediately
_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
for p in (_API_DIR, _ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from job_scraper.gui.server import app
except Exception:
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    app = FastAPI()
    err_msg = traceback.format_exc()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def catch_all(path: str):
        return PlainTextResponse(f"Startup Exception in Vercel:\n{err_msg}", status_code=500)
