"""HTTP service converting uploaded documents to Markdown.

The service exposes a deliberately generic contract (see README.md): a single
POST /convert endpoint taking a multipart upload and returning Markdown. The
engine used is an implementation detail reported back for observability only,
so that any other service honouring the same contract can replace this one.

Routing is by file suffix:

    .pdf                  -> PyMuPDF (AGPL) text extraction
    .md/.markdown/.txt    -> raw UTF-8 passthrough
    everything else       -> markitdown

Uploaded content is never persisted: PDFs are parsed straight from memory and
the temporary file markitdown requires is removed in a finally block.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Final

# The historical module name is `fitz`; PyMuPDF >= 1.28 deprecates importing it
# under that name, so import the canonical module and keep the familiar alias.
import pymupdf as fitz
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from markitdown import (
    FileConversionException,
    MarkItDown,
    UnsupportedFormatException,
)

# --- Conversion contract constants -----------------------------------------

PDF_SUFFIXES: Final[frozenset[str]] = frozenset({".pdf"})
PASSTHROUGH_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown", ".txt"})

ENGINE_PYMUPDF: Final[str] = "pymupdf"
ENGINE_PASSTHROUGH: Final[str] = "passthrough"
ENGINE_MARKITDOWN: Final[str] = "markitdown"

#: Inserted between the text of two consecutive PDF pages.
PAGE_SEPARATOR: Final[str] = "\n\n"

#: PyMuPDF text extraction mode and reading-order flag.
PDF_TEXT_MODE: Final[str] = "text"
PDF_TEXT_SORT: Final[bool] = True

#: Encoding assumed for plain-text passthrough uploads.
PASSTHROUGH_ENCODING: Final[str] = "utf-8"

#: Every rejected upload answers with this status; the body is {"detail": ...}.
HTTP_UNPROCESSABLE_ENTITY: Final[int] = 422

#: Reported by /health when a package is importable but carries no metadata.
UNKNOWN_VERSION: Final[str] = "installed"

FALLBACK_FILENAME: Final[str] = "upload"


# --- Logging ----------------------------------------------------------------

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Attach a stderr handler unless the host application already logs.

    Uploaded content is never logged - only metadata (name, suffix, size).
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)


_configure_logging()


class ConversionError(Exception):
    """An upload could not be turned into Markdown.

    Carries a message meant for the caller, so the API can answer with a
    readable 422 instead of leaking a traceback.
    """


# --- Engines ----------------------------------------------------------------


def _convert_pdf(data: bytes) -> str:
    """Extract text from a PDF with PyMuPDF, in reading order, page by page."""
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (fitz.FileDataError, fitz.EmptyFileError, ValueError, RuntimeError) as exc:
        raise ConversionError(f"The PDF could not be opened: {exc}") from exc

    try:
        pages = [
            page.get_text(PDF_TEXT_MODE, sort=PDF_TEXT_SORT) for page in document
        ]
    except (fitz.FileDataError, ValueError, RuntimeError) as exc:
        raise ConversionError(f"The PDF could not be read: {exc}") from exc
    finally:
        document.close()

    return PAGE_SEPARATOR.join(pages)


def _convert_passthrough(data: bytes) -> str:
    """Decode an already-textual upload as UTF-8, unchanged."""
    try:
        return data.decode(PASSTHROUGH_ENCODING)
    except UnicodeDecodeError as exc:
        raise ConversionError(
            f"The file is not valid {PASSTHROUGH_ENCODING.upper()} text: {exc}"
        ) from exc


def _convert_markitdown(data: bytes, suffix: str) -> str:
    """Convert anything else with markitdown.

    markitdown dispatches on the file extension, so the temporary file keeps
    the suffix of the original upload. The file is removed in every case.
    """
    handle, raw_path = tempfile.mkstemp(suffix=suffix)
    tmp_path = Path(raw_path)
    try:
        with open(handle, "wb") as tmp_file:
            tmp_file.write(data)
        result = MarkItDown().convert(str(tmp_path))
    except (UnsupportedFormatException, FileConversionException) as exc:
        raise ConversionError(f"The file could not be converted: {exc}") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConversionError(f"The file could not be read: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    markdown = getattr(result, "markdown", None)
    if markdown is None:
        markdown = result.text_content
    return markdown


def _dispatch(data: bytes, suffix: str) -> tuple[str, str]:
    """Route an upload to an engine by suffix. Returns (markdown, engine)."""
    if suffix in PDF_SUFFIXES:
        return _convert_pdf(data), ENGINE_PYMUPDF
    if suffix in PASSTHROUGH_SUFFIXES:
        return _convert_passthrough(data), ENGINE_PASSTHROUGH
    return _convert_markitdown(data, suffix), ENGINE_MARKITDOWN


def _pymupdf_version() -> str:
    version = getattr(fitz, "__version__", None)
    if version:
        return str(version)
    bind = getattr(fitz, "VersionBind", None)
    return str(bind) if bind else UNKNOWN_VERSION


def _markitdown_version() -> str:
    try:
        return metadata.version("markitdown")
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION


# --- Application ------------------------------------------------------------

app = FastAPI(
    title="doc_converter",
    description=(
        "Document-to-Markdown conversion over HTTP. AGPL-3.0, because its PDF "
        "engine (PyMuPDF) is AGPL-3.0."
    ),
)


@app.middleware("http")
async def log_requests(request: Request, call_next: Any) -> Response:
    """Log method, path, status and duration of every request to stderr."""
    started = perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (perf_counter() - started) * 1000
    logger.info(
        "%s %s -> %s in %.1f ms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.post("/convert")
async def convert(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Convert one uploaded document to Markdown."""
    filename = file.filename or FALLBACK_FILENAME
    suffix = Path(filename).suffix.lower()

    try:
        data = await file.read()
    finally:
        await file.close()

    logger.info("convert: name=%s suffix=%s bytes=%d", filename, suffix, len(data))

    try:
        markdown, engine = _dispatch(data, suffix)
    except ConversionError as exc:
        logger.warning("convert failed: name=%s suffix=%s: %s", filename, suffix, exc)
        raise HTTPException(
            status_code=HTTP_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if not markdown.strip():
        logger.warning("convert empty: name=%s engine=%s", filename, engine)
        raise HTTPException(
            status_code=HTTP_UNPROCESSABLE_ENTITY,
            detail=(
                f"No text could be extracted from '{filename}'. The file may be "
                "empty, or a scanned document without a text layer."
            ),
        )

    logger.info(
        "convert ok: name=%s engine=%s characters=%d", filename, engine, len(markdown)
    )
    return {
        "markdown": markdown,
        "engine": engine,
        "filename": filename,
        "characters": len(markdown),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Report liveness and the versions of the conversion engines."""
    return {
        "status": "ok",
        "engines": {
            "pymupdf": _pymupdf_version(),
            "markitdown": _markitdown_version(),
        },
    }
