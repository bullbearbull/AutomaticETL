"""무의존성 OOXML 엑셀(`.xlsx`, SpreadsheetML) 파서 패키지."""

from __future__ import annotations

from autoetl.parser.xlsx.adapter import XlsxDocumentAdapter
from autoetl.parser.xlsx.elements import extract_xlsx_pages
from autoetl.parser.xlsx.ooxml import XlsxPackage, format_excel_number

__all__ = [
    "XlsxDocumentAdapter",
    "XlsxPackage",
    "extract_xlsx_pages",
    "format_excel_number",
]
