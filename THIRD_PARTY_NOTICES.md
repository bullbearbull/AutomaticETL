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

## Web UI (`/next/`)

The new web screen served by `autoetl ui` at `/next/` bundles the npm packages listed below. Their full license texts ship with the build at `src/autoetl/ui/static/THIRD_PARTY_LICENSES.md`, which the build generates. All of them are verified to carry permissive licenses via `uv run python tools/npm_license_check.py`:

| Package | License | Purpose |
| --- | --- | --- |
| `@internationalized/string` | Apache-2.0 | Localized built-in strings for React Aria components |
| `clsx` | MIT | Class name joining inside React Aria components |
| `entities` | BSD-2-Clause | HTML entity decoding for `markdown-it` |
| `linkify-it` | MIT | Link detection for `markdown-it` |
| `markdown-it` | MIT | Rendering extracted Markdown as a document preview |
| `mdurl` | MIT | URL parsing and normalization for `markdown-it` |
| `punycode.js` | MIT | Domain name encoding for `markdown-it` links |
| `react` | MIT | UI component library |
| `react-aria` | Apache-2.0 | Accessibility hooks behind React Aria components |
| `react-aria-components` | Apache-2.0 | Accessible tabs, buttons, dialogs, list boxes and drop zones |
| `react-dom` | MIT | Rendering React components in the browser |
| `react-stately` | Apache-2.0 | State management behind React Aria components |
| `scheduler` | MIT | Task scheduling for React |
| `uc.micro` | MIT | Unicode character classes for `linkify-it` and `markdown-it` |
| `use-sync-external-store` | MIT | External store subscriptions for React Aria |

Build and test tools (Vite, TypeScript, Playwright, axe-core, lightningcss and the like) are used only during development and are not shipped.
