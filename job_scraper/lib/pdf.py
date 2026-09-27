"""PDF text extraction for the job_scraper pipeline.

Extracts the text layer from a PDF so Phase 2 can read a candidate's
resume without re-implementing extraction logic.

Delegates to the proven extractors already in tools/verify_pdf.py:
- pypdf (primary, BSD-licensed, pure-Python)
- pdftotext / Poppler (fallback, if installed)

Usage::

    from job_scraper.lib.pdf import extract_text

    text = extract_text("documents/cv/my_resume.pdf")
    print(text)
"""

import sys
from pathlib import Path

# Allow importing from tools/ without installing the package.
# __file__ is <repo_root>/job_scraper/lib/pdf.py  =>  parents[2] is <repo_root>
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from verify_pdf import _extract_pypdf, _extract_pdftotext  # noqa: E402


class PDFExtractionError(Exception):
    """Raised when no extractor can read the PDF."""


def extract_text(path: str | Path) -> str:
    """Extract the full text layer from a PDF file.

    Tries pypdf first; falls back to pdftotext (Poppler) if pypdf is
    unavailable or returns an empty result.

    Args:
        path: Path to the PDF file.

    Returns:
        Extracted text as a single string (may contain newlines).

    Raises:
        FileNotFoundError:   If the file does not exist.
        PDFExtractionError:  If neither extractor can read the file.
    """
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    result = _extract_pypdf(pdf_path)
    if result is not None:
        text, _ = result
        return text

    # Fall back to pdftotext
    try:
        text, _ = _extract_pdftotext(pdf_path)
        return text
    except Exception as exc:
        raise PDFExtractionError(
            f"Could not extract text from {pdf_path}. "
            "Install pypdf (`pip install pypdf`) or poppler-utils."
        ) from exc
