import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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
