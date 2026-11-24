# AutoETL

[![CI](https://github.com/bullbearbull/AutomaticETL/actions/workflows/ci.yml/badge.svg)](https://github.com/bullbearbull/AutomaticETL/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)

[한국어](README.md) · [Parser Details](src/autoetl/parser/README.md) · [Changelog](CHANGELOG.md)

A local document parser and ETL tool that runs in a single Python process.  
It extracts clean Markdown, CSV/JSON tables, and RAG-ready chunks (`chunks.jsonl`) from PDF, Microsoft Office, and Korean Hancom (HWP/HWPX) documents without sending files to external APIs or pulling in heavy ML runtimes.

> Status: **`v0.1.0-beta.1`** (installable from Git; not yet on PyPI).
> - **Document parser (`parse`, `ui`, `doctor`)**: ready to use across 8 formats, with chunking, folder batching, and a local web UI.
> - **Structured ETL (`analyze`)**: early `--no-llm` CSV prototype; LLM harness and PostgreSQL DDL integration are in progress.

---

## Install

Requires Python 3.10+ on Windows, macOS, or Linux.

```bash
pip install git+https://github.com/bullbearbull/AutomaticETL.git
```

For local development with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/bullbearbull/AutomaticETL.git
cd AutomaticETL
uv sync
```

---

## Usage

### Parse a single file

```bash
autoetl parse report.pdf --out ./out
```

This writes a self-contained bundle under `./out`:

```text
out/
├── document.md         # unified Markdown (body, tables, footnotes, formulas)
├── chunks.jsonl        # RAG chunks preserving heading paths, pages, and table headers
├── parsed.json         # full structured IR (Pydantic schema)
├── routing_report.md   # per-page/sheet structural diagnosis and routing plan
├── routing_report.json
├── tables/             # extracted tables as .csv and .json
└── crops/              # cropped figures, charts, diagrams, and scan regions
```

You can also stream directly to stdout without writing a directory:

```bash
autoetl parse report.pdf --plan-only       # structural routing plan only
autoetl parse report.pdf --format markdown # Markdown to stdout
autoetl parse report.pdf --format chunks   # JSONL chunks to stdout
```

### Batch-parse a folder

Pass a directory to recursively discover and parse all supported files:

```bash
autoetl parse ./docs --out ./batch_out --workers 4 --resume
```

- Corrupt or encrypted files are isolated so the rest of the batch finishes cleanly.
- `--resume` skips files whose SHA-256 already succeeded in a prior run.
- Produces `batch_summary.md`, `batch_manifest.json`, and a merged `all_chunks.jsonl`.

### Local web UI

```bash
autoetl ui
```

Starts a stdlib-only web UI on `http://127.0.0.1:8765` (loopback only by default). Drop a file to inspect Markdown, tables (with CSV/Markdown copy), searchable RAG chunks, and cropped visuals, or download the full bundle as a ZIP.

### Python API

```python
import autoetl

doc = autoetl.parse("notice.hwp")
chunks = autoetl.chunk_document(doc, options=autoetl.ChunkOptions(max_chars=1500))
manifest = autoetl.export_bundle(doc, "./out_hwp")

batch = autoetl.parse_batch("./docs", "./batch_out", workers=4, resume=True)
```

### Environment check

```bash
autoetl doctor
```

Shows all 8 built-in formats and which optional backends (LibreOffice, PDF renderer, ONNX, Tesseract) are available in your environment.

---

## Supported Formats

Aside from `pypdf` for PDFs, all parsers are pure Python using only the standard library.

| Format | Built-in engine | Notes |
| --- | --- | --- |
| `.pdf` | `pypdf` + geometry/table/image extractor | Multi-column reading order, ruled & borderless tables, scan crops |
| `.docx` | Pure-Python OOXML parser | Styles, multi-level lists, track changes, OMML math, tables, footnotes, comments |
| `.pptx` | Pure-Python OOXML parser | Layouts, grouped shapes, tables, charts, embedded Excel, speaker notes |
| `.xlsx` | Streaming OOXML parser | Sheets, merged cells, multi-row headers, date/number formats, comments, charts |
| `.hwpx` | Pure-Python OWPML parser | Outlines, merged grid tables, Hancom equations, headers/footers, footnotes, comments |
| `.hwp` | Pure-Python HWP 5.0 (OLE2) parser | Body text, merged tables, equations, footnotes, BinData images (rejects encrypted/DRM) |
| `.doc`, `.ppt` | Pure-Python OLE2 parser (+ LibreOffice upgrade) | Uses `soffice` automatically when installed; falls back to built-in parser otherwise |

Pass `--include-comments` to include review comments / hidden notes. Encrypted or DRM-protected files are rejected with a clear error rather than bypassed.

---

## Optional OCR & VLM Backends

Digital documents need no OCR or external models. For scanned PDFs or image-heavy slides, attach a local backend when needed:

```bash
# Built-in heuristic layout split + local Tesseract OCR
autoetl parse scanned.pdf --layout-detector heuristic --vision tesseract --out ./out

# Local Ollama or OpenAI-compatible server (vLLM, llama-server, etc.)
autoetl parse scanned.pdf \
  --vision openai-compat --vision-model PaddleOCR-VL-1.6 \
  --vision-base-url http://127.0.0.1:8080/v1 --out ./out
```

- External endpoints are blocked by default; opt in explicitly with `--allow-external-vision` and pass the secret via an env var name (`--vision-api-key-env`).
- Logs and error messages never include raw document text or cell values.

---

## Structured CSV Analysis (WIP)

The CSV profiling and schema-design pipeline is still at v0 (`--no-llm` only):

```bash
autoetl analyze sample.csv --no-llm --out report.json
```

Statistics, type inference, relation discovery, and PostgreSQL DDL generation are computed deterministically in code, with LLM assistance planned only at fixed decision points.

---

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python tools/license_check.py
```

CI runs tests, linting, strict `mypy`, and runtime dependency license checks across Windows, macOS, and Linux on Python 3.10–3.13. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

[Apache-2.0](LICENSE)
