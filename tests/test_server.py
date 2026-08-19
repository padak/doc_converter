"""Tests for the conversion service.

The suite is fully offline: the PDF fixtures are generated in-process with
PyMuPDF, and every other input is a literal byte string.
"""

from __future__ import annotations

import pymupdf
import pytest
from fastapi.testclient import TestClient

from converter.server import (
    ENGINE_MARKITDOWN,
    ENGINE_PASSTHROUGH,
    ENGINE_PYMUPDF,
    HTTP_UNPROCESSABLE_ENTITY,
    PAGE_SEPARATOR,
)

CONVERT_URL = "/convert"
HEALTH_URL = "/health"
HTTP_OK = 200

#: An accented name with hačeks - the round trip must preserve it verbatim,
#: including the space between the two words.
ACCENTED_NAME = "Petr Šimeček"


def _make_pdf(*pages: str) -> bytes:
    """Build a real single- or multi-page PDF containing the given texts.

    ``insert_htmlbox`` is used rather than ``insert_text`` because the base-14
    fonts reachable through ``insert_text`` cannot encode characters outside
    Latin-1, which would mangle the hačeks before extraction ever runs.
    """
    document = pymupdf.open()
    try:
        for text in pages:
            page = document.new_page()
            page.insert_htmlbox(pymupdf.Rect(50, 50, 550, 700), text)
        return document.tobytes()
    finally:
        document.close()


def _post(client: TestClient, filename: str, data: bytes, content_type: str):
    return client.post(CONVERT_URL, files={"file": (filename, data, content_type)})


# --- PDF / PyMuPDF ----------------------------------------------------------


def test_pdf_extraction_preserves_accented_words(client: TestClient) -> None:
    text = f"Prepared by {ACCENTED_NAME} for the board."
    response = _post(client, "report.pdf", _make_pdf(text), "application/pdf")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["engine"] == ENGINE_PYMUPDF
    assert payload["filename"] == "report.pdf"
    # Words survive with their diacritics and the space between them intact.
    assert ACCENTED_NAME in payload["markdown"]
    assert "Prepared by" in payload["markdown"]


def test_pdf_characters_matches_markdown_length(client: TestClient) -> None:
    response = _post(client, "report.pdf", _make_pdf("Some content."), "application/pdf")

    payload = response.json()
    assert payload["characters"] == len(payload["markdown"])


def test_pdf_pages_are_joined_with_a_blank_line(client: TestClient) -> None:
    pdf = _make_pdf("First page body.", "Second page body.")
    response = _post(client, "two-pages.pdf", pdf, "application/pdf")

    assert response.status_code == HTTP_OK
    markdown = response.json()["markdown"]
    assert PAGE_SEPARATOR in markdown
    assert markdown.index("First page") < markdown.index("Second page")


def test_corrupt_pdf_returns_readable_422(client: TestClient) -> None:
    response = _post(client, "broken.pdf", b"this is not a PDF at all", "application/pdf")

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail
    assert "Traceback" not in detail


def test_pdf_without_text_layer_returns_422(client: TestClient) -> None:
    """A structurally valid PDF with no extractable text is a 422, not a 200."""
    response = _post(client, "blank.pdf", _make_pdf(""), "application/pdf")

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert "blank.pdf" in response.json()["detail"]


# --- Passthrough ------------------------------------------------------------


@pytest.mark.parametrize("filename", ["notes.md", "notes.markdown", "notes.txt"])
def test_text_formats_pass_through_unchanged(client: TestClient, filename: str) -> None:
    source = f"# Heading\n\nA line about {ACCENTED_NAME}.\n"
    response = _post(client, filename, source.encode("utf-8"), "text/plain")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["engine"] == ENGINE_PASSTHROUGH
    assert payload["markdown"] == source
    assert payload["characters"] == len(source)


def test_uppercase_suffix_is_routed_the_same(client: TestClient) -> None:
    response = _post(client, "NOTES.TXT", b"plain content", "text/plain")

    assert response.status_code == HTTP_OK
    assert response.json()["engine"] == ENGINE_PASSTHROUGH


def test_invalid_utf8_text_returns_422(client: TestClient) -> None:
    response = _post(client, "notes.txt", b"\xff\xfe\x00broken", "text/plain")

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert "UTF-8" in response.json()["detail"]


# --- markitdown -------------------------------------------------------------


def test_html_is_converted_by_markitdown(client: TestClient) -> None:
    html = (
        "<html><body><h1>Quarterly Report</h1>"
        "<p>Revenue grew by 12 percent.</p></body></html>"
    )
    response = _post(client, "report.html", html.encode("utf-8"), "text/html")

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["engine"] == ENGINE_MARKITDOWN
    assert "Quarterly Report" in payload["markdown"]
    assert "Revenue grew by 12 percent." in payload["markdown"]
    # markitdown emits Markdown structure, not raw HTML.
    assert "<h1>" not in payload["markdown"]


def test_unconvertible_binary_returns_422(client: TestClient) -> None:
    response = _post(
        client, "mystery.xyz", b"\x00\x01\x02\x03not a document", "application/octet-stream"
    )

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "Traceback" not in detail


# --- Empty input ------------------------------------------------------------


def test_empty_file_returns_422(client: TestClient) -> None:
    response = _post(client, "empty.txt", b"", "text/plain")

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert "empty.txt" in response.json()["detail"]


def test_whitespace_only_file_returns_422(client: TestClient) -> None:
    response = _post(client, "blank.md", b"   \n\t\n  ", "text/markdown")

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY
    assert isinstance(response.json()["detail"], str)


def test_missing_file_field_is_rejected(client: TestClient) -> None:
    response = client.post(CONVERT_URL)

    assert response.status_code == HTTP_UNPROCESSABLE_ENTITY


# --- Health -----------------------------------------------------------------


def test_health_shape(client: TestClient) -> None:
    response = client.get(HEALTH_URL)

    assert response.status_code == HTTP_OK
    payload = response.json()
    assert payload["status"] == "ok"
    engines = payload["engines"]
    assert set(engines) == {"pymupdf", "markitdown"}
    assert all(isinstance(value, str) and value for value in engines.values())
    # The PyMuPDF version is a real version string, not the fallback marker.
    assert engines["pymupdf"][0].isdigit()
