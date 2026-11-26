"""기존 import 경로(`autoetl.parser.ui`)를 유지하는 로컬 웹 UI(`autoetl.ui`) 호환 패키지."""

from __future__ import annotations

from autoetl.ui import LocalParserUiServer, create_ui_server, serve_ui

__all__ = [
    "LocalParserUiServer",
    "create_ui_server",
    "serve_ui",
]
