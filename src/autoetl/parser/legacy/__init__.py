"""구형 바이너리 문서(HWP 5.0 `.hwp`, MS Word `.doc`, MS PowerPoint `.ppt`) 호환 패키지."""

from autoetl.parser.hwp import HwpDocumentAdapter
from autoetl.parser.legacy.cfb import (
    CfbDirEntry,
    CfbFile,
    OfficeArtBlip,
    OleSummaryInfo,
    extract_officeart_blips,
    read_ole_summary_info,
)
from autoetl.parser.legacy.converter import (
    convert_legacy_office_document,
    find_libreoffice_binary,
)
from autoetl.parser.legacy.doc import DocDocumentAdapter, extract_doc_pages
from autoetl.parser.legacy.ppt import PptDocumentAdapter, extract_ppt_slides

__all__ = [
    "CfbDirEntry",
    "CfbFile",
    "DocDocumentAdapter",
    "HwpDocumentAdapter",
    "OfficeArtBlip",
    "OleSummaryInfo",
    "PptDocumentAdapter",
    "convert_legacy_office_document",
    "extract_doc_pages",
    "extract_officeart_blips",
    "extract_ppt_slides",
    "find_libreoffice_binary",
    "read_ole_summary_info",
]
