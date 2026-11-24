"""Auto ETL & Auto Parser: 결정론 엔진 기반 로컬 ETL·비정형 문서 파싱 플랫폼."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0b1"

if TYPE_CHECKING:
    from autoetl.doctor import DoctorCheck, DoctorReport, collect_doctor_report
    from autoetl.parser import (
        BatchManifest,
        ChunkOptions,
        DocumentChunk,
        ExportManifest,
        ParsedDocument,
        ParseOptions,
        RoutingPlan,
        chunk_document,
        export_parse_bundle,
        inspect_document,
        parse_and_export,
        parse_batch,
        parse_document,
        supported_extensions,
    )
    from autoetl.parser.ui import LocalParserUiServer, create_ui_server, serve_ui

    parse = parse_document
    inspect = inspect_document
    export_bundle = export_parse_bundle

# 최상위 SDK 심볼은 무거운 파서/UI 의존성을 지연 import해 CLI 기동과 코어 경로를 가볍게 유지한다.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BatchManifest": ("autoetl.parser", "BatchManifest"),
    "ChunkOptions": ("autoetl.parser", "ChunkOptions"),
    "DoctorCheck": ("autoetl.doctor", "DoctorCheck"),
    "DoctorReport": ("autoetl.doctor", "DoctorReport"),
    "DocumentChunk": ("autoetl.parser", "DocumentChunk"),
    "ExportManifest": ("autoetl.parser", "ExportManifest"),
    "LocalParserUiServer": ("autoetl.parser.ui", "LocalParserUiServer"),
    "ParseOptions": ("autoetl.parser", "ParseOptions"),
    "ParsedDocument": ("autoetl.parser", "ParsedDocument"),
    "RoutingPlan": ("autoetl.parser", "RoutingPlan"),
    "chunk_document": ("autoetl.parser", "chunk_document"),
    "collect_doctor_report": ("autoetl.doctor", "collect_doctor_report"),
    "create_ui_server": ("autoetl.parser.ui", "create_ui_server"),
    "export_bundle": ("autoetl.parser", "export_parse_bundle"),
    "export_parse_bundle": ("autoetl.parser", "export_parse_bundle"),
    "inspect": ("autoetl.parser", "inspect_document"),
    "inspect_document": ("autoetl.parser", "inspect_document"),
    "parse": ("autoetl.parser", "parse_document"),
    "parse_and_export": ("autoetl.parser", "parse_and_export"),
    "parse_batch": ("autoetl.parser", "parse_batch"),
    "parse_document": ("autoetl.parser", "parse_document"),
    "serve_ui": ("autoetl.parser.ui", "serve_ui"),
    "supported_extensions": ("autoetl.parser", "supported_extensions"),
}

__all__ = [
    "BatchManifest",
    "ChunkOptions",
    "DoctorCheck",
    "DoctorReport",
    "DocumentChunk",
    "ExportManifest",
    "LocalParserUiServer",
    "ParseOptions",
    "ParsedDocument",
    "RoutingPlan",
    "__version__",
    "chunk_document",
    "collect_doctor_report",
    "create_ui_server",
    "export_bundle",
    "export_parse_bundle",
    "inspect",
    "inspect_document",
    "parse",
    "parse_and_export",
    "parse_batch",
    "parse_document",
    "serve_ui",
    "supported_extensions",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    value = getattr(importlib.import_module(mod_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
