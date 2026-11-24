"""Auto Parser 결과물 내보내기 — 통합 Markdown, 구조·전략 리포트, 표 CSV/JSON, IR JSON."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from autoetl.parser.contracts import (
    ElementKind,
    ExtractedTable,
    PageCategory,
    ParsedDocument,
    ParseStrategy,
    RoutingPlan,
    VisionStatus,
    VisualRegion,
)

if TYPE_CHECKING:
    from autoetl.parser.chunker import ChunkOptions

_ORDERED_LIST_PREFIX_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]|[a-zA-Z][.)]|[ivxlcdmIVXLCDM]+\.|[가-하ㄱ-ㅎ][.)]|\(\d+\))\s+"
)

_CATEGORY_LABELS: dict[PageCategory, str] = {
    PageCategory.DIGITAL_TEXT: "디지털 본문",
    PageCategory.MULTI_COLUMN: "다단 레이아웃",
    PageCategory.TABLE_HEAVY: "표 중심",
    PageCategory.SCANNED_IMAGE: "스캔/이미지",
    PageCategory.HYBRID_TEXT_IMAGE: "텍스트+시각 혼재",
    PageCategory.SLIDE_LIKE: "슬라이드형",
    PageCategory.VECTOR_DIAGRAM_HEAVY: "벡터 도식/차트",
    PageCategory.BLANK: "빈 페이지",
}

_STRATEGY_LABELS: dict[ParseStrategy, str] = {
    ParseStrategy.NATIVE_TEXT: "네이티브 텍스트 추출",
    ParseStrategy.MULTI_COLUMN_LAYOUT: "다단 XY-Cut 읽기 순서",
    ParseStrategy.RULED_TABLE: "격자선 표 복원",
    ParseStrategy.HORIZONTAL_RULED_TABLE: "가로선(booktabs) 표 복원",
    ParseStrategy.BORDERLESS_TABLE: "무선 공백정렬 표 복원",
    ParseStrategy.EMBEDDED_IMAGE_OCR: "내장 이미지 OCR",
    ParseStrategy.VLM_REGION: "부분 영역 VLM 해석",
    ParseStrategy.VLM_FULL_PAGE: "전체 페이지 VLM 해석",
    ParseStrategy.SKIP_DECORATIVE: "장식/반복 요소 제외",
}


@dataclass(frozen=True)
class ExportManifest:
    """`export_parse_bundle`이 생성한 산출물 경로 모음."""

    out_dir: Path
    parsed_json: Path
    document_markdown: Path
    routing_report_json: Path
    routing_report_markdown: Path
    table_csv_paths: tuple[Path, ...]
    table_json_paths: tuple[Path, ...]
    crop_paths: tuple[Path, ...]
    chunks_jsonl: Path | None = None
    chunk_count: int = 0


def _escape_md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>").strip()


def render_table_markdown(table: ExtractedTable) -> str:
    """`ExtractedTable`을 GFM 파이프 테이블 Markdown으로 렌더링한다."""
    n_cols = table.col_count
    headers = list(table.headers)
    if len(headers) < n_cols:
        headers.extend(f"col_{i + 1}" for i in range(len(headers), n_cols))
    headers = headers[:n_cols]

    lines: list[str] = []
    if table.caption:
        lines.append(f"*{table.caption}*")
        lines.append("")
    elif table.continued_from_table_id:
        lines.append(f"*(이전 페이지 표 `{table.continued_from_table_id}`에서 이어짐)*")
        lines.append("")
    lines.append("| " + " | ".join(_escape_md_cell(h) for h in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in range(n_cols)) + " |")
    for row in table.rows:
        padded = list(row[:n_cols]) + [""] * max(0, n_cols - len(row))
        lines.append("| " + " | ".join(_escape_md_cell(c) for c in padded) + " |")
    return "\n".join(lines)


def _format_crop_link(crop_path: str | None, relative_to: Path | None) -> str | None:
    if not crop_path:
        return None
    p = Path(crop_path)
    if relative_to is not None:
        try:
            return p.resolve().relative_to(relative_to.resolve()).as_posix()
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def _render_visual_block_markdown(
    region: VisualRegion,
    *,
    relative_to: Path | None = None,
) -> str:
    rel_link = _format_crop_link(region.crop_path, relative_to)
    if region.vision_status == VisionStatus.COMPLETED and (
        region.vision_markdown or region.vision_text
    ):
        body = (region.vision_markdown or region.vision_text or "").strip()
        header = (
            f"<!-- {region.region_id}: {region.element_kind.value} via {region.vision_adapter} -->"
        )
        meta_bits: list[str] = []
        if region.vision_adapter:
            meta_bits.append(f"`{region.vision_adapter}`")
        if rel_link:
            meta_bits.append(f"원본 크롭: [{Path(rel_link).name}]({rel_link})")
        parts = [header]
        if region.caption:
            parts.append(f"*{region.caption}*")
        if meta_bits:
            parts.append(f"<sub>시각 해석: {' · '.join(meta_bits)}</sub>")
        parts.append("")
        parts.append(body)
        return "\n".join(parts)

    strat_label = _STRATEGY_LABELS.get(
        region.recommended_strategy, region.recommended_strategy.value
    )
    bbox_text = f"({region.bbox.x0}, {region.bbox.top}) ~ ({region.bbox.x1}, {region.bbox.bottom})"
    status_line = f"> - 상태: `{region.vision_status.value}` / 크롭: `{region.crop_status.value}`"
    if region.vision_adapter:
        status_line += f" / 어댑터: `{region.vision_adapter}`"
    if region.vision_status == VisionStatus.FAILED and region.vision_metadata.get("vision_error"):
        status_line += f" (`{region.vision_metadata['vision_error']}`)"
    lines = [
        f"> **[{region.element_kind.value.upper()} · {region.region_id}]** "
        f"권장 전략: `{region.recommended_strategy.value}` ({strat_label})",
        f"> - 판정 근거: {region.reason}",
        f"> - 위치(pt): `{bbox_text}`",
        status_line,
    ]
    if region.caption:
        lines.append(f"> - 캡션: {region.caption}")
    if rel_link:
        lines.append(f"> - 크롭 파일: [{Path(rel_link).name}]({rel_link})")
    if region.embedded_text_hints:
        hints_preview = ", ".join(region.embedded_text_hints[:12])
        lines.append(f"> - 내부 텍스트 힌트: {hints_preview}")
    return "\n".join(lines)


def render_document_markdown(
    doc: ParsedDocument,
    *,
    relative_to: Path | None = None,
    include_header_footer: bool = False,
) -> str:
    """통합 IR을 읽기 순서가 보존된 Markdown 문서로 렌더링한다."""
    parts: list[str] = []
    title = doc.document.title or doc.document.source_path
    parts.append(f"# {title}")
    parts.append("")
    parts.append(
        f"> 출처: `{doc.document.source_path}` · 형식: `{doc.document.format.value}` · "
        f"분석 페이지: {doc.routing_plan.summary.analyzed_pages}/{doc.document.page_count} · "
        f"추출 표: {len(doc.tables)}개 · 시각 영역: {len(doc.visual_regions)}개"
    )
    parts.append("")

    for parsed_page in doc.pages:
        cat_label = _CATEGORY_LABELS.get(
            parsed_page.route.category, parsed_page.route.category.value
        )
        strat_label = _STRATEGY_LABELS.get(
            parsed_page.route.primary_strategy, parsed_page.route.primary_strategy.value
        )
        parts.append(
            f"<!-- Page {parsed_page.page} | category={parsed_page.route.category.value} "
            f"| strategy={parsed_page.route.primary_strategy.value} -->"
        )
        parts.append(f"## Page {parsed_page.page} ({cat_label} · {strat_label})")
        parts.append("")

        table_map = {t.table_id: t for t in parsed_page.tables}
        visual_map = {v.region_id: v for v in parsed_page.visual_regions}
        linked_captions = {
            c
            for c in [
                *(t.caption for t in parsed_page.tables),
                *(v.caption for v in parsed_page.visual_regions),
            ]
            if c
        }

        if include_header_footer and parsed_page.header_footer_blocks:
            hf_texts = " / ".join(b.text for b in parsed_page.header_footer_blocks)
            parts.append(f"<sub>머리글·바닥글: {hf_texts}</sub>")
            parts.append("")

        if not parsed_page.blocks:
            parts.append("*(추출된 본문 블록이 없는 페이지)*")
            parts.append("")
            continue

        prev_was_list = False
        for blk in parsed_page.blocks:
            if prev_was_list and blk.kind != ElementKind.LIST_ITEM:
                parts.append("")
                prev_was_list = False

            if blk.kind == ElementKind.TABLE and blk.table_id and blk.table_id in table_map:
                parts.append(render_table_markdown(table_map[blk.table_id]))
                parts.append("")
            elif blk.visual_region_id and blk.visual_region_id in visual_map:
                parts.append(
                    _render_visual_block_markdown(
                        visual_map[blk.visual_region_id], relative_to=relative_to
                    )
                )
                parts.append("")
            elif blk.kind == ElementKind.HEADING:
                level = min(6, max(3, (blk.heading_level or 1) + 2))
                parts.append(f"{'#' * level} {blk.text}")
                parts.append("")
            elif blk.kind == ElementKind.LIST_ITEM:
                stripped = blk.text.strip()
                if stripped.startswith(("o ", "O ")):
                    sub = stripped[2:].strip()
                    if _ORDERED_LIST_PREFIX_RE.match(sub):
                        parts.append(f"  {sub}")
                    else:
                        parts.append(f"  - {sub}")
                elif _ORDERED_LIST_PREFIX_RE.match(stripped):
                    parts.append(stripped)
                else:
                    text = stripped.lstrip("•▪▫■□●○◦◆◇▶▷·-* ").strip()
                    parts.append(f"- {text or stripped}")
                prev_was_list = True
            elif blk.kind == ElementKind.FORMULA_OR_CODE:
                parts.append("```text")
                parts.append(blk.text)
                parts.append("```")
                parts.append("")
            elif blk.kind == ElementKind.CAPTION:
                if blk.text in linked_captions:
                    continue
                parts.append(f"*{blk.text}*")
                parts.append("")
            else:
                parts.append(blk.text)
                parts.append("")

        if prev_was_list:
            parts.append("")

        parts.append("---")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def render_routing_report_markdown(plan: RoutingPlan) -> str:
    """문서 구조 진단 및 페이지·요소별 파싱 전략 결정 근거 리포트를 Markdown으로 생성한다."""
    doc = plan.document
    s = plan.summary
    outline_desc = f"있음 ({len(plan.outline)}항목)" if doc.has_outline else "없음"
    body_font_desc = f"{s.detected_body_font_size:.1f} pt" if s.detected_body_font_size else "-"
    hf_desc = (
        f"{'감지' if s.running_header_detected else '미감지'} / "
        f"{'감지' if s.running_footer_detected else '미감지'}"
    )
    multi_col_desc = ", ".join(map(str, s.multi_column_pages)) if s.multi_column_pages else "없음"
    vision_desc = (
        ", ".join(map(str, s.vision_required_pages)) if s.vision_required_pages else "없음"
    )
    lines: list[str] = [
        f"# 문서 구조 진단 및 파싱 전략 리포트 — `{doc.source_path}`",
        "",
        "## 1. 문서 개요",
        "",
        "| 항목 | 값 |",
        "| --- | --- |",
        f"| 파일명 | `{doc.source_path}` |",
        f"| 형식 | `{doc.format.value}` |",
        f"| 크기 | {doc.file_size_bytes:,} bytes |",
        f"| 전체 / 분석 페이지 | {s.total_pages} / {s.analyzed_pages} |",
        f"| 제목 메타데이터 | {doc.title or '-'} |",
        f"| 목차(Outline) | {outline_desc} |",
        f"| 추정 본문 글꼴 크기 | {body_font_desc} |",
        f"| 반복 머리글 / 바닥글 | {hf_desc} |",
        f"| 탐지된 표 / 시각 영역 | 표 {s.table_count}개 · 시각 영역 {s.visual_region_count}개 |",
        f"| 다단 레이아웃 페이지 | {multi_col_desc} |",
        f"| OCR/VLM 필요 페이지 | {vision_desc} |",
        "",
        "## 2. 구조 유형 및 파싱 전략 분포",
        "",
        "| 페이지 구조 유형 | 페이지 수 |",
        "| --- | ---: |",
    ]
    for cat_key, cnt in s.category_counts.items():
        try:
            label = _CATEGORY_LABELS[PageCategory(cat_key)]
        except ValueError:
            label = cat_key
        lines.append(f"| {label} (`{cat_key}`) | {cnt} |")

    lines.extend(
        [
            "",
            "| 적용 파싱 전략 | 적용 횟수(주+보조) |",
            "| --- | ---: |",
        ]
    )
    for strat_key, cnt in s.strategy_counts.items():
        try:
            label = _STRATEGY_LABELS[ParseStrategy(strat_key)]
        except ValueError:
            label = strat_key
        lines.append(f"| {label} (`{strat_key}`) | {cnt} |")

    lines.extend(
        [
            "",
            "## 3. 페이지별 구조 분류 및 전략 결정",
            "",
            "| 페이지 | 크기(pt) | 구조 유형 | 주 파싱 전략 | 보조 전략 "
            "| 문자/표/시각 | 단수 | 신뢰도 | 결정 근거 |",
            "| ---: | --- | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )

    for dec in plan.pages:
        m = dec.metrics
        cat_label = _CATEGORY_LABELS.get(dec.category, dec.category.value)
        prim_label = _STRATEGY_LABELS.get(dec.primary_strategy, dec.primary_strategy.value)
        sec_text = (
            ", ".join(f"`{st.value}`" for st in dec.secondary_strategies)
            if dec.secondary_strategies
            else "-"
        )
        reason_text = _escape_md_cell("; ".join(dec.reasons))
        size_text = f"{m.width_pt:.0f}x{m.height_pt:.0f}"
        counts_text = f"{m.usable_char_count}자 / 표 {m.table_count} / 시각 {m.visual_region_count}"
        lines.append(
            f"| {dec.page} | {size_text} | {cat_label} (`{dec.category.value}`) | "
            f"{prim_label} (`{dec.primary_strategy.value}`) | {sec_text} | "
            f"{counts_text} | {m.column_count} | {dec.confidence:.2f} | {reason_text} |"
        )

    lines.extend(["", "## 4. 주요 비정형 요소(표·시각 영역) 라우팅 세부 내역", ""])
    special_kinds = (
        ElementKind.TABLE,
        ElementKind.FIGURE_IMAGE,
        ElementKind.VECTOR_DIAGRAM,
        ElementKind.SCANNED_PAGE,
        ElementKind.FORMULA_OR_CODE,
    )
    special_rows = [
        (dec.page, reg)
        for dec in plan.pages
        for reg in dec.regions
        if reg.element_kind in special_kinds or reg.requires_vision
    ]
    if not special_rows:
        lines.append("특수 비정형 요소(표·그림·도식·스캔)가 감지되지 않았다.")
    else:
        lines.extend(
            [
                "| 페이지 | 영역 ID | 요소 유형 | 배정 전략 | OCR/VLM | 신뢰도 | 근거 |",
                "| ---: | --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for page_num, reg in special_rows:
            lines.append(
                f"| {page_num} | `{reg.region_id}` | `{reg.element_kind.value}` | "
                f"`{reg.strategy.value}` | {'필요' if reg.requires_vision else '불필요'} | "
                f"{reg.confidence:.2f} | {_escape_md_cell(reg.reason)} |"
            )

    if plan.outline:
        lines.extend(["", "## 5. 문서 목차(Outline)", ""])
        for entry in plan.outline[:50]:
            indent = "  " * (entry.level - 1)
            pg = f" (p.{entry.page})" if entry.page else ""
            lines.append(f"{indent}- {entry.title}{pg}")

    return "\n".join(lines).rstrip() + "\n"


def export_table_csv(table: ExtractedTable, out_path: Path) -> None:
    """추출된 표 하나를 UTF-8 CSV로 저장한다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if table.headers:
            writer.writerow(table.headers)
        for row in table.rows:
            writer.writerow(row)


def export_table_json(table: ExtractedTable, out_path: Path) -> None:
    """추출된 표 하나를 셀·병합·메타 정보 포함 JSON으로 저장한다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = table.model_dump(mode="json")
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def export_parse_bundle(
    doc: ParsedDocument,
    out_dir: Path,
    *,
    include_header_footer: bool = False,
    include_chunks: bool = True,
    chunk_options: ChunkOptions | None = None,
) -> ExportManifest:
    """통합 IR JSON, Markdown, 청크(JSONL), 전략 리포트(JSON·MD), 표 CSV/JSON을 저장한다."""
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed_json = out_dir / "parsed.json"
    parsed_json.write_text(
        doc.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    routing_json = out_dir / "routing_report.json"
    routing_json.write_text(
        doc.routing_plan.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    routing_md = out_dir / "routing_report.md"
    routing_md.write_text(
        render_routing_report_markdown(doc.routing_plan),
        encoding="utf-8",
    )

    doc_md = out_dir / "document.md"
    doc_md.write_text(
        render_document_markdown(
            doc,
            relative_to=out_dir,
            include_header_footer=include_header_footer,
        ),
        encoding="utf-8",
    )

    chunks_jsonl_path: Path | None = None
    chunk_count = 0
    if include_chunks:
        from autoetl.parser.chunker import chunk_document, write_chunks_jsonl

        chunks = chunk_document(doc, options=chunk_options)
        chunks_jsonl_path = write_chunks_jsonl(chunks, out_dir / "chunks.jsonl")
        chunk_count = len(chunks)

    tables_dir = out_dir / "tables"
    csv_paths: list[Path] = []
    json_paths: list[Path] = []
    for table in doc.tables:
        csv_p = tables_dir / f"table_{table.table_id}.csv"
        json_p = tables_dir / f"table_{table.table_id}.json"
        export_table_csv(table, csv_p)
        export_table_json(table, json_p)
        csv_paths.append(csv_p)
        json_paths.append(json_p)

    crop_paths = tuple(Path(v.crop_path) for v in doc.visual_regions if v.crop_path is not None)

    return ExportManifest(
        out_dir=out_dir,
        parsed_json=parsed_json,
        document_markdown=doc_md,
        routing_report_json=routing_json,
        routing_report_markdown=routing_md,
        table_csv_paths=tuple(csv_paths),
        table_json_paths=tuple(json_paths),
        crop_paths=crop_paths,
        chunks_jsonl=chunks_jsonl_path,
        chunk_count=chunk_count,
    )
