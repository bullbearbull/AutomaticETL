"""로컬 웹 UI의 기존 import 경로(`autoetl.parser.ui`, `autoetl.parser.ui.server`) 호환 테스트."""

from __future__ import annotations

import autoetl
import autoetl.parser.ui
import autoetl.parser.ui.server
import autoetl.ui
import autoetl.ui.server

_PUBLIC_NAMES = ["LocalParserUiServer", "create_ui_server", "serve_ui"]


def test_parser_ui_package_reexports_autoetl_ui() -> None:
    """`autoetl.parser.ui`의 공개 이름은 `autoetl.ui`의 객체와 같은 객체다."""
    assert autoetl.parser.ui.LocalParserUiServer is autoetl.ui.LocalParserUiServer
    assert autoetl.parser.ui.create_ui_server is autoetl.ui.create_ui_server
    assert autoetl.parser.ui.serve_ui is autoetl.ui.serve_ui
    assert autoetl.parser.ui.__all__ == autoetl.ui.__all__ == _PUBLIC_NAMES


def test_parser_ui_server_module_reexports_autoetl_ui() -> None:
    """`autoetl.parser.ui.server`도 같은 세 공개 이름만 `autoetl.ui`의 객체로 내보낸다."""
    assert autoetl.parser.ui.server.LocalParserUiServer is autoetl.ui.LocalParserUiServer
    assert autoetl.parser.ui.server.create_ui_server is autoetl.ui.create_ui_server
    assert autoetl.parser.ui.server.serve_ui is autoetl.ui.serve_ui
    assert autoetl.parser.ui.server.__all__ == _PUBLIC_NAMES
    assert autoetl.ui.server.create_ui_server is autoetl.ui.create_ui_server


def test_top_level_sdk_exports_point_to_autoetl_ui() -> None:
    """최상위 `autoetl`의 로컬 웹 UI 지연 export도 `autoetl.ui`의 객체를 가리킨다."""
    assert autoetl.create_ui_server is autoetl.ui.create_ui_server
    assert autoetl.serve_ui is autoetl.ui.serve_ui
    assert autoetl.LocalParserUiServer is autoetl.ui.LocalParserUiServer
