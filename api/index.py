import sys
from pathlib import Path

# Add project root directory to sys.path so job_scraper imports work on Vercel
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from job_scraper.gui.server import app
