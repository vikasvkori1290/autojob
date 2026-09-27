"""Tests for job_scraper/lib/pdf.py — PDF text extraction."""

import io
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from job_scraper.lib.pdf import PDFExtractionError, extract_text


def _minimal_pdf(text: str = "Hello PDF") -> bytes:
    """Build a minimal valid single-page PDF containing `text` in a stream.

    The PDF spec requires specific cross-reference and trailer structures, but
    this minimal form is enough for pypdf to open and extract the text.
    """
    # Encode the text as a PDF content stream (very basic — works for ASCII)
    stream_content = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    stream_len = len(stream_content)

    objects = []

    # Object 1: catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    # Object 2: pages tree
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    # Object 3: page (no /Font needed for the byte-level test)
    objects.append(
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>\n"
        b"endobj\n"
    )
    # Object 4: content stream
    objects.append(
        b"4 0 obj\n"
        + f"<< /Length {stream_len} >>\n".encode()
        + b"stream\n"
        + stream_content
        + b"\nendstream\nendobj\n"
    )

    header = b"%PDF-1.4\n"
    body = b"".join(objects)
    xref_offset = len(header) + len(body)

    # Cross-reference table (minimal)
    offsets = []
    pos = len(header)
    for obj in objects:
        offsets.append(pos)
        pos += len(obj)

    xref = b"xref\n0 5\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n<< /Size 5 /Root 1 0 R >>\n"
        b"startxref\n"
        + str(xref_offset + len(body)).encode()
        + b"\n%%EOF\n"
    )

    # Correct: startxref points at xref table, not body end
    xref_pos = len(header) + len(body)
    trailer = (
        b"trailer\n<< /Size 5 /Root 1 0 R >>\n"
        b"startxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )

    return header + body + xref + trailer


class TestExtractTextFileNotFound(unittest.TestCase):
    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            extract_text("/nonexistent/path/resume.pdf")

    def test_error_message_contains_path(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            extract_text("/nonexistent/path/resume.pdf")
        self.assertIn("resume.pdf", str(ctx.exception))


class TestExtractTextWithPypdf(unittest.TestCase):
    def test_pypdf_result_is_returned_directly(self):
        """When _extract_pypdf succeeds its text is returned without touching pdftotext."""
        fake_path = Path("fake.pdf")
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=("extracted text", 1)) as mock_py, \
             patch("job_scraper.lib.pdf._extract_pdftotext") as mock_pt:
            result = extract_text(fake_path)
        self.assertEqual(result, "extracted text")
        mock_py.assert_called_once()
        mock_pt.assert_not_called()

    def test_returns_string(self):
        fake_path = Path("fake.pdf")
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=("some text\nline 2", 2)):
            result = extract_text(fake_path)
        self.assertIsInstance(result, str)


class TestExtractTextFallback(unittest.TestCase):
    def test_falls_back_to_pdftotext_when_pypdf_returns_none(self):
        fake_path = Path("fake.pdf")
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=None), \
             patch("job_scraper.lib.pdf._extract_pdftotext", return_value=("fallback text", 1)):
            result = extract_text(fake_path)
        self.assertEqual(result, "fallback text")

    def test_raises_pdf_extraction_error_when_both_fail(self):
        fake_path = Path("fake.pdf")
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=None), \
             patch("job_scraper.lib.pdf._extract_pdftotext", side_effect=Exception("not found")):
            with self.assertRaises(PDFExtractionError) as ctx:
                extract_text(fake_path)
        self.assertIn("pypdf", str(ctx.exception).lower())

    def test_extraction_error_message_contains_path(self):
        fake_path = Path("my_resume.pdf")
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=None), \
             patch("job_scraper.lib.pdf._extract_pdftotext", side_effect=Exception("fail")):
            with self.assertRaises(PDFExtractionError) as ctx:
                extract_text(fake_path)
        self.assertIn("my_resume.pdf", str(ctx.exception))


class TestExtractTextAcceptsStringPath(unittest.TestCase):
    def test_accepts_string_path(self):
        """extract_text must accept a plain str, not just a Path."""
        with patch("job_scraper.lib.pdf.Path.is_file", return_value=True), \
             patch("job_scraper.lib.pdf._extract_pypdf", return_value=("text", 1)):
            result = extract_text("some/path/file.pdf")
        self.assertEqual(result, "text")


if __name__ == "__main__":
    unittest.main()
