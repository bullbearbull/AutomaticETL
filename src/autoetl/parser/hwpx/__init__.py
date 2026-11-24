"""KS X 6101 OWPML(`.hwpx`) 무의존성 심층 파서 서브패키지."""

from __future__ import annotations

from autoetl.parser.hwpx.adapter import HwpxDocumentAdapter
from autoetl.parser.hwpx.elements import extract_hwpx_pages, normalize_hwp_equation_script
from autoetl.parser.hwpx.owpml import HwpxHeaderCatalog, HwpxPackage
from autoetl.parser.hwpx.router import (
    AnalyzedHwpxPage,
    analyze_hwpx_page,
    build_hwpx_routing_plan,
    estimate_hwpx_body_font_size,
    find_repeating_hwpx_logos,
)

__all__ = [
    "AnalyzedHwpxPage",
    "HwpxDocumentAdapter",
    "HwpxHeaderCatalog",
    "HwpxPackage",
    "analyze_hwpx_page",
    "build_hwpx_routing_plan",
    "estimate_hwpx_body_font_size",
    "extract_hwpx_pages",
    "find_repeating_hwpx_logos",
    "normalize_hwp_equation_script",
]
