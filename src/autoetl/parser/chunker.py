"""RAG·LLM 컨텍스트 투입용 구조화 문서 청킹(`chunks.jsonl`) 엔진.

`ParsedDocument` 통합 IR의 제목 계층(`ElementKind.HEADING`), 페이지 경계, 표 무결성,
시각 영역 해석 결과를 보존하면서 검색·임베딩 친화적인 청크(`DocumentChunk`)를 생성한다.
외부 토크나이저 의존성 없이 한국어/CJK·영문 혼합 토큰 수를 근사 산출한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoetl.parser.contracts import (
    DocumentChunk,
    ElementKind,
    ExtractedTable,
    ParsedDocument,
    VisionStatus,
    VisualRegion,
)
from autoetl.parser.exporters import _escape_md_cell, render_table_markdown


@dataclass(frozen=True)
class ChunkOptions:
    """구조화 문서 청킹 옵션."""

    max_chars: int = 1500
    overlap_chars: int = 150
    include_section_header: bool = True
    include_visual_placeholders: bool = True


def estimate_token_count(text: str) -> int:
    """한국어/CJK(~2자/토큰)와 영문/숫자(~4자/토큰) 비율을 반영한 경량 토큰 추정."""
    if not text:
        return 0
    cjk_chars = 0
    other_chars = 0
    for ch in text:
        code = ord(ch)
        if (
            0xAC00 <= code <= 0xD7A3  # Hangul Syllables
            or 0x1100 <= code <= 0x11FF
            or 0x3130 <= code <= 0x318F
            or 0x4E00 <= code <= 0x9FFF  # CJK Unified
            or 0x3040 <= code <= 0x30FF  # Hiragana/Katakana
        ):
            cjk_chars += 1
        elif not ch.isspace():
            other_chars += 1
    est = int(cjk_chars / 1.8 + other_chars / 3.8)
    return max(1, est)


def _split_long_prose(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """단일 초대형 문단을 줄바꿈·문장·공백 경계 우선으로 오버랩 분할한다."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    safe_overlap = min(max(0, overlap_chars), max(0, max_chars // 3))
    while start < n:
        end = min(n, start + max_chars)
        if end < n:
            # 문단/줄바꿈 -> 문장 끝 -> 공백 순으로 자연 경계 탐색
            cut = -1
            for sep in ("\n\n", "\n", ". ", "다. ", "요. ", "? ", "! ", " "):
                pos = text.rfind(sep, start + max_chars // 2, end)
                if pos > start:
                    cut = pos + len(sep)
                    break
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        next_start = max(start + 1, end - safe_overlap)
        start = next_start
    return chunks


def _split_table_markdown_preserving_rows(
    table: ExtractedTable,
    max_chars: int,
) -> list[str]:
    """Markdown 표를 행 중간에서 자르지 않고 헤더 행을 반복해 분할한다."""
    full_md = render_table_markdown(table)
    if len(full_md) <= max_chars or len(table.rows) <= 1:
        return [full_md]

    n_cols = table.col_count
    headers = list(table.headers)
    if len(headers) < n_cols:
        headers.extend(f"col_{i + 1}" for i in range(len(headers), n_cols))
    headers = headers[:n_cols]

    prefix_lines: list[str] = []
    if table.caption:
        prefix_lines.extend([f"*{table.caption}*", ""])
    hdr_line = "| " + " | ".join(_escape_md_cell(h) for h in headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in range(n_cols)) + " |"
    header_block = "\n".join([*prefix_lines, hdr_line, sep_line])

    parts: list[str] = []
    cur_lines: list[str] = [header_block]
    cur_len = len(header_block)

    for row in table.rows:
        padded = list(row[:n_cols]) + [""] * max(0, n_cols - len(row))
        row_line = "| " + " | ".join(_escape_md_cell(c) for c in padded) + " |"
        if len(cur_lines) > 1 and cur_len + 1 + len(row_line) > max_chars:
            parts.append("\n".join(cur_lines))
            cur_lines = [header_block, row_line]
            cur_len = len(header_block) + 1 + len(row_line)
        else:
            cur_lines.append(row_line)
            cur_len += 1 + len(row_line)

    if len(cur_lines) > 1:
        parts.append("\n".join(cur_lines))
    return parts or [full_md]


def _visual_to_chunk_text(
    region: VisualRegion,
    *,
    include_placeholders: bool,
) -> str:
    """시각 영역을 RAG 검색용 텍스트로 변환한다."""
    if region.vision_status == VisionStatus.COMPLETED and (
        region.vision_markdown or region.vision_text
    ):
        body = (region.vision_markdown or region.vision_text or "").strip()
        if region.caption:
            return f"*{region.caption}*\n{body}"
        return body
    if not include_placeholders:
        return ""
    bits: list[str] = []
    if region.caption:
        bits.append(region.caption)
    if region.embedded_text_hints:
        bits.append(", ".join(region.embedded_text_hints[:10]))
    if not bits:
        return ""
    return f"[{region.element_kind.value.upper()} {region.region_id}] " + " — ".join(bits)


@dataclass
class _HeadingEntry:
    """제목 스택 항목(레벨·제목·페이지·청크 반영 여부)."""

    level: int
    title: str
    page: int
    represented: bool = False


def chunk_document(
    doc: ParsedDocument,
    *,
    options: ChunkOptions | None = None,
) -> list[DocumentChunk]:
    """`ParsedDocument`를 제목 계층·페이지·표 무결성을 보존한 `DocumentChunk` 목록으로 변환한다."""
    opts = options or ChunkOptions()
    max_chars = max(200, opts.max_chars)
    overlap_chars = min(max(0, opts.overlap_chars), max_chars // 3)

    table_by_id: dict[str, ExtractedTable] = {t.table_id: t for t in doc.tables}
    vis_by_id: dict[str, VisualRegion] = {v.region_id: v for v in doc.visual_regions}
    emitted_tables: set[str] = set()
    emitted_visuals: set[str] = set()

    heading_stack: list[_HeadingEntry] = []
    chunks: list[DocumentChunk] = []

    # 현재 축적 중인 산문 버퍼 상태
    buf_segments: list[str] = []
    buf_pages: list[int] = []
    buf_kinds: list[ElementKind] = []
    buf_table_ids: list[str] = []
    buf_vis_ids: list[str] = []
    buf_section: list[str] = []

    def _current_section_path() -> list[str]:
        return [entry.title for entry in heading_stack]

    def _make_section_prefix(sec_path: list[str], body_text: str) -> str:
        if not opts.include_section_header or not sec_path:
            return body_text
        breadcrumb = " > ".join(sec_path)
        header_line = f"[{breadcrumb}]"
        if body_text.startswith(header_line):
            return body_text
        return f"{header_line}\n{body_text}"

    def _emit_chunk(
        raw_body: str,
        *,
        pages: list[int],
        kinds: list[ElementKind],
        sec_path: list[str],
        t_ids: list[str],
        v_ids: list[str],
        mark_stack_represented: bool = True,
    ) -> None:
        cleaned = raw_body.strip()
        if not cleaned:
            return
        full_text = _make_section_prefix(sec_path, cleaned)
        c_idx = len(chunks)
        p_start = min(pages) if pages else 1
        p_end = max(pages) if pages else p_start
        uniq_kinds = list(dict.fromkeys(kinds)) or [ElementKind.PARAGRAPH]
        chunks.append(
            DocumentChunk(
                chunk_id=f"{doc.document.sha256[:8]}_c{c_idx:04d}",
                chunk_index=c_idx,
                source_path=doc.document.source_path,
                format=doc.document.format,
                sha256=doc.document.sha256,
                page_start=p_start,
                page_end=p_end,
                section_path=list(sec_path),
                element_kinds=uniq_kinds,
                text=full_text,
                char_count=len(full_text),
                estimated_tokens=estimate_token_count(full_text),
                table_ids=list(dict.fromkeys(t_ids)),
                visual_region_ids=list(dict.fromkeys(v_ids)),
            )
        )
        if mark_stack_represented:
            for entry in heading_stack:
                entry.represented = True

    def _emit_unrepresented_headings(
        popped: list[_HeadingEntry],
        surviving: list[_HeadingEntry],
    ) -> None:
        unrep = [entry for entry in popped if not entry.represented]
        if not unrep:
            return
        ancestors = [entry.title for entry in surviving] + [
            entry.title for entry in popped if entry.represented
        ]
        _emit_chunk(
            "\n".join(entry.title for entry in unrep),
            pages=[entry.page for entry in unrep],
            kinds=[ElementKind.HEADING],
            sec_path=ancestors,
            t_ids=[],
            v_ids=[],
            mark_stack_represented=False,
        )
        for entry in surviving:
            entry.represented = True

    def _flush_buffer(*, keep_overlap: bool = False) -> None:
        nonlocal buf_segments, buf_pages, buf_kinds, buf_table_ids, buf_vis_ids, buf_section
        if not buf_segments:
            return
        joined = "\n\n".join(buf_segments).strip()
        prev_pages = list(buf_pages)
        prev_sec = list(buf_section)
        _emit_chunk(
            joined,
            pages=prev_pages,
            kinds=list(buf_kinds),
            sec_path=prev_sec,
            t_ids=list(buf_table_ids),
            v_ids=list(buf_vis_ids),
        )
        tail_overlap = ""
        if keep_overlap and overlap_chars > 0 and len(joined) > overlap_chars:
            tail_raw = joined[-overlap_chars:]
            sp = tail_raw.find(" ")
            tail_overlap = tail_raw[sp + 1 :].strip() if 0 <= sp < len(tail_raw) // 2 else tail_raw
        buf_segments = [tail_overlap] if tail_overlap else []
        buf_pages = [prev_pages[-1]] if (tail_overlap and prev_pages) else []
        buf_kinds = [ElementKind.PARAGRAPH] if tail_overlap else []
        buf_table_ids = []
        buf_vis_ids = []
        buf_section = prev_sec if tail_overlap else []

    def _append_prose(
        seg: str,
        *,
        page_num: int,
        kind: ElementKind,
        vis_id: str | None = None,
    ) -> None:
        nonlocal buf_section
        cleaned = seg.strip()
        if not cleaned:
            return
        sec_path = _current_section_path()
        prefix_overhead = (
            len(" > ".join(sec_path)) + 4 if (opts.include_section_header and sec_path) else 0
        )
        eff_max = max(160, max_chars - prefix_overhead)

        if len(cleaned) > eff_max:
            _flush_buffer(keep_overlap=False)
            for piece in _split_long_prose(cleaned, eff_max, overlap_chars):
                _emit_chunk(
                    piece,
                    pages=[page_num],
                    kinds=[kind],
                    sec_path=sec_path,
                    t_ids=[],
                    v_ids=[vis_id] if vis_id else [],
                )
            return

        cur_len = sum(len(s) for s in buf_segments) + max(0, len(buf_segments) - 1) * 2
        if buf_segments and cur_len + 2 + len(cleaned) > eff_max:
            _flush_buffer(keep_overlap=True)

        if not buf_segments:
            buf_section = sec_path
        buf_segments.append(cleaned)
        buf_pages.append(page_num)
        buf_kinds.append(kind)
        if vis_id:
            buf_vis_ids.append(vis_id)

    for page in doc.pages:
        p_num = page.page
        for blk in page.blocks:
            if blk.kind == ElementKind.HEADER_FOOTER:
                continue

            if blk.kind == ElementKind.HEADING and blk.text.strip():
                _flush_buffer(keep_overlap=False)
                lvl = blk.heading_level or 1
                heading_txt = " ".join(blk.text.split()).strip()
                cut_idx = len(heading_stack)
                while cut_idx > 0 and heading_stack[cut_idx - 1].level >= lvl:
                    cut_idx -= 1
                if cut_idx < len(heading_stack):
                    popped = heading_stack[cut_idx:]
                    del heading_stack[cut_idx:]
                    _emit_unrepresented_headings(popped, heading_stack)
                heading_stack.append(_HeadingEntry(level=lvl, title=heading_txt, page=p_num))
                continue

            if blk.kind == ElementKind.TABLE and blk.table_id and blk.table_id in table_by_id:
                if blk.table_id in emitted_tables:
                    continue
                emitted_tables.add(blk.table_id)
                _flush_buffer(keep_overlap=False)
                tbl = table_by_id[blk.table_id]
                sec_path = _current_section_path()
                prefix_overhead = (
                    len(" > ".join(sec_path)) + 4
                    if (opts.include_section_header and sec_path)
                    else 0
                )
                eff_max = max(300, max_chars - prefix_overhead)
                for tbl_part in _split_table_markdown_preserving_rows(tbl, eff_max):
                    _emit_chunk(
                        tbl_part,
                        pages=[p_num],
                        kinds=[ElementKind.TABLE],
                        sec_path=sec_path,
                        t_ids=[tbl.table_id],
                        v_ids=[],
                    )
                continue

            if blk.visual_region_id and blk.visual_region_id in vis_by_id:
                if blk.visual_region_id in emitted_visuals:
                    continue
                emitted_visuals.add(blk.visual_region_id)
                vis = vis_by_id[blk.visual_region_id]
                v_txt = _visual_to_chunk_text(
                    vis,
                    include_placeholders=opts.include_visual_placeholders,
                )
                if v_txt:
                    _append_prose(
                        v_txt, page_num=p_num, kind=vis.element_kind, vis_id=vis.region_id
                    )
                continue

            text_val = blk.text.strip()
            if not text_val:
                continue
            if blk.kind == ElementKind.LIST_ITEM and not text_val.startswith(("-", "*", "•")):
                text_val = f"- {text_val}"
            elif blk.kind == ElementKind.FORMULA_OR_CODE:
                text_val = f"```\n{text_val}\n```"
            _append_prose(text_val, page_num=p_num, kind=blk.kind)

        # 블록에 매핑되지 않은 잔여 표 보완
        for tbl in page.tables:
            if tbl.table_id in emitted_tables:
                continue
            emitted_tables.add(tbl.table_id)
            _flush_buffer(keep_overlap=False)
            sec_path = _current_section_path()
            for tbl_part in _split_table_markdown_preserving_rows(tbl, max(300, max_chars)):
                _emit_chunk(
                    tbl_part,
                    pages=[p_num],
                    kinds=[ElementKind.TABLE],
                    sec_path=sec_path,
                    t_ids=[tbl.table_id],
                    v_ids=[],
                )

    _flush_buffer(keep_overlap=False)

    # 본문 없이 끝난 제목 섹션(예: 제목 전용 슬라이드·빈 장/절)도 원래 페이지 번호와 함께 보존
    if heading_stack:
        _emit_unrepresented_headings(heading_stack, [])

    return chunks


def render_chunks_jsonl(chunks: list[DocumentChunk]) -> str:
    """`DocumentChunk` 목록을 UTF-8 JSONL 문자열로 직렬화한다."""
    if not chunks:
        return ""
    return "\n".join(chunk.model_dump_json() for chunk in chunks) + "\n"


def write_chunks_jsonl(chunks: list[DocumentChunk], out_path: Path) -> Path:
    """`DocumentChunk` 목록을 `chunks.jsonl` 파일로 저장한다."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_chunks_jsonl(chunks), encoding="utf-8")
    return out_path


__all__ = [
    "ChunkOptions",
    "chunk_document",
    "estimate_token_count",
    "render_chunks_jsonl",
    "write_chunks_jsonl",
]
