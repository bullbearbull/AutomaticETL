"""Auto ETL & Auto Parser: 결정론 엔진 기반 로컬 ETL·비정형 문서 파싱 플랫폼."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.2.0b4"

if TYPE_CHECKING:
    from autoetl.contracts import (
        ColumnOverride,
        DatasetKind,
        DbLoadResult,
        MaskingPolicy,
        MultiTableAnalysisBundle,
        OrphanPolicy,
        Purpose,
        SchemaOverrideSpec,
    )
    from autoetl.core.db_loader import (
        inspect_db_catalog,
        load_tables,
        render_management_tables_ddl,
        rollback_batch,
    )
    from autoetl.core.load_planner import export_multi_table_bundle, render_analysis_report
    from autoetl.core.multi_analyze import analyze_tables
    from autoetl.core.multi_context import MultiTableContext, NamedTableInput
    from autoetl.core.string_pattern import analyze_string_pattern
    from autoetl.docindex import (
        DocumentIndex,
        DocumentToolkit,
        EvidencePack,
        OnnxCrossEncoderReranker,
        build_document_index,
        render_evidence_pack_markdown,
    )
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
    "ColumnOverride": ("autoetl.contracts", "ColumnOverride"),
    "DatasetKind": ("autoetl.contracts", "DatasetKind"),
    "DbLoadResult": ("autoetl.contracts", "DbLoadResult"),
    "DoctorCheck": ("autoetl.doctor", "DoctorCheck"),
    "DoctorReport": ("autoetl.doctor", "DoctorReport"),
    "DocumentChunk": ("autoetl.parser", "DocumentChunk"),
    "DocumentIndex": ("autoetl.docindex", "DocumentIndex"),
    "DocumentToolkit": ("autoetl.docindex", "DocumentToolkit"),
    "EvidencePack": ("autoetl.docindex", "EvidencePack"),
    "ExportManifest": ("autoetl.parser", "ExportManifest"),
    "LocalParserUiServer": ("autoetl.parser.ui", "LocalParserUiServer"),
    "MaskingPolicy": ("autoetl.contracts", "MaskingPolicy"),
    "MultiTableAnalysisBundle": ("autoetl.contracts", "MultiTableAnalysisBundle"),
    "MultiTableContext": ("autoetl.core.multi_context", "MultiTableContext"),
    "NamedTableInput": ("autoetl.core.multi_context", "NamedTableInput"),
    "OnnxCrossEncoderReranker": ("autoetl.docindex", "OnnxCrossEncoderReranker"),
    "OrphanPolicy": ("autoetl.contracts", "OrphanPolicy"),
    "ParseOptions": ("autoetl.parser", "ParseOptions"),
    "ParsedDocument": ("autoetl.parser", "ParsedDocument"),
    "Purpose": ("autoetl.contracts", "Purpose"),
    "RoutingPlan": ("autoetl.parser", "RoutingPlan"),
    "SchemaOverrideSpec": ("autoetl.contracts", "SchemaOverrideSpec"),
    "analyze_string_pattern": ("autoetl.core.string_pattern", "analyze_string_pattern"),
    "analyze_tables": ("autoetl.core.multi_analyze", "analyze_tables"),
    "build_document_index": ("autoetl.docindex", "build_document_index"),
    "chunk_document": ("autoetl.parser", "chunk_document"),
    "collect_doctor_report": ("autoetl.doctor", "collect_doctor_report"),
    "create_ui_server": ("autoetl.parser.ui", "create_ui_server"),
    "export_bundle": ("autoetl.parser", "export_parse_bundle"),
    "export_multi_table_bundle": ("autoetl.core.load_planner", "export_multi_table_bundle"),
    "export_parse_bundle": ("autoetl.parser", "export_parse_bundle"),
    "inspect": ("autoetl.parser", "inspect_document"),
    "inspect_db_catalog": ("autoetl.core.db_loader", "inspect_db_catalog"),
    "inspect_document": ("autoetl.parser", "inspect_document"),
    "load_tables": ("autoetl.core.db_loader", "load_tables"),
    "parse": ("autoetl.parser", "parse_document"),
    "parse_and_export": ("autoetl.parser", "parse_and_export"),
    "parse_batch": ("autoetl.parser", "parse_batch"),
    "parse_document": ("autoetl.parser", "parse_document"),
    "render_analysis_report": ("autoetl.core.load_planner", "render_analysis_report"),
    "render_evidence_pack_markdown": ("autoetl.docindex", "render_evidence_pack_markdown"),
    "render_management_tables_ddl": ("autoetl.core.db_loader", "render_management_tables_ddl"),
    "rollback_batch": ("autoetl.core.db_loader", "rollback_batch"),
    "serve_ui": ("autoetl.parser.ui", "serve_ui"),
    "supported_extensions": ("autoetl.parser", "supported_extensions"),
}

__all__ = [
    "BatchManifest",
    "ChunkOptions",
    "ColumnOverride",
    "DatasetKind",
    "DbLoadResult",
    "DoctorCheck",
    "DoctorReport",
    "DocumentChunk",
    "DocumentIndex",
    "DocumentToolkit",
    "EvidencePack",
    "ExportManifest",
    "LocalParserUiServer",
    "MaskingPolicy",
    "MultiTableAnalysisBundle",
    "MultiTableContext",
    "NamedTableInput",
    "OnnxCrossEncoderReranker",
    "OrphanPolicy",
    "ParseOptions",
    "ParsedDocument",
    "Purpose",
    "RoutingPlan",
    "SchemaOverrideSpec",
    "__version__",
    "analyze_string_pattern",
    "analyze_tables",
    "build_document_index",
    "chunk_document",
    "collect_doctor_report",
    "create_ui_server",
    "export_bundle",
    "export_multi_table_bundle",
    "export_parse_bundle",
    "inspect",
    "inspect_db_catalog",
    "inspect_document",
    "load_tables",
    "parse",
    "parse_and_export",
    "parse_batch",
    "parse_document",
    "render_analysis_report",
    "render_evidence_pack_markdown",
    "render_management_tables_ddl",
    "rollback_batch",
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
