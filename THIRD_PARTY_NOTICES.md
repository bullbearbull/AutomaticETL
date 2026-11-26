# Third-Party Notices

AutoETL incorporates or relies on open-source software under permissive licenses.

## Hangul Document Parsing & Rendering (`rhwp` / `rhwp-python`)

AutoETL uses **`rhwp`** (Rust core) via **`rhwp-python`** to parse `.hwp` (HWP 5.0) and `.hwpx` (KS X 6101 OWPML) documents and to render Hangul document pages to PNG raster images.

- **Core Engine**: `rhwp` (<https://github.com/edwardkim/rhwp>)
  - Copyright (c) Edward Kim
  - License: **MIT**
- **Python Bindings**: `rhwp-python` (<https://github.com/DanMeon/rhwp-python>)
  - Copyright (c) DanMeon
  - License: **MIT**

## Core Runtime Dependencies

All runtime dependencies are verified to carry permissive licenses via `uv run python tools/license_check.py`:

| Package | License | Purpose |
| --- | --- | --- |
| `charset-normalizer` | MIT | Character encoding detection for CSV/TSV inputs |
| `duckdb` | MIT | In-process analytical SQL, isolated table SQL & schema verification |
| `pydantic` | MIT | Typed schema contracts and Document IR validation |
| `pypdf` | BSD-3-Clause | Core PDF structure and content extraction |
| `PyYAML` | MIT | Configuration and YAML output support |
| `rhwp-python` | MIT | Rust-backed HWP / HWPX parsing and native PNG rendering (`rhwp`) |
| `sqlglot` | MIT | SQL validation, AST inspection, and PostgreSQL DDL verification |
