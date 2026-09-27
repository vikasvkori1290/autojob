"""PDF text extraction for the job_scraper pipeline.

Extracts the text layer from a PDF so Phase 2 can read a candidate's
resume without re-implementing extraction logic.
"""

import sys
from pathlib import Path


class PDFExtractionError(Exception):
    """Raised when no extractor can read the PDF."""


def extract_text(path: str | Path) -> str:
    """Extract full text layer from a PDF using pypdf (primary) or tools (fallback)."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Primary: pypdf (pure Python, BSD licensed, installed via requirements.txt)
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if text.strip():
            return text
    except Exception:
        pass

    # Fallback: pdftotext via tools.verify_pdf if present
    try:
        _TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
        if str(_TOOLS_DIR) not in sys.path:
            sys.path.insert(0, str(_TOOLS_DIR))
        from verify_pdf import _extract_pdftotext
        text, _ = _extract_pdftotext(pdf_path)
        if text and text.strip():
            return text
    except Exception:
        pass

    raise PDFExtractionError(
        f"Could not extract text from {pdf_path}. Ensure pypdf is installed."
    )
