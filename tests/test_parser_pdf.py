"""Auto Parser PDF 심층 테스트 — 구조 분류·전략 라우팅·표·다단·스캔·도식·크롭·CLI."""

from __future__ import annotations

import csv
import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from autoetl.cli import main as cli_main
from autoetl.parser import (
    CropStatus,
    ElementKind,
    PageCategory,
    ParsedDocument,
    ParseOptions,
    ParserInputError,
    ParseStrategy,
    RoutingPlan,
    VisionRequest,
    VisionResult,
    VisionStatus,
    inspect_document,
    parse_and_export,
    parse_document,
    parse_page_selection,
    render_document_markdown,
    render_routing_report_markdown,
)
from autoetl.parser.adapters import UnavailablePageRenderer

# ---------------------------------------------------------------------------
# 순수 Python PDF 1.4 픽스처 빌더 (외부 의존성 없이 정확한 벡터·이미지·폼·목차 생성)
# ---------------------------------------------------------------------------


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


@dataclass
class _PdfPageSpec:
    width: float = 595.0
    height: float = 842.0
    rotation: int = 0
    ops: list[str] = field(default_factory=list)
    images: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)  # name -> (w, h, rgb)
    forms: dict[str, tuple[list[float], str]] = field(default_factory=dict)  # name -> (matrix, ops)

    def add_text(
        self,
        text: str,
        x: float,
        top: float,
        *,
        font: str = "F1",
        size: float = 11.0,
    ) -> None:
        """좌상단 원점(top) 기준으로 PDF 텍스트 연산자를 추가한다."""
        y = self.height - top
        self.ops.append(
            f"BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_pdf_escape(text)}) Tj ET"
        )

    def add_line(
        self, x0: float, top0: float, x1: float, top1: float, *, width: float = 1.0
    ) -> None:
        y0 = self.height - top0
        y1 = self.height - top1
        self.ops.append(f"{width:.2f} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S")

    def add_rect(
        self,
        x0: float,
        top: float,
        w: float,
        h: float,
        *,
        stroke: bool = True,
        fill: bool = False,
        gray: float = 0.9,
    ) -> None:
        y = self.height - top - h
        op = "B" if (stroke and fill) else ("f" if fill else "S")
        prefix = f"{gray:.2f} g " if fill else ""
        self.ops.append(f"q {prefix}{x0:.2f} {y:.2f} {w:.2f} {h:.2f} re {op} Q")

    def add_curve(
        self,
        x0: float,
        top0: float,
        x1: float,
        top1: float,
        x2: float,
        top2: float,
        x3: float,
        top3: float,
    ) -> None:
        y0 = self.height - top0
        y1 = self.height - top1
        y2 = self.height - top2
        y3 = self.height - top3
        self.ops.append(
            f"{x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f} {x3:.2f} {y3:.2f} c S"
        )

    def add_image(
        self,
        name: str,
        x0: float,
        top: float,
        w: float,
        h: float,
        *,
        px_w: int = 16,
        px_h: int = 16,
        rgb: tuple[int, int, int] = (80, 140, 210),
    ) -> None:
        self.images[name] = (px_w, px_h, bytes(rgb) * (px_w * px_h))
        y = self.height - top - h
        self.ops.append(f"q {w:.2f} 0 0 {h:.2f} {x0:.2f} {y:.2f} cm /{name} Do Q")

    def add_form(
        self,
        name: str,
        dx: float,
        dy: float,
        inner_ops: str,
        *,
        form_matrix: list[float] | None = None,
    ) -> None:
        self.forms[name] = (form_matrix or [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], inner_ops)
        self.ops.append(f"q 1 0 0 1 {dx:.2f} {dy:.2f} cm /{name} Do Q")


def _build_pdf_bytes(
    pages: list[_PdfPageSpec],
    *,
    title: str | None = None,
    author: str | None = None,
    outlines: list[tuple[str, int]] | None = None,  # (title, 1-based page)
) -> bytes:
    """표준 PDF 1.4 바이트를 순수 Python으로 직렬화한다."""
    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # 1: Catalog (placeholder), 2: Pages (placeholder)
    catalog_id = add_obj(b"")
    pages_id = add_obj(b"")
    f1_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    f2_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    f3_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    font_dict = f"<< /F1 {f1_id} 0 R /F2 {f2_id} 0 R /F3 {f3_id} 0 R >>"

    page_ids: list[int] = []
    for p in pages:
        xobj_entries: list[str] = []
        for img_name, (pw, ph, raw_rgb) in p.images.items():
            compressed = zlib.compress(raw_rgb)
            img_obj = (
                (
                    f"<< /Type /XObject /Subtype /Image /Width {pw} /Height {ph} "
                    f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                    f"/Length {len(compressed)} >>\nstream\n"
                ).encode("ascii")
                + compressed
                + b"\nendstream"
            )
            img_id = add_obj(img_obj)
            xobj_entries.append(f"/{img_name} {img_id} 0 R")

        for form_name, (mat, inner_ops) in p.forms.items():
            mat_s = " ".join(f"{v:.2f}" for v in mat)
            inner_b = inner_ops.encode("latin-1")
            form_obj = (
                (
                    f"<< /Type /XObject /Subtype /Form /BBox [0 0 {p.width:.2f} {p.height:.2f}] "
                    f"/Matrix [{mat_s}] /Resources << /Font {font_dict} >> "
                    f"/Length {len(inner_b)} >>\nstream\n"
                ).encode("ascii")
                + inner_b
                + b"\nendstream"
            )
            form_id = add_obj(form_obj)
            xobj_entries.append(f"/{form_name} {form_id} 0 R")

        stream_bytes = "\n".join(p.ops).encode("latin-1")
        content_id = add_obj(
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        xobj_part = f" /XObject << {' '.join(xobj_entries)} >>" if xobj_entries else ""
        rot_part = f" /Rotate {p.rotation}" if p.rotation else ""
        page_dict = (
            f"<< /Type /Page /Parent {pages_id} 0 R "
            f"/MediaBox [0 0 {p.width:.2f} {p.height:.2f}]{rot_part} "
            f"/Resources << /Font {font_dict}{xobj_part} >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        page_ids.append(add_obj(page_dict))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(
        "ascii"
    )

    outlines_part = ""
    if outlines:
        root_outline_id = add_obj(b"")
        item_ids: list[int] = [add_obj(b"") for _ in outlines]
        for idx, (o_title, pg_num) in enumerate(outlines):
            prev_s = f" /Prev {item_ids[idx - 1]} 0 R" if idx > 0 else ""
            next_s = f" /Next {item_ids[idx + 1]} 0 R" if idx + 1 < len(outlines) else ""
            target_pid = page_ids[pg_num - 1]
            objects[item_ids[idx] - 1] = (
                f"<< /Title ({_pdf_escape(o_title)}) /Parent {root_outline_id} 0 R"
                f"{prev_s}{next_s} /Dest [{target_pid} 0 R /Fit] >>"
            ).encode("latin-1")
        objects[root_outline_id - 1] = (
            f"<< /Type /Outlines /First {item_ids[0]} 0 R "
            f"/Last {item_ids[-1]} 0 R /Count {len(item_ids)} >>"
        ).encode("ascii")
        outlines_part = f" /Outlines {root_outline_id} 0 R"

    objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R{outlines_part} >>".encode(
        "ascii"
    )

    info_id: int | None = None
    if title or author:
        parts = []
        if title:
            parts.append(f"/Title ({_pdf_escape(title)})")
        if author:
            parts.append(f"/Author ({_pdf_escape(author)})")
        info_id = add_obj(f"<< {' '.join(parts)} >>".encode("latin-1"))

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for obj_num, body in enumerate(objects, 1):
        offsets.append(len(buf))
        buf.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    info_part = f" /Info {info_id} 0 R" if info_id is not None else ""
    buf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R{info_part} >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(buf)


def _create_comprehensive_sample_pdf(path: Path) -> Path:
    """7가지 대표 PDF 구조가 모두 들어있는 복합 테스트 문서를 생성한다.

    - p1: 디지털 본문 + 제목 계층 + 목록 + 코드 + 반복 머리글/바닥글
    - p2: 격자형 표(병합 셀 포함) + 표 캡션 (표 중심 페이지)
    - p3: 가로선(booktabs) 재무 표 + 무선(공백정렬) 요약 표
    - p4: 전폭 제목 배너 + 2단 다단 본문 (산문을 표로 오인하지 않고 왼쪽→오른쪽 단 읽기 순서 검증)
    - p5: 전체 페이지 스캔 이미지 (내장 이미지 직접 PNG 크롭 추출 검증)
    - p6: 텍스트 + 내장 그림 + 벡터 차트(막대·곡선 + 축 레이블 힌트) 혼재 페이지
    - p7: 가로형 슬라이드 PDF + 전체 배경 이미지 위 디지털 텍스트(스캔 오인 방지 검증)
    - p8: 빈 페이지
    """
    pages: list[_PdfPageSpec] = []

    def add_hf(pg: _PdfPageSpec, page_num: int, total: int = 8) -> None:
        pg.add_text("ACME Annual Strategy Report 2026", 60, 32, font="F1", size=9.0)
        pg.add_text(f"Page {page_num} / {total}", 270, 816, font="F1", size=9.0)

    # Page 1: Digital text with headings, paragraphs, bullets, code
    p1 = _PdfPageSpec()
    add_hf(p1, 1)
    p1.add_text("1. Executive Overview", 60, 95, font="F2", size=20.0)
    p1.add_text(
        "This report summarizes quarterly revenue performance across core business divisions.",
        60,
        130,
        size=11.0,
    )
    p1.add_text(
        "All figures have been audited and reconciled against enterprise ledger systems.",
        60,
        146,
        size=11.0,
    )
    p1.add_text("1.1 Key Highlights", 60, 185, font="F2", size=14.0)
    p1.add_text("- Cloud revenue grew 34% year-over-year.", 72, 212, size=11.0)
    p1.add_text("- Enterprise retention reached 96.4% in Q4.", 72, 230, size=11.0)
    p1.add_text(
        "SELECT region, SUM(amount) FROM sales GROUP BY region;", 72, 270, font="F3", size=10.0
    )
    pages.append(p1)

    # Page 2: Ruled grid table with merged header cell (colspan=2)
    p2 = _PdfPageSpec()
    add_hf(p2, 2)
    p2.add_text("Table 1. Regional Revenue Breakdown", 60, 96, font="F2", size=11.0)
    # 4 rows x 3 cols: x=[60, 220, 370, 520], y=[115, 145, 175, 205, 235]
    # Header row shading
    p2.add_rect(60, 115, 460, 30, stroke=False, fill=True, gray=0.88)
    # Horizontal lines (5)
    for y in (115, 145, 175, 205, 235):
        p2.add_line(60, y, 520, y)
    # Outer vertical lines + col 1 separator across all rows
    p2.add_line(60, 115, 60, 235)
    p2.add_line(220, 115, 220, 235)
    p2.add_line(520, 115, 520, 235)
    # Internal col 2 separator ONLY for rows 1..3 (row 0 cols 1-2 merged!)
    p2.add_line(370, 145, 370, 235)
    # Cell texts
    p2.add_text("Region", 75, 134, font="F2", size=11.0)
    p2.add_text("2025-2026 Sales", 300, 134, font="F2", size=11.0)
    p2.add_text("Korea", 75, 164, size=11.0)
    p2.add_text("12,400", 240, 164, size=11.0)
    p2.add_text("15,800", 390, 164, size=11.0)
    p2.add_text("Japan", 75, 194, size=11.0)
    p2.add_text("8,200", 240, 194, size=11.0)
    p2.add_text("9,950", 390, 194, size=11.0)
    p2.add_text("US", 75, 224, size=11.0)
    p2.add_text("21,000", 240, 224, size=11.0)
    p2.add_text("26,300", 390, 224, size=11.0)
    pages.append(p2)

    # Page 3: Horizontal-ruled (booktabs) table + borderless whitespace table
    p3 = _PdfPageSpec()
    add_hf(p3, 3)
    p3.add_text("2. Financial Tables", 60, 92, font="F2", size=16.0)
    # Booktabs table (3 horizontal rules only, NO vertical rules)
    p3.add_line(60, 120, 520, 120, width=1.5)
    p3.add_text("Metric", 70, 138, font="F2", size=10.5)
    p3.add_text("Q1", 250, 138, font="F2", size=10.5)
    p3.add_text("Q2", 360, 138, font="F2", size=10.5)
    p3.add_text("Q3", 460, 138, font="F2", size=10.5)
    p3.add_line(60, 148, 520, 148, width=0.8)
    for idx, (m, q1, q2, q3) in enumerate(
        [
            ("Revenue", "100.5", "112.0", "128.4"),
            ("Operating Cost", "62.1", "65.0", "69.8"),
            ("Net Profit", "38.4", "47.0", "58.6"),
        ]
    ):
        top_y = 168 + idx * 24
        p3.add_text(m, 70, top_y, size=10.5)
        p3.add_text(q1, 250, top_y, size=10.5)
        p3.add_text(q2, 360, top_y, size=10.5)
        p3.add_text(q3, 460, top_y, size=10.5)
    p3.add_line(60, 232, 520, 232, width=1.5)

    # Borderless whitespace-aligned table further down (4 rows x 3 cols)
    p3.add_text("Department Headcount Summary", 60, 310, font="F2", size=13.0)
    borderless_rows = [
        ("Dept", "Staff", "Budget"),
        ("Engineering", "124", "4,200"),
        ("Product", "38", "1,450"),
        ("Operations", "52", "1,180"),
        ("Research", "29", "2,100"),
    ]
    for idx, (c1, c2, c3) in enumerate(borderless_rows):
        top_y = 345 + idx * 22
        f = "F2" if idx == 0 else "F1"
        p3.add_text(c1, 70, top_y, font=f, size=10.5)
        p3.add_text(c2, 260, top_y, font=f, size=10.5)
        p3.add_text(c3, 410, top_y, font=f, size=10.5)
    pages.append(p3)

    # Page 4: Full-width banner + 2-column prose layout
    p4 = _PdfPageSpec()
    add_hf(p4, 4)
    p4.add_text(
        "3. Market Analysis and Competitive Landscape Across Global Regions",
        60,
        92,
        font="F2",
        size=15.0,
    )
    left_lines = [
        "LEFT_START: Demand for automated data pipelines",
        "accelerated significantly across manufacturing",
        "and financial institutions throughout the year.",
        "Teams struggled with heterogeneous document",
        "formats that combined tables, charts, and prose.",
        "LEFT_END: Modular routing solved this bottleneck.",
    ]
    right_lines = [
        "RIGHT_START: Meanwhile, local deployment rules",
        "became stricter due to data governance policies.",
        "Organizations now require zero-leakage parsing",
        "engines that keep raw documents on premise while",
        "still delivering high accuracy on complex pages.",
        "RIGHT_END: Deterministic engines anchor trust.",
    ]
    for idx, txt in enumerate(left_lines):
        p4.add_text(txt, 55, 140 + idx * 20, size=10.5)
    for idx, txt in enumerate(right_lines):
        p4.add_text(txt, 320, 140 + idx * 20, size=10.5)
    pages.append(p4)

    # Page 5: Full-page scanned image (0 text, single large embedded RGB image)
    p5 = _PdfPageSpec()
    p5.add_image("ScanPage1", 10, 10, 575, 822, px_w=32, px_h=44, rgb=(220, 225, 230))
    pages.append(p5)

    # Page 6: Hybrid page — digital text + embedded figure + vector bar chart with curves & labels
    p6 = _PdfPageSpec()
    add_hf(p6, 6)
    p6.add_text("4. Visual Insights and Architecture", 60, 92, font="F2", size=16.0)
    p6.add_text(
        "The diagram below illustrates throughput growth and system layout components.",
        60,
        122,
        size=11.0,
    )
    p6.add_image("FigArch", 60, 150, 220, 150, px_w=24, px_h=16, rgb=(50, 110, 180))
    p6.add_text("Figure 1. System Architecture Screenshot", 60, 315, size=10.0)
    # Vector chart cluster on right side (x=310..530, top=150..320)
    p6.add_line(320, 300, 520, 300)  # x-axis
    p6.add_line(320, 160, 320, 300)  # y-axis
    p6.add_rect(340, 240, 28, 60, stroke=True, fill=True, gray=0.5)
    p6.add_rect(385, 210, 28, 90, stroke=True, fill=True, gray=0.4)
    p6.add_rect(430, 180, 28, 120, stroke=True, fill=True, gray=0.3)
    p6.add_rect(475, 165, 28, 135, stroke=True, fill=True, gray=0.2)
    p6.add_curve(340, 235, 385, 200, 440, 175, 495, 160)
    p6.add_text("Q1", 345, 312, size=9.0)
    p6.add_text("Q2", 390, 312, size=9.0)
    p6.add_text("Q3", 435, 312, size=9.0)
    p6.add_text("Q4", 480, 312, size=9.0)
    pages.append(p6)

    # Page 7: Landscape slide-like PDF with full-slide background wallpaper + crisp digital text
    p7 = _PdfPageSpec(width=960.0, height=540.0)
    p7.add_image("SlideBg", 0, 0, 960, 540, px_w=16, px_h=9, rgb=(245, 247, 250))
    p7.add_text("Quarterly Product Roadmap", 70, 90, font="F2", size=28.0)
    p7.add_text(
        "- Phase 1: Deep PDF routing and deterministic table extraction", 90, 175, size=18.0
    )
    p7.add_text("- Phase 2: Native PPTX slide, chart, and diagram parsing", 90, 225, size=18.0)
    p7.add_text("- Phase 3: Local VLM and OCR adapter integration for scans", 90, 275, size=18.0)
    pages.append(p7)

    # Page 8: Blank page
    p8 = _PdfPageSpec()
    pages.append(p8)

    pdf_bytes = _build_pdf_bytes(
        pages,
        title="ACME Strategy Report",
        author="AutoETL Test",
        outlines=[
            ("1. Executive Overview", 1),
            ("2. Financial Tables", 3),
            ("4. Visual Insights and Architecture", 6),
        ],
    )
    path.write_bytes(pdf_bytes)
    return path


# ---------------------------------------------------------------------------
# 핵심 구조 분류·전략 라우팅·파싱 검증 테스트
# ---------------------------------------------------------------------------


def test_pdf_deep_structure_classification_and_routing(tmp_path: Path) -> None:
    pdf_path = _create_comprehensive_sample_pdf(tmp_path / "sample.pdf")
    crop_dir = tmp_path / "crops"
    doc = parse_document(
        pdf_path,
        options=ParseOptions(extract_crops=True, crop_dir=crop_dir),
        page_renderer=UnavailablePageRenderer(),
    )

    assert isinstance(doc, ParsedDocument)
    assert doc.document.page_count == 8
    assert doc.document.title == "ACME Strategy Report"
    assert doc.document.has_outline is True
    assert len(doc.routing_plan.outline) == 3

    by_page = {p.page: p for p in doc.pages}

    # Page 1: 디지털 본문 + 제목/목록/코드 + 머리글/바닥글 분리
    p1 = by_page[1]
    assert p1.route.category == PageCategory.DIGITAL_TEXT
    assert p1.route.primary_strategy == ParseStrategy.NATIVE_TEXT
    assert any(b.kind == ElementKind.HEADING for b in p1.blocks)
    assert any(b.kind == ElementKind.LIST_ITEM for b in p1.blocks)
    assert any(b.kind == ElementKind.FORMULA_OR_CODE for b in p1.blocks)
    assert len(p1.header_footer_blocks) >= 1
    assert all("ACME Annual Strategy Report" not in b.text for b in p1.blocks)

    # Page 2: 격자형 표 + 병합 셀(colspan=2) + 캡션 연결
    p2 = by_page[2]
    assert p2.route.category == PageCategory.TABLE_HEAVY
    assert p2.route.primary_strategy == ParseStrategy.RULED_TABLE
    assert len(p2.tables) == 1
    t1 = p2.tables[0]
    assert t1.strategy == ParseStrategy.RULED_TABLE
    assert t1.row_count == 4
    assert t1.col_count == 3
    assert t1.has_merged_cells is True
    assert any(c.col_span == 2 and "2025-2026 Sales" in c.text for c in t1.cells)
    assert t1.caption is not None and "Table 1" in t1.caption
    assert any("Korea" in row and "15,800" in row for row in t1.rows)

    # Page 3: 가로선(booktabs) 표 + 무선(borderless) 표 모두 추출
    p3 = by_page[3]
    strategies_p3 = {t.strategy for t in p3.tables}
    assert ParseStrategy.HORIZONTAL_RULED_TABLE in strategies_p3
    assert ParseStrategy.BORDERLESS_TABLE in strategies_p3
    assert len(p3.tables) == 2

    # Page 4: 다단(2단) 레이아웃 — 표로 오인하지 않고 왼쪽 단 전체를 오른쪽 단보다 먼저 읽음
    p4 = by_page[4]
    assert p4.route.category == PageCategory.MULTI_COLUMN
    assert p4.route.primary_strategy == ParseStrategy.MULTI_COLUMN_LAYOUT
    assert p4.route.metrics.column_count == 2
    assert len(p4.tables) == 0
    full_p4_text = "\n".join(b.text for b in p4.blocks)
    assert full_p4_text.index("3. Market Analysis") < full_p4_text.index("LEFT_START")
    assert full_p4_text.index("LEFT_START") < full_p4_text.index("LEFT_END")
    assert full_p4_text.index("LEFT_END") < full_p4_text.index("RIGHT_START")
    assert full_p4_text.index("RIGHT_START") < full_p4_text.index("RIGHT_END")

    # Page 5: 전체 페이지 스캔 — OCR 라우팅 + 내장 RGB 이미지 직접 PNG 크롭 저장
    p5 = by_page[5]
    assert p5.route.category == PageCategory.SCANNED_IMAGE
    assert p5.route.requires_vision is True
    assert p5.route.primary_strategy in (
        ParseStrategy.EMBEDDED_IMAGE_OCR,
        ParseStrategy.VLM_FULL_PAGE,
    )
    assert len(p5.visual_regions) == 1
    scan_reg = p5.visual_regions[0]
    assert scan_reg.element_kind == ElementKind.SCANNED_PAGE
    assert scan_reg.crop_status == CropStatus.SAVED
    assert scan_reg.crop_path is not None
    crop_file = Path(scan_reg.crop_path)
    assert crop_file.exists()
    assert crop_file.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    # Page 6: 혼재 페이지 — 내장 그림(캡션 연결) + 벡터 차트(축 레이블 힌트)
    p6 = by_page[6]
    assert p6.route.category in (
        PageCategory.HYBRID_TEXT_IMAGE,
        PageCategory.VECTOR_DIAGRAM_HEAVY,
    )
    kinds_p6 = {v.element_kind for v in p6.visual_regions}
    assert ElementKind.FIGURE_IMAGE in kinds_p6
    assert ElementKind.VECTOR_DIAGRAM in kinds_p6
    fig_region = next(v for v in p6.visual_regions if v.element_kind == ElementKind.FIGURE_IMAGE)
    assert fig_region.caption is not None and "Figure 1" in fig_region.caption
    assert fig_region.crop_status == CropStatus.SAVED
    vec_region = next(v for v in p6.visual_regions if v.element_kind == ElementKind.VECTOR_DIAGRAM)
    assert vec_region.recommended_strategy == ParseStrategy.VLM_REGION
    assert vec_region.crop_status == CropStatus.RENDERER_UNAVAILABLE
    assert any(lbl in vec_region.embedded_text_hints for lbl in ("Q1", "Q2", "Q3", "Q4"))

    # Page 7: 가로형 슬라이드 + 배경 벽지 위 텍스트 (스캔 오인 금지!)
    p7 = by_page[7]
    assert p7.route.category == PageCategory.SLIDE_LIKE
    assert p7.route.requires_vision is False
    assert any("Quarterly Product Roadmap" in b.text for b in p7.blocks)

    # Page 8: 빈 페이지
    p8 = by_page[8]
    assert p8.route.category == PageCategory.BLANK
    assert p8.route.primary_strategy == ParseStrategy.SKIP_DECORATIVE


def test_form_xobject_nested_coordinates_and_rotation(tmp_path: Path) -> None:
    """Form XObject 중첩 CTM 보정과 90도 회전 페이지 좌표 정규화를 검증한다."""
    pg = _PdfPageSpec(width=600.0, height=400.0)
    inner = "BT /F2 14 Tf 1 0 0 1 10 20 Tm (InsideFormTitle) Tj ET"
    pg.add_form("BoxForm", dx=100.0, dy=250.0, inner_ops=inner)
    pg.add_text("OutsideBodyLine", 110.0, 220.0, size=11.0)

    rot_pg = _PdfPageSpec(width=595.0, height=842.0, rotation=90)
    rot_pg.add_text("Rotated Landscape Heading", 80.0, 100.0, font="F2", size=18.0)

    pdf_path = tmp_path / "form_and_rot.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg, rot_pg]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    p1_blocks = doc.pages[0].blocks
    form_blk = next(b for b in p1_blocks if "InsideFormTitle" in b.text)
    out_blk = next(b for b in p1_blocks if "OutsideBodyLine" in b.text)
    # Form이 (dx=100, dy=250) + (10, 20) -> PDF y=270 -> top = 130 (OutsideBody top=220보다 위)
    assert form_blk.bbox.x0 == pytest.approx(110.0, abs=3.0)
    assert form_blk.bbox.top < out_blk.bbox.top

    # 90도 회전 페이지: width/height가 스왑되어 가로형(842x595)이 되어야 함
    m2 = doc.pages[1].route.metrics
    assert m2.rotation == 90
    assert m2.width_pt == pytest.approx(842.0, abs=1.0)
    assert m2.height_pt == pytest.approx(595.0, abs=1.0)
    assert m2.aspect_ratio > 1.3


def test_broken_hidden_ocr_layer_routes_to_scanned_vision(tmp_path: Path) -> None:
    """스캔 이미지 위에 깨진/제어문자 텍스트 레이어가 덮인 PDF도 스캔으로 감지한다."""
    pg = _PdfPageSpec()
    pg.add_image("ScanImg", 10, 10, 575, 822, px_w=20, px_h=20)
    # 제어문자 다수 포함 가비지 텍스트 레이어(ASCII 0x01~0x07)
    garbage = "".join(f"\\00{i}" for i in range(1, 8)) * 5
    pg.ops.append(f"BT /F1 10 Tf 1 0 0 1 50 700 Tm ({garbage}) Tj ET")

    pdf_path = tmp_path / "broken_ocr.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg]))

    plan = inspect_document(pdf_path)
    dec = plan.pages[0]
    assert dec.category == PageCategory.SCANNED_IMAGE
    assert dec.requires_vision is True
    assert dec.metrics.garbage_char_ratio >= 0.35
    assert any("깨짐" in w for w in dec.warnings)


def test_vision_adapter_plugin_splices_markdown_into_ir(tmp_path: Path) -> None:
    """VisionParserAdapter를 연결하면 라우팅된 시각 영역만 호출해 Markdown에 반영한다."""
    pdf_path = _create_comprehensive_sample_pdf(tmp_path / "vision_test.pdf")
    seen_requests: list[VisionRequest] = []

    class FakeVlmAdapter:
        @property
        def name(self) -> str:
            return "fake-vlm-v1"

        def parse_region(self, request: VisionRequest) -> VisionResult:
            seen_requests.append(request)
            return VisionResult(
                adapter_name=self.name,
                succeeded=True,
                text=f"VLM parsed {request.region_id}",
                markdown=f"**[VLM 결과: {request.region_id}]** 추출 성공",
                metadata={"model": "fake-vlm"},
            )

    doc = parse_document(
        pdf_path,
        options=ParseOptions(pages=(5, 6), extract_crops=True),
        vision_adapter=FakeVlmAdapter(),
        page_renderer=UnavailablePageRenderer(),
    )

    assert len(seen_requests) == 3  # p5 스캔 1개 + p6 그림 1개 + p6 벡터 차트 1개
    # 스캔 페이지와 그림에는 순수 Python으로 추출한 이미지 바이트가 전달되어야 함
    scan_req = next(r for r in seen_requests if r.element_kind == ElementKind.SCANNED_PAGE)
    assert scan_req.image_bytes is not None
    assert scan_req.image_bytes.startswith(b"\x89PNG\r\n\x1a\n")

    assert all(v.vision_status == VisionStatus.COMPLETED for v in doc.visual_regions)
    assert all(v.vision_adapter == "fake-vlm-v1" for v in doc.visual_regions)
    md = render_document_markdown(doc)
    assert "**[VLM 결과: p005_v01]** 추출 성공" in md


def test_parse_and_export_bundle_files(tmp_path: Path) -> None:
    """번들 내보내기가 IR JSON, Markdown, 전략 리포트, 표 CSV/JSON, 크롭을 모두 생성한다."""
    pdf_path = _create_comprehensive_sample_pdf(tmp_path / "bundle.pdf")
    out_dir = tmp_path / "out_bundle"

    doc, manifest = parse_and_export(
        pdf_path,
        out_dir,
        page_renderer=UnavailablePageRenderer(),
    )

    assert manifest.parsed_json.exists()
    assert manifest.document_markdown.exists()
    assert manifest.routing_report_json.exists()
    assert manifest.routing_report_markdown.exists()
    assert len(manifest.table_csv_paths) == 3
    assert len(manifest.table_json_paths) == 3
    assert len(manifest.crop_paths) >= 2

    # JSON 계약 왕복 검증
    loaded_doc = ParsedDocument.model_validate_json(
        manifest.parsed_json.read_text(encoding="utf-8")
    )
    assert loaded_doc.document.page_count == doc.document.page_count
    loaded_plan = RoutingPlan.model_validate_json(
        manifest.routing_report_json.read_text(encoding="utf-8")
    )
    assert loaded_plan.summary.table_count == 3

    # 추출된 CSV 파일 검증
    first_csv = manifest.table_csv_paths[0]
    with first_csv.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) >= 3
    assert any("Korea" in row for row in rows)

    # 추출된 표 JSON 검증
    first_tbl_json = json.loads(manifest.table_json_paths[0].read_text(encoding="utf-8"))
    assert first_tbl_json["strategy"] == "ruled_table"

    # Markdown과 전략 리포트 내용 확인
    doc_md = manifest.document_markdown.read_text(encoding="utf-8")
    assert "## Page 2 (" in doc_md
    assert "| Region |" in doc_md
    report_md = manifest.routing_report_markdown.read_text(encoding="utf-8")
    assert "다단 레이아웃" in report_md
    assert "스캔/이미지" in report_md
    assert "격자선 표 복원" in report_md


def test_parse_page_selection_validation() -> None:
    assert parse_page_selection("1,3-5,2") == (1, 3, 4, 5, 2)
    assert parse_page_selection("2-2") == (2,)
    with pytest.raises(ParserInputError):
        parse_page_selection("")
    with pytest.raises(ParserInputError):
        parse_page_selection("5-2")
    with pytest.raises(ParserInputError):
        parse_page_selection("0")
    with pytest.raises(ParserInputError):
        parse_page_selection("abc")


def test_cli_parse_modes_and_error_handling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf_path = _create_comprehensive_sample_pdf(tmp_path / "cli_sample.pdf")

    # 1. --plan-only stdout report
    code = cli_main(["parse", str(pdf_path), "--plan-only", "--pages", "1-2,5"])
    assert code == 0
    out = capsys.readouterr().out
    assert "문서 구조 진단 및 파싱 전략 리포트" in out
    assert "3 / 3" not in out  # total 8 / analyzed 3
    assert "8 / 3" in out

    # 2. --plan-only --format json
    code = cli_main(["parse", str(pdf_path), "--plan-only", "--format", "json", "--pages", "1"])
    assert code == 0
    plan = RoutingPlan.model_validate_json(capsys.readouterr().out)
    assert plan.summary.analyzed_pages == 1

    # 3. --out 번들 저장
    out_dir = tmp_path / "cli_out"
    code = cli_main(["parse", str(pdf_path), "--out", str(out_dir)])
    assert code == 0
    assert (out_dir / "parsed.json").exists()
    assert (out_dir / "document.md").exists()
    assert (out_dir / "routing_report.md").exists()
    assert (out_dir / "tables").is_dir()

    # 4. --format markdown stdout
    code = cli_main(["parse", str(pdf_path), "--format", "markdown", "--pages", "1", "--no-crops"])
    assert code == 0
    md_out = capsys.readouterr().out
    assert "Executive Overview" in md_out

    # 5. 오류 경로: 없는 파일(2), 원본 덮어쓰기(2), 미지원 형식(1), 손상 PDF(1), 범위 초과 페이지(1)
    assert cli_main(["parse", str(tmp_path / "missing.pdf")]) == 2
    assert cli_main(["parse", str(pdf_path), "--out", str(pdf_path)]) == 2

    unsupported = tmp_path / "notes.xyz"
    unsupported.write_text("hello", encoding="utf-8")
    assert cli_main(["parse", str(unsupported)]) == 1

    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"NOT A REAL PDF FILE")
    assert cli_main(["parse", str(corrupt)]) == 1

    assert cli_main(["parse", str(pdf_path), "--pages", "99"]) == 1

    # 전략 리포트 렌더링 직접 호출도 결정론 확인
    plan_a = inspect_document(pdf_path)
    plan_b = inspect_document(pdf_path)
    assert render_routing_report_markdown(plan_a) == render_routing_report_markdown(plan_b)


def test_non_unit_matrix_scaling_and_grouped_subrow_table(tmp_path: Path) -> None:
    """실제 HWP(cm=0.12)·Word(tm=14) 비단위 스케일링과 묶음 행 격자표 자동 분할을 검증한다."""
    # Page 1: HWP 스타일 cm=0.12 + fs=83 (유효 ~10pt) 2단 레이아웃
    p1 = _PdfPageSpec(width=595.0, height=841.0)
    p1.ops.append("q 0.12 0 0 0.12 0 0 cm")
    for idx in range(5):
        y = 6200 - idx * 160
        p1.ops.append(f"BT /F1 83 Tf 1 0 0 1 600 {y} Tm (LeftColLine{idx} sample text ) Tj ET")
        p1.ops.append(f"BT /F1 83 Tf 1 0 0 1 2600 {y} Tm (RightColLine{idx} sample text ) Tj ET")
    p1.ops.append("Q")

    # Page 2: 3개 데이터 행이 하나의 가로선 밴드 안에 묶인 격자표(FBI NICS 패턴)
    p2 = _PdfPageSpec(width=595.0, height=842.0)
    for y in (100, 130, 210):
        p2.add_line(60, y, 500, y)
    for x in (60, 220, 360, 500):
        p2.add_line(x, 100, x, 210)
    p2.add_text("State", 75, 118, font="F2", size=10.0)
    p2.add_text("Permit", 235, 118, font="F2", size=10.0)
    p2.add_text("Total", 375, 118, font="F2", size=10.0)
    for idx, (st, pm, tot) in enumerate(
        [
            ("Alabama", "18,870", "71,137"),
            ("Alaska", "209", "7,095"),
            ("Arizona", "2,303", "27,087"),
        ]
    ):
        top_y = 148 + idx * 20
        p2.add_text(st, 75, top_y, size=10.0)
        p2.add_text(pm, 235, top_y, size=10.0)
        p2.add_text(tot, 375, top_y, size=10.0)

    pdf_path = tmp_path / "real_world_quirks.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([p1, p2]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    # HWP 스케일 2단 레이아웃 정확 감지 및 읽기 순서 보존
    assert doc.pages[0].route.category == PageCategory.MULTI_COLUMN
    assert doc.pages[0].route.metrics.column_count == 2
    p1_text = "\n".join(b.text for b in doc.pages[0].blocks)
    assert p1_text.index("LeftColLine4") < p1_text.index("RightColLine0")

    # 묶음 행 격자표가 1행이 아니라 3개 데이터 행(총 4행: 헤더1 + 데이터3)으로 자동 분할
    assert len(doc.pages[1].tables) == 1
    tbl = doc.pages[1].tables[0]
    assert tbl.row_count == 4
    assert [row[0] for row in tbl.rows] == ["Alabama", "Alaska", "Arizona"]
    assert [row[2] for row in tbl.rows] == ["71,137", "7,095", "27,087"]


def test_tj_tc_multicolumn_and_booktabs_superheader(tmp_path: Path) -> None:
    """TJ 대간격, 고-Tc+상쇄 TJ, booktabs 병합 슈퍼 헤더 하위 열 복원을 검증한다."""
    # Page 1: LaTeX booktabs 표 (슈퍼 헤더 'BLEU' 아래 EN-DE / EN-FR, TJ 대간격 + 앞공백 유도 Td)
    p1 = _PdfPageSpec(width=612.0, height=792.0)
    p1.add_line(100, 100, 480, 100)
    p1.add_line(100, 134, 480, 134)
    p1.add_line(100, 232, 480, 232)
    # 슈퍼 헤더 (EN-DE와 EN-FR 사이의 여백을 가로지름)
    p1.add_text("BLEU", 300, 114, font="F2", size=10.0)
    # 2행 헤더: Model, EN-DE, EN-FR (TJ 대간격으로 한 배열에 기록)
    p1.ops.append("BT /F2 10 Tf 1 0 0 1 110 664 Tm [(Model) -14000 (EN-DE) -1800 (EN-FR)] TJ ET")
    rows_data = [
        ("GNMT [38]", "24.6", "39.92"),
        ("ConvS2S [9]", "25.16", "40.46"),
        ("MoE [32]", "26.03", "40.56"),
        ("Base Model", "27.3", "38.1"),
    ]
    for idx, (model, ende, enfr) in enumerate(rows_data):
        pdf_y = 644 - idx * 18
        p1.ops.append(
            f"BT /F1 10 Tf 1 0 0 1 110 {pdf_y} Tm "
            f"[({_pdf_escape(model)}) -11500 ({ende}) -2200 ({enfr})] TJ ET"
        )
    # 마지막 행은 Tf 전환 + Td 이동 뒤 TJ(pypdf가 앞 공백을 붙이는 실전 패턴)
    p1.ops.append(
        "BT /F1 10 Tf 1 0 0 1 110 572 Tm (Transformer big) Tj "
        "/F2 10 Tf 170 0 Td [(28.4) -2200 (41.8)] TJ ET"
    )

    # Page 2: FBI NICS 스타일 고-Tc(4.11) + 상쇄 +4125 TJ 격자표 ('0', '2,248', '1,135' 분할)
    p2 = _PdfPageSpec(width=612.0, height=792.0)
    for y in (100, 126, 154, 182):
        p2.add_line(60, y, 320, y)
    for x in (60, 160, 195, 235, 320):
        p2.add_line(x, 100, x, 182)
    p2.add_text("State", 70, 116, font="F2", size=8.0)
    p2.add_text("ColA", 168, 116, font="F2", size=8.0)
    p2.add_text("ColB", 202, 116, font="F2", size=8.0)
    p2.add_text("ColC", 250, 116, font="F2", size=8.0)

    # Row 1 (PDF y=648): Tc=4.1107, scale=6 -> '0'(175) | '2,248'(~203) | '1,135'(~243)
    p2.add_text("Florida", 70, 144, size=8.0)
    p2.ops.append(
        "BT 4.1107 Tc 6 0 0 6 175 648 Tm /F1 1 Tf "
        "[(02) 4125 (,) 4098 (2) 4125 (4) 4125 (81) 4125 (,) 4098 (1) 4125 (3) 4125 (5)] TJ "
        "0 Tc ET"
    )
    p2.add_text("Georgia", 70, 172, size=8.0)
    p2.ops.append("BT 5.0 Tc 6 0 0 6 175 620 Tm /F1 1 Tf (30) Tj 0 Tc ET")
    p2.add_text("999", 250, 172, size=8.0)

    pdf_path = tmp_path / "tj_tc_tables.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([p1, p2]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))

    # Page 1: booktabs 3열 복원 및 Transformer big 행 분리 검증
    assert len(doc.pages[0].tables) == 1
    t1 = doc.pages[0].tables[0]
    assert t1.col_count == 3
    all_rows_t1 = [t1.headers, *t1.rows]
    assert any(r[0] == "GNMT [38]" and r[1] == "24.6" and r[2] == "39.92" for r in all_rows_t1)
    assert any("Transformer big" in r[0] and r[1] == "28.4" and r[2] == "41.8" for r in all_rows_t1)

    # Page 2: 고-Tc + 상쇄 TJ가 정확히 3개 별도 셀로 분할되었는지 검증
    assert len(doc.pages[1].tables) == 1
    t2 = doc.pages[1].tables[0]
    assert t2.col_count == 4
    florida = next(r for r in t2.rows if r[0] == "Florida")
    assert florida == ["Florida", "0", "2,248", "1,135"]
    georgia = next(r for r in t2.rows if r[0] == "Georgia")
    assert georgia[:3] == ["Georgia", "3", "0"]


def test_open_sided_ruled_table_cross_page_and_td_lead_jump(tmp_path: Path) -> None:
    """좌측 외곽선 없는 열린 표 복원, 페이지 간 표 연속 연결, 선행 공백 Td 점프를 검증한다."""
    # Page 1: 상단 열린 표(좌측 세로선 생략) + 하단 이어질 표(norm_bottom >= 0.70)
    p1 = _PdfPageSpec(width=595.0, height=842.0)
    # 상단 열린 표: 가로선은 x=60..500, 세로선은 x=190, 345, 500만 존재(좌측 60..190 열 열림)
    for y in (90, 120, 150, 180):
        p1.add_line(60, y, 500, y)
    for x in (190, 345, 500):
        p1.add_line(x, 90, x, 180)
    p1.add_text("Rate", 220, 108, font="F2", size=10.0)
    p1.add_text("CPI", 380, 108, font="F2", size=10.0)
    p1.add_text("1Y Change", 75, 138, size=10.0)
    p1.add_text("-100bp", 220, 138, size=10.0)
    p1.add_text("-0.5%p", 380, 138, size=10.0)
    p1.add_text("Impact", 75, 168, size=10.0)
    p1.add_text("-0.064", 220, 168, size=10.0)
    p1.add_text("-0.005", 380, 168, size=10.0)

    # 하단 이어질 표 (y=600..780 -> norm_bottom = 0.926 >= 0.70)
    for y in (600, 645, 690, 735, 780):
        p1.add_line(60, y, 520, y)
    for x in (60, 140, 310, 520):
        p1.add_line(x, 600, x, 780)
    p1.add_text("Session", 72, 625, font="F2", size=10.0)
    p1.add_text("Topic", 155, 625, font="F2", size=10.0)
    p1.add_text("Details", 325, 625, font="F2", size=10.0)
    for idx, (s, tp, dt) in enumerate(
        [
            ("S1", "Intro", "Overview"),
            ("S2", "Prompting", "Design"),
            ("S3", "Writing", "Drafting"),
        ]
    ):
        ty = 670 + idx * 45
        p1.add_text(s, 72, ty, size=10.0)
        p1.add_text(tp, 155, ty, size=10.0)
        p1.add_text(dt, 325, ty, size=10.0)

    # Page 2: 상단 이어진 표(y=60..195 -> norm_top = 0.071 <= 0.20, 헤더 미반복) + 공백-Td-AI 점프
    p2 = _PdfPageSpec(width=595.0, height=842.0)
    for y in (60, 105, 150, 195):
        p2.add_line(60, y, 520, y)
    for x in (60, 140, 310, 520):
        p2.add_line(x, 60, x, 195)
    p2.add_text("S4", 72, 85, size=10.0)
    p2.add_text("Analysis", 155, 85, size=10.0)
    p2.add_text("Charts", 325, 85, size=10.0)

    # Row S5: 2열 끝(x=285)에 공백 1개만 그린 뒤 3열(x=325)로 Td 점프 후 'AI' 출력 (Word 패턴)
    p2.add_text("S5", 72, 130, size=10.0)
    p2.add_text("Management", 155, 130, size=10.0)
    pdf_y_s5 = 842.0 - 130.0
    p2.ops.append(
        f"BT /F1 10 Tf 1 0 0 1 285 {pdf_y_s5:.1f} Tm ( ) Tj 40 0 Td (AI) Tj 18 0 Td (tools) Tj ET"
    )
    p2.add_text("S6", 72, 175, size=10.0)
    p2.add_text("Strategy", 155, 175, size=10.0)
    p2.add_text("Optimization", 325, 175, size=10.0)

    pdf_path = tmp_path / "open_and_cross_page.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([p1, p2]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    # Page 1 열린 표: 좌측 날개 열 복원으로 3열, 행 헤더('1Y Change', 'Impact') 포함
    assert len(doc.pages[0].tables) == 2
    open_tbl = doc.pages[0].tables[0]
    assert open_tbl.col_count == 3
    assert [r[0] for r in open_tbl.rows] == ["1Y Change", "Impact"]

    # Page 2 이어진 표: 헤더 상속, S4 데이터 행 복원, S5 'AI' 3열 유지
    assert len(doc.pages[1].tables) == 1
    cont_tbl = doc.pages[1].tables[0]
    assert cont_tbl.continued_from_table_id == doc.pages[0].tables[1].table_id
    assert cont_tbl.headers == ["Session", "Topic", "Details"]
    assert [r[0] for r in cont_tbl.rows] == ["S4", "S5", "S6"]
    s5_row = cont_tbl.rows[1]
    assert "AI" not in s5_row[1]
    assert "AI" in s5_row[2]

    md = render_document_markdown(doc)
    assert "에서 이어짐" in md


def test_cover_page_inline_superscript_and_two_line_caption(tmp_path: Path) -> None:
    """표지 소형 다단 저자 블록 오분류 방지, 인라인 위첨자 병합, 2줄 캡션 연결을 검증한다."""
    # Page 1: 표지 (제목 + 작은 2열 저자 블록 + 긴 전폭 초록 -> DIGITAL_TEXT 유지)
    p1 = _PdfPageSpec(width=595.0, height=842.0)
    p1.add_text("Attention Architecture Paper Title", 120, 90, font="F2", size=18.0)
    # 작은 2열 저자 블록 (전체 문자의 <25%)
    for i, (left_a, right_a) in enumerate(
        [
            ("Alice Kim", "Bob Lee"),
            ("Lab A", "Lab B"),
            ("Seoul", "Daejeon"),
            ("a@ex.org", "b@ex.org"),
        ]
    ):
        p1.add_text(left_a, 130, 140 + i * 16, size=9.5)
        p1.add_text(right_a, 340, 140 + i * 16, size=9.5)
    p1.add_text("Abstract", 260, 235, font="F2", size=12.0)
    for i in range(10):
        p1.add_text(
            "This full-width abstract paragraph describes the complete experimental setup and "
            "evaluation methodology across multiple benchmarks in detail for every reader.",
            65,
            260 + i * 18,
            size=10.0,
        )

    # Page 2: 인라인 위첨자 '1)'이 포함된 문단 + 공백 괄호 2줄 그림 캡션 '< Figure 1>' + 이미지
    p2 = _PdfPageSpec(width=595.0, height=842.0)
    p2.add_text("The central bank published estimates in 2021.", 70, 100, size=10.0)
    # 같은 줄(top=118)에 'First part.' + 위첨자 '1)'(y 살짝 위, sz=7.0) + 'Second' 배치
    p2.add_text("First part.", 70, 118, size=10.0)
    p2.add_text("1)", 120, 115.5, size=7.0)
    p2.add_text("Second clause continues cleanly.", 132, 118, size=10.0)
    p2.add_text("Final sentence finishes the paragraph smoothly.", 70, 136, size=10.0)

    # 2줄로 줄바꿈된 공백 포함 그림 캡션 '< Figure 1>' + 바로 아래 내장 이미지
    p2.add_text("< Figure 1> Quarterly Employment Rate Trend", 120, 220, size=9.5)
    p2.add_text("Before and After 2021", 160, 234, size=9.5)
    p2.add_image("ChartImg", 110, 252, 340, 180, px_w=24, px_h=16)

    pdf_path = tmp_path / "cover_sup_caption.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([p1, p2]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    # Page 1: 표지의 작은 저자 그리드 때문에 multi_column으로 오분류되지 않음
    assert doc.pages[0].route.category == PageCategory.DIGITAL_TEXT

    # Page 2: 위첨자 '1)'이 문단을 끊지 않고 원래 위치('First part.1) Second...')에 인라인 병합
    p2_paras = [b.text for b in doc.pages[1].blocks if b.kind == ElementKind.PARAGRAPH]
    merged_para = next((t for t in p2_paras if "First part." in t), "")
    assert "First part.1) Second clause" in merged_para
    assert "Final sentence" in merged_para

    # Page 2: 2줄 '< Figure 1> ...' 캡션이 병합되어 시각 영역에 자동 연결됨
    assert len(doc.pages[1].visual_regions) == 1
    cap = doc.pages[1].visual_regions[0].caption
    assert cap is not None
    assert "Figure 1" in cap
    assert "Before and After 2021" in cap


def test_cjk_two_column_prose_not_misclassified_as_borderless_table() -> None:
    """한글 2단 본문(~20자/열)이 무선 표(borderless_table)로 오인되지 않음을 검증한다."""
    from autoetl.parser.pdf.geometry import RawPageData, RawTextSpan
    from autoetl.parser.pdf.tables import extract_page_tables

    left_lines = [
        "한국은행은 팬데믹 충격의 영향을 고려하여",
        "잠재성장률 평균을 내외로 발표하였다.",
        "그러나 방역조치 해제 등으로 회복시점에",
        "대한 불확실성이 제거된 데다 추가적인",
        "데이터가 축적되면서 우리 경제의 성장에",
        "미친 영향에 대한 재검토가 필요한 상황이다.",
    ]
    right_lines = [
        "아울러 잠재성장률 추정에 필요한 국민계정",
        "자료가 기준년 개편으로 새롭게 편제된 가운데",
        "인구추계 역시 수정되면서 그간의 경제구조",
        "변화를 반영하여 재추정해 볼 필요가 있다.",
        "먼저 성장률의 경우 기준년 개편으로 구계열",
        "대비 신계열에서 연평균 상향조정되었다.",
    ]
    spans: list[RawTextSpan] = []
    for idx, (lt, rt) in enumerate(zip(left_lines, right_lines, strict=True)):
        top = 120.0 + idx * 18.0
        spans.append(
            RawTextSpan(
                text=lt,
                x0=70.0,
                top=top,
                x1=285.0,
                bottom=top + 10.0,
                font_name="MalgunGothic",
                font_size=10.0,
                is_bold=False,
                is_italic=False,
                orientation=0,
            )
        )
        spans.append(
            RawTextSpan(
                text=rt,
                x0=310.0,
                top=top,
                x1=525.0,
                bottom=top + 10.0,
                font_name="MalgunGothic",
                font_size=10.0,
                is_bold=False,
                is_italic=False,
                orientation=0,
            )
        )

    raw_page = RawPageData(
        page=1,
        width=595.0,
        height=842.0,
        rotation=0,
        spans=spans,
        lines=[],
        rects=[],
        curves=[],
        images=[],
    )
    res = extract_page_tables(raw_page, detect_borderless=True)
    assert res.tables == []


def test_pdf_colored_header_with_zebra_fill_does_not_drop_first_data_row() -> None:
    """배경색 헤더 + 얼룩말(zebra) 음영 PDF 표에서 첫 데이터 행이 헤더로 흡수되지 않는다."""
    from autoetl.parser.pdf.geometry import RawPageData, RawTextSpan, RawVectorLine, RawVectorRect
    from autoetl.parser.pdf.tables import extract_page_tables

    # 3열 x 4행 (행 0: 진한 네이비 헤더, 행 1: 연회색 zebra, 행 2: 흰색, 행 3: 연회색)
    xs = [60.0, 200.0, 340.0, 480.0]
    ys = [100.0, 124.0, 148.0, 172.0, 196.0]

    lines: list[RawVectorLine] = []
    for y in ys:
        lines.append(RawVectorLine(x0=xs[0], top=y, x1=xs[-1], bottom=y, stroke_width=0.6))
    for x in xs:
        lines.append(RawVectorLine(x0=x, top=ys[0], x1=x, bottom=ys[-1], stroke_width=0.6))

    rects = [
        # 전체 표 배경(흰색) — 헤더 음영으로 오인 금지
        RawVectorRect(
            x0=xs[0],
            top=ys[0],
            x1=xs[-1],
            bottom=ys[-1],
            is_filled=True,
            is_stroked=False,
            stroke_width=0.0,
            fill_color=(1.0, 1.0, 1.0),
        ),
        # 행 0: 네이비 헤더 음영
        RawVectorRect(
            x0=xs[0],
            top=ys[0],
            x1=xs[-1],
            bottom=ys[1],
            is_filled=True,
            is_stroked=False,
            stroke_width=0.0,
            fill_color=(0.12, 0.25, 0.45),
        ),
        # 행 1: 연회색 zebra 데이터 행 음영 (헤더와 색상이 다름)
        RawVectorRect(
            x0=xs[0],
            top=ys[1],
            x1=xs[-1],
            bottom=ys[2],
            is_filled=True,
            is_stroked=False,
            stroke_width=0.0,
            fill_color=(0.92, 0.94, 0.96),
        ),
        # 행 2: 흰색 데이터 행
        RawVectorRect(
            x0=xs[0],
            top=ys[2],
            x1=xs[-1],
            bottom=ys[3],
            is_filled=True,
            is_stroked=False,
            stroke_width=0.0,
            fill_color=(1.0, 1.0, 1.0),
        ),
    ]

    grid_text = [
        ("지점명", "매출액(억원)", "달성률"),
        ("강남본점", "125.4", "112%"),
        ("부산센텀", "88.2", "98%"),
        ("대구동성로", "64.0", "105%"),
    ]
    spans: list[RawTextSpan] = []
    for r_idx, row in enumerate(grid_text):
        top = ys[r_idx] + 6.0
        for c_idx, cell in enumerate(row):
            spans.append(
                RawTextSpan(
                    text=cell,
                    x0=xs[c_idx] + 8.0,
                    top=top,
                    x1=xs[c_idx + 1] - 8.0,
                    bottom=top + 11.0,
                    font_name="MalgunGothic-Bold" if r_idx == 0 else "MalgunGothic",
                    font_size=10.0,
                    is_bold=(r_idx == 0),
                    is_italic=False,
                    orientation=0,
                )
            )

    raw_page = RawPageData(
        page=1,
        width=595.0,
        height=842.0,
        rotation=0,
        spans=spans,
        lines=lines,
        rects=rects,
        curves=[],
        images=[],
    )
    res = extract_page_tables(raw_page)
    assert len(res.tables) == 1
    tbl = res.tables[0]
    assert tbl.headers == ["지점명", "매출액(억원)", "달성률"]
    assert tbl.row_count == 4
    assert len(tbl.rows) == 3
    assert tbl.rows[0] == ["강남본점", "125.4", "112%"]
    assert tbl.rows[1] == ["부산센텀", "88.2", "98%"]
    assert tbl.rows[2] == ["대구동성로", "64.0", "105%"]


def test_pdf_dotted_borders_reconstructed_and_multiline_cells_preserved(tmp_path: Path) -> None:
    """한컴 PDF처럼 0.36pt 점선 스트로크로 그린 표 내부 가로선을 복원해 2줄 셀 분할을 막는다."""
    pg = _PdfPageSpec()
    xs = [70.0, 200.0, 280.0, 360.0, 520.0]
    ys = [120.0, 148.0, 188.0, 218.0, 258.0]  # 헤더 + 3개 데이터 행

    # 외곽·헤더 아래·최하단 실선 + 세로 실선
    for y in (ys[0], ys[1], ys[-1]):
        pg.add_line(xs[0], y, xs[-1], y, width=0.6)
    for x in xs:
        pg.add_line(x, ys[0], x, ys[-1], width=0.6)

    # 내부 행 구분선(ys[2], ys[3])을 0.36pt 길이, 0.84pt 간격의 점선 스트로크로 그림
    for y in (ys[2], ys[3]):
        pdf_y = pg.height - y
        cur_x = xs[0]
        while cur_x < xs[-1]:
            pg.ops.append(f"0.36 w {cur_x:.2f} {pdf_y:.2f} m {cur_x + 0.36:.2f} {pdf_y:.2f} l S")
            cur_x += 0.84

    # 헤더
    pg.add_text("Type", 80.0, 138.0, font="F2", size=10.0)
    pg.add_text("Count", 215.0, 138.0, font="F2", size=10.0)
    pg.add_text("Amount", 295.0, 138.0, font="F2", size=10.0)
    pg.add_text("Notes", 375.0, 138.0, font="F2", size=10.0)

    # 행 1: 비고 열만 2줄, 나머지 열은 세로 중앙 정렬
    pg.add_text("DirectDeal", 80.0, 172.0, size=10.0)
    pg.add_text("647", 225.0, 172.0, size=10.0)
    pg.add_text("213.2", 300.0, 172.0, size=10.0)
    pg.add_text("Line one note,", 370.0, 164.0, size=10.0)
    pg.add_text("line two detail", 370.0, 178.0, size=10.0)

    # 행 2: 1줄 행
    pg.add_text("Family", 80.0, 206.0, size=10.0)
    pg.add_text("122", 225.0, 206.0, size=10.0)
    pg.add_text("13.3", 300.0, 206.0, size=10.0)
    pg.add_text("Single line note", 370.0, 206.0, size=10.0)

    # 행 3: 비고 열 2줄
    pg.add_text("Misuse", 80.0, 242.0, size=10.0)
    pg.add_text("83", 225.0, 242.0, size=10.0)
    pg.add_text("23.1", 300.0, 242.0, size=10.0)
    pg.add_text("Restricted merchant,", 370.0, 234.0, size=10.0)
    pg.add_text("excessive rent", 370.0, 248.0, size=10.0)

    pdf_path = tmp_path / "dotted_table.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg]))

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    assert len(doc.tables) == 1
    tbl = doc.tables[0]
    assert tbl.row_count == 4
    assert tbl.col_count == 4
    assert tbl.headers == ["Type", "Count", "Amount", "Notes"]
    assert tbl.rows == [
        ["DirectDeal", "647", "213.2", "Line one note, line two detail"],
        ["Family", "122", "13.3", "Single line note"],
        ["Misuse", "83", "23.1", "Restricted merchant, excessive rent"],
    ]


def test_pdf_filled_polygon_rects_colspan_subrow_split_and_wrap_guard(tmp_path: Path) -> None:
    """`m l l l h f*` 채움 다각형, `col_span` 묶음 행 분할, 무선 2줄 셀 보호를 함께 검증한다."""
    from autoetl.parser.pdf.extractor import extract_pdf_document
    from autoetl.parser.pdf.geometry import RawPageData, RawTextSpan, RawVectorLine
    from autoetl.parser.pdf.tables import extract_page_tables

    # 1) `m l l l l h f*` 셀 채움 다각형이 RawVectorRect로 복원되고 색상 경계로 소계 행이 분리됨
    pg = _PdfPageSpec()
    xs = [60.0, 120.0, 200.0, 300.0, 400.0]
    # 행1(125..175): col0+1 병합 2개 소행, 행2(175..225): 배경색 경계
    ys = [100.0, 125.0, 175.0, 225.0]

    for y in ys:
        pg.add_line(xs[0], y, xs[-1], y, width=0.6)
    for x in xs:
        if x == xs[1]:
            # col0+1 병합 영역(ys[0]..ys[2])에는 세로선 없음, 아래쪽(ys[2]..ys[3])만 세로선
            pg.add_line(x, ys[2], x, ys[3], width=0.6)
        else:
            pg.add_line(x, ys[0], x, ys[-1], width=0.6)

    # 셀별 `m l l l l h f*` 헤더 채움(하늘색) + 하단 반쪽(200..225) 연녹색 채움
    def add_poly_fill(x0: float, top: float, x1: float, bot: float, rgb: str) -> None:
        y_top = pg.height - top
        y_bot = pg.height - bot
        pg.ops.append(
            f"q {rgb} rg {x0:.2f} {y_top:.2f} m {x0:.2f} {y_bot:.2f} l "
            f"{x1:.2f} {y_bot:.2f} l {x1:.2f} {y_top:.2f} l {x0:.2f} {y_top:.2f} l h f* Q"
        )

    for c0, c1 in [(xs[0], xs[2]), (xs[2], xs[3]), (xs[3], xs[4])]:
        add_poly_fill(c0, ys[0], c1, ys[1], "0.87 0.90 0.97")
    for c0, c1 in [(xs[0], xs[1]), (xs[1], xs[2]), (xs[2], xs[3]), (xs[3], xs[4])]:
        add_poly_fill(c0, 200.0, c1, ys[3], "0.95 1.00 0.86")

    pg.add_text("Item", 80.0, 116.0, font="F2", size=10.0)
    pg.add_text("2024", 230.0, 116.0, font="F2", size=10.0)
    pg.add_text("2025", 330.0, 116.0, font="F2", size=10.0)

    # col_span=2 묶음 행(선 없이 2줄 정렬)
    pg.add_text("FraudCount", 70.0, 142.0, size=10.0)
    pg.add_text("630", 230.0, 142.0, size=10.0)
    pg.add_text("992", 330.0, 142.0, size=10.0)
    pg.add_text("FraudAmt", 70.0, 162.0, size=10.0)
    pg.add_text("49302", 230.0, 162.0, size=10.0)
    pg.add_text("66765", 330.0, 162.0, size=10.0)

    # 배경색 전환 행(175..200 흰색, 200..225 연녹색)
    pg.add_text("Joint", 130.0, 190.0, size=10.0)
    pg.add_text("510", 230.0, 190.0, size=10.0)
    pg.add_text("606", 330.0, 190.0, size=10.0)
    pg.add_text("Special", 130.0, 215.0, size=10.0)
    pg.add_text("60", 230.0, 215.0, size=10.0)
    pg.add_text("106", 330.0, 215.0, size=10.0)

    pdf_path = tmp_path / "poly_colspan.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg]))

    raw_doc = extract_pdf_document(pdf_path)
    assert len(raw_doc.pages[0].rects) == 7

    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    assert len(doc.tables) == 1
    tbl = doc.tables[0]
    assert tbl.headers == ["Item", "", "2024", "2025"]
    assert tbl.rows == [
        ["FraudCount", "", "630", "992"],
        ["FraudAmt", "", "49302", "66765"],
        ["", "Joint", "510", "606"],
        ["", "Special", "60", "106"],
    ]

    # 2) 내부 가로선이 아예 없는 묶음 행에서도 2줄 비고 셀이 3행으로 찢어지지 않아야 함
    xs2 = [60.0, 180.0, 260.0, 340.0, 520.0]
    ys2 = [100.0, 126.0, 210.0]
    lines2 = [RawVectorLine(x0=xs2[0], top=y, x1=xs2[-1], bottom=y) for y in ys2] + [
        RawVectorLine(x0=x, top=ys2[0], x1=x, bottom=ys2[-1]) for x in xs2
    ]

    def sp(txt: str, x0: float, top: float, x1: float) -> RawTextSpan:
        return RawTextSpan(
            text=txt,
            x0=x0,
            top=top,
            x1=x1,
            bottom=top + 10.0,
            font_name="MalgunGothic",
            font_size=10.0,
            is_bold=False,
            is_italic=False,
        )

    spans2 = [
        sp("유형", 80, 108, 120),
        sp("건수", 200, 108, 230),
        sp("금액", 280, 108, 310),
        sp("비고", 360, 108, 400),
        # 1번째 데이터 행: 비고 2줄, 나머지 열 세로 중앙
        sp("특정거래", 80, 144, 140),
        sp("647", 200, 144, 230),
        sp("213.2", 280, 144, 320),
        sp("수의계약 조건위반,", 355, 134, 480),
        sp("특정업체 몰아주기", 355, 154, 480),
        # 2번째 데이터 행: 1줄
        sp("가족거래", 80, 184, 140),
        sp("122", 200, 184, 230),
        sp("13.3", 280, 184, 320),
        sp("직계존비속 거래 등", 355, 184, 480),
    ]
    raw_pg = RawPageData(page=1, width=595.0, height=842.0, rotation=0, spans=spans2, lines=lines2)
    res2 = extract_page_tables(raw_pg)
    assert len(res2.tables) == 1
    assert res2.tables[0].rows == [
        ["특정거래", "647", "213.2", "수의계약 조건위반, 특정업체 몰아주기"],
        ["가족거래", "122", "13.3", "직계존비속 거래 등"],
    ]


def test_pdf_shadow_dedup_korean_superscript_and_table_page_not_multicolumn(
    tmp_path: Path,
) -> None:
    """그림자 중복 제거, `1｣` 위첨자 병합, 표 사이 단위 표기의 다단 오탐 방지를 검증한다."""
    from autoetl.parser.pdf.geometry import RawPageData, RawTextSpan, RawVectorLine
    from autoetl.parser.pdf.layout import analyze_page_layout
    from autoetl.parser.pdf.tables import extract_ruled_tables

    # 1) 실제 PDF에서 그림자/의사볼드 미세 오프셋 중복 텍스트 제거 확인
    pg = _PdfPageSpec()
    pg.add_text("Ref", 82.5, 80.5, font="F2", size=16.0)
    pg.add_text("Ref", 81.7, 79.8, font="F2", size=16.0)
    pg.add_text(" 1", 118.5, 80.5, font="F2", size=16.0)
    pg.add_text("1", 117.7, 79.8, font="F2", size=16.0)
    pg.add_text("Body paragraph text.", 70.0, 140.0, size=12.0)

    pdf_path = tmp_path / "shadow_dedup.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg]))
    doc = parse_document(pdf_path, options=ParseOptions(extract_crops=False))
    md = render_document_markdown(doc)
    assert "RefRef" not in md
    assert "Ref 1" in md

    # 2) `1｣` 위첨자 인라인 병합 + 두 전폭 표 사이의 좌측 소제목·우측 단위 표기 다단 오탐 방지
    def mk_sp(txt: str, x0: float, top: float, x1: float, fs: float = 12.0) -> RawTextSpan:
        return RawTextSpan(
            text=txt,
            x0=x0,
            top=top,
            x1=x1,
            bottom=top + fs,
            font_name="HCRBatang",
            font_size=fs,
            is_bold=False,
            is_italic=False,
        )

    xs = [70.0, 290.0, 520.0]
    t1_ys = [170.0, 200.0, 360.0]
    t2_ys = [460.0, 490.0, 650.0]
    lines: list[RawVectorLine] = []
    for y_list in (t1_ys, t2_ys):
        for y in y_list:
            lines.append(RawVectorLine(x0=xs[0], top=y, x1=xs[-1], bottom=y))
        for x in xs:
            lines.append(RawVectorLine(x0=x, top=y_list[0], x1=x, bottom=y_list[-1]))

    spans = [
        mk_sp("< 연도별 점검 결과", 140.0, 100.0, 280.0, 13.0),
        mk_sp("1｣", 281.0, 98.0, 292.0, 8.2),
        mk_sp(">", 298.0, 100.0, 306.0, 13.0),
        mk_sp("□ 분야별 점검 결과", 70.0, 130.0, 210.0, 14.0),
        mk_sp("(단위: 건, 억 원, 금액 기준)", 395.0, 152.0, 525.0, 10.0),
        mk_sp("사업분야", 90.0, 180.0, 160.0, 11.0),
        mk_sp("적발건수", 310.0, 180.0, 380.0, 11.0),
        mk_sp("합계", 90.0, 240.0, 140.0, 11.0),
        mk_sp("992", 310.0, 240.0, 350.0, 11.0),
        mk_sp("□ 패턴별 점검 결과", 70.0, 418.0, 210.0, 14.0),
        mk_sp("(단위: 건, 억 원, 건수 기준)", 395.0, 442.0, 525.0, 10.0),
        mk_sp("유형", 90.0, 470.0, 140.0, 11.0),
        mk_sp("적발금액", 310.0, 470.0, 380.0, 11.0),
        mk_sp("특정거래", 90.0, 540.0, 160.0, 11.0),
        mk_sp("213.2", 310.0, 540.0, 360.0, 11.0),
    ]
    raw_pg = RawPageData(page=1, width=595.0, height=842.0, rotation=0, spans=spans, lines=lines)
    tbl_res = extract_ruled_tables(raw_pg)
    assert len(tbl_res.tables) == 2
    layout_res = analyze_page_layout(
        raw_pg,
        excluded_span_indices=tbl_res.consumed_span_indices,
        header_footer_span_indices=frozenset(),
        body_font_size=14.0,
        outline=[],
        excluded_boxes=tbl_res.consumed_table_boxes,
    )
    assert layout_res.column_count == 1
    assert layout_res.column_gutters_x == []
    joined_texts = [b.text for b in layout_res.text_blocks]
    assert any("점검 결과1｣ >" in t for t in joined_texts)
    assert not any(t.strip() == "1｣" for t in joined_texts)


def test_pdf_type0_cidfont_widths_digit_one_and_tz_scaling(tmp_path: Path) -> None:
    """Type0 CIDFont `/W`+`/ToUnicode` 실측 폭, 숫자 `1` 고정폭, `Tz` 장평 보정을 검증한다."""
    from autoetl.parser.pdf.extractor import extract_pdf_document
    from autoetl.parser.pdf.geometry import estimate_text_width

    class _DummyStream:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def get_data(self) -> bytes:
            return self._data

    cmap = (
        b"begincmap\n"
        b"2 beginbfchar\n<0004> <ac00>\n<0005> <0031>\nendbfchar\n"
        b"1 beginbfrange\n<0006> <0007> <0032>\nendbfrange\n"
        b"endcmap\n"
    )
    fake_type0 = {
        "/Subtype": "/Type0",
        "/DescendantFonts": [{"/DW": 1000, "/W": [4, [970, 550, 550, 550]]}],
        "/ToUnicode": _DummyStream(cmap),
    }
    cache: dict[int, dict[str, float] | None] = {}
    assert estimate_text_width("가123", 10.0, fake_type0, width_cache=cache) == pytest.approx(
        (0.97 + 0.55 * 3) * 10.0, abs=0.05
    )
    # 폴백 휴리스틱에서도 '1'은 '2'와 동일한 숫자 폭이어야 하며 0.28로 축소되지 않아야 함
    assert estimate_text_width("1", 10.0, None) == pytest.approx(
        estimate_text_width("2", 10.0, None), abs=0.01
    )
    assert estimate_text_width("가", 10.0, None) == pytest.approx(9.7, abs=0.05)

    # `Tz` 수평 축척(50%)이 스팬 폭에 반영되는지 확인
    pg = _PdfPageSpec()
    pg.ops.append("BT /F1 12.00 Tf 100 Tz 1 0 0 1 70 700 Tm (ABCDEF) Tj ET")
    pg.ops.append("BT /F1 12.00 Tf 50 Tz 1 0 0 1 70 670 Tm (ABCDEF) Tj ET")
    pdf_path = tmp_path / "tz_scale.pdf"
    pdf_path.write_bytes(_build_pdf_bytes([pg]))
    extracted = extract_pdf_document(pdf_path)
    sp_normal, sp_half = extracted.pages[0].spans
    assert sp_half.width == pytest.approx(sp_normal.width * 0.5, rel=0.05)
