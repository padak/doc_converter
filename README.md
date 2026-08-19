# doc_converter

A small HTTP service that turns an uploaded document into Markdown.

Send a file to `POST /convert`, get Markdown back. That is the whole product.
It is a single-purpose, stateless service: nothing about the caller is stored,
and the uploaded content is never written to persistent storage.

## Why this is a separate repository

This service exists on its own, under its own license, for one reason:
**license isolation**.

Its PDF engine is [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is
licensed under the **AGPL-3.0**. The consuming product,
[doc_quantization](https://github.com/padak/doc_quantization), is **Apache-2.0**
and must stay free of AGPL code - linking an AGPL library into it would force
the whole product to become AGPL.

So the AGPL code lives here, in its own AGPL-3.0 repository, and the two
programs communicate **only over this generic HTTP API, at arm's length**. No
AGPL code is imported, vendored, linked, or compiled into the Apache-2.0
consumer; it only makes network requests to a separate process. Using a service
over a network interface does not make the client a derivative work of it.

That arrangement only holds as long as the boundary stays a boundary. Two rules
follow:

1. **The API stays generic.** It exposes "document in, Markdown out" - never
   PyMuPDF concepts, options, or types. The `engine` field in the response is
   reported for observability only; callers must not branch on it for anything
   load-bearing.
2. **Nothing from this repository is copied into the consumer.** Not the client
   code, not helper functions, not the conversion logic. Only HTTP calls cross
   the line.

Because the contract is generic, **any service implementing the same two
endpoints is a drop-in replacement** for this one. Point the consumer at a
different base URL - a commercial conversion API, a permissively licensed
converter, an in-house one - and nothing else has to change. That is the point:
this repository is one interchangeable implementation, not a dependency.

## API contract

The contract is exactly the two endpoints below. Anything else this service
happens to expose is not part of it.

**The interactive docs at `/docs` don't show engine routing.** FastAPI
generates that page from the OpenAPI schema, which only knows the generic
shape of the request (one `file` upload) and response (`additionalProperties:
true` - a JSON object with no fixed fields, since `engine` and `characters`
are the interesting parts, and neither is declared as a typed response model).
Which engine actually handles a given upload is dispatch logic that runs
*inside* the handler, so it never reaches the schema. This README, not
`/docs`, is the source of truth for the routing table below and the response
shape.

### `POST /convert`

`multipart/form-data` with a single field named `file`.

Routing is by filename suffix:

| Suffix | Engine | Behaviour |
| --- | --- | --- |
| `.pdf` | `pymupdf` | Text extraction in reading order, pages joined with a blank line |
| `.md`, `.markdown`, `.txt` | `passthrough` | Returned verbatim, decoded as UTF-8 |
| everything else | `markitdown` | Converted by [markitdown](https://github.com/microsoft/markitdown) (`.docx`, `.xlsx`, `.pptx`, `.html`, ...) |

**200 response:**

```json
{
  "markdown": "# Report\n\nBody text.",
  "engine": "markitdown",
  "filename": "report.html",
  "characters": 20
}
```

- `markdown` - the converted document.
- `engine` - which engine handled it: `pymupdf`, `passthrough`, or `markitdown`.
- `filename` - the uploaded filename, echoed back.
- `characters` - `len(markdown)`, for quick sanity checks by the caller.

**422 response** - the upload could not be converted, or produced no text at
all (an empty file, or a scanned PDF with no text layer). The body carries a
human-readable message and never a traceback:

```json
{ "detail": "The PDF could not be opened: Failed to open stream" }
```

```bash
curl -sS -F "file=@report.pdf" http://localhost:8802/convert
```

### `GET /health`

Liveness plus the versions of the conversion engines.

```bash
curl -sS http://localhost:8802/health
```

```json
{
  "status": "ok",
  "engines": { "pymupdf": "1.28.2", "markitdown": "0.1.7" }
}
```

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn converter.server:app --port 8802
```

`requirements.txt` lists direct dependencies with lower bounds only. Do not
regenerate it with `pip freeze`.

Requests are logged to stderr (method, path, status, duration, plus the
filename, engine and size of each conversion). Document content is never
logged and never stored: PDFs are parsed from memory, and the temporary file
markitdown requires is deleted in a `finally` block.

### Makefile

Once the venv above exists, `make` wraps the same service as a background
process, so you don't need a dedicated terminal tab for it:

```bash
make help     # list targets
make start    # start in the background, waits until /health responds
make status   # is it running, and on which PID/port
make logs     # follow logs/converter.log
make stop     # stop it
make restart  # stop then start
make test     # .venv/bin/pytest -q
```

`make start`/`make status`/`make stop` check the actual process on
`$(PORT)` (8802 by default), not just a PID file - so they don't get fooled by
a stale file or by a server someone started manually with `uvicorn` directly.

## Works with doc_quantization

This service was built as the conversion companion of
[doc_quantization](https://github.com/padak/doc_quantization) (Apache-2.0),
a decontextualization pipeline that anonymizes documents. Running the two
together is the recommended setup: start this service (`--port 8802`), set
`conversion.service_url` to `http://localhost:8802` in doc_quantization's
`config/config.json`, and every uploaded document is converted here before
chunking — its Verify setup screen reports this service's health. The
contract is deliberately generic: any client can call it, and any converter
implementing the same two endpoints can replace this one.

## Tests

```bash
.venv/bin/pytest -q       # or: make test
```

The suite is fully offline - the PDF fixtures are generated in-process with
PyMuPDF, and no test reaches the network.

## License

**GNU Affero General Public License v3.0** - see [LICENSE](LICENSE).

This service is AGPL-3.0 because it has to be: its PDF engine, PyMuPDF, is
AGPL-3.0, and the AGPL propagates to the program it is part of. Running this
service and making it available to users over a network triggers the AGPL's
section 13 obligation to offer those users the corresponding source of this
service.

**Consumers that only call this service over HTTP are unaffected.** They are
not derivative works of it, do not become AGPL, and take on no AGPL obligation
of their own - which is precisely the reason this code sits behind an HTTP
boundary in a separate repository instead of being imported directly.
