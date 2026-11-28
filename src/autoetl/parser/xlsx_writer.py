"""무의존성 OOXML SpreadsheetML(`.xlsx`) 라이터 — 표를 시트로 담은 엑셀 파일을 만든다.

외부 서드파티 없이 Python 표준 라이브러리(`zipfile`)만 쓴다. 칸마다 글자인지 숫자인지를
파일에 적으므로, `=1+1` 같은 글도 앞에 `'`를 붙이지 않고 글자 칸으로 남길 수 있다.
수식 칸(`<f>`)은 쓰지 않는다. 어떤 값을 글자로, 어떤 값을 숫자로 둘지는 부르는 쪽
(`autoetl.parser.exporters`)이 정한다.
"""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

# 엑셀 시트 하나가 담을 수 있는 한도.
XLSX_MAX_ROWS = 1_048_576
XLSX_MAX_COLS = 16_384
XLSX_MAX_CELL_CHARS = 32_767

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_CT_PREFIX = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# 같은 입력이면 같은 파일이 나오도록 ZIP 항목 시각을 고정한다.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# 엑셀 내장 숫자 서식 번호(ECMA-376 Part 1 §18.8.30). 여기 없는 서식은 164번부터 새로 등록한다.
_BUILTIN_NUM_FMT_IDS: dict[str, int] = {
    "0": 1,
    "0.00": 2,
    "#,##0": 3,
    "#,##0.00": 4,
    "0%": 9,
    "0.00%": 10,
}
_TEXT_NUM_FMT_ID = 49  # "@" 글자 서식
# 글자 서식을 준 칸은 글이 255자를 넘으면 엑셀 판에 따라 ####로 보인다.
_TEXT_NUM_FMT_MAX_CHARS = 255
_FIRST_CUSTOM_NUM_FMT_ID = 164

_MIN_COL_WIDTH = 6
_MAX_COL_WIDTH = 60

# XML 1.0에 그대로 쓸 수 없는 글자와 CR. 엑셀 방식(`_xHHHH_`)으로 바꿔 쓴다.
_XML_UNSAFE_CHAR_RE = re.compile("[\x00-\x08\x0b\x0c\x0d\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
# 원문에 이미 `_xHHHH_` 꼴이 있으면 엑셀이 글자 하나로 풀어 버리므로 앞의 `_`를 먼저 바꿔 둔다.
_XML_ESCAPE_LOOKALIKE_RE = re.compile(r"_(?=x[0-9A-Fa-f]{4}_)")
_SHEET_NAME_FORBIDDEN_RE = re.compile(r"[\[\]:*?/\\\x00-\x1f]")
_SHEET_NAME_MAX_CHARS = 31


class XlsxLimitError(ValueError):
    """표가 엑셀 시트의 한도(행·열 수, 칸 글자 수)를 넘는다."""


@dataclass(frozen=True)
class XlsxText:
    """글자 칸.

    `force_text`가 참이면 `quotePrefix`와 글자 서식(`@`)을 함께 적는다. 엑셀에서 그 칸을
    다시 입력해도 수식이나 숫자로 바뀌지 않게 하려는 것이다. 글이 255자를 넘으면
    `quotePrefix`만 적는다.
    """

    text: str
    force_text: bool = False


@dataclass(frozen=True)
class XlsxNumber:
    """숫자 칸.

    `value`는 `-1234.5` 같은 10진수 글, `format_code`는 `#,##0.0` 같은 표시 서식이다.
    `display`는 화면에 보일 모습으로, 열 너비를 잡을 때만 쓴다.
    """

    value: str
    format_code: str
    display: str = ""


XlsxCell = XlsxText | XlsxNumber | None


@dataclass(frozen=True)
class XlsxSheet:
    """시트 하나. `rows`의 `None`과 빈 글은 빈칸으로 남긴다."""

    name: str
    rows: Sequence[Sequence[XlsxCell]]


def _utf16_length(text: str) -> int:
    """엑셀이 세는 방식(UTF-16 단위)의 글자 수."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def check_xlsx_sheet_limits(rows: Sequence[Sequence[XlsxCell]]) -> None:
    """`rows`가 엑셀 시트 한도 안에 들어가는지 본다. 넘으면 `XlsxLimitError`를 낸다."""
    if len(rows) > XLSX_MAX_ROWS:
        raise XlsxLimitError(f"행이 {len(rows):,}개로 엑셀 한도({XLSX_MAX_ROWS:,}개)를 넘는다.")
    for row in rows:
        if len(row) > XLSX_MAX_COLS:
            raise XlsxLimitError(f"열이 {len(row):,}개로 엑셀 한도({XLSX_MAX_COLS:,}개)를 넘는다.")
        for cell in row:
            if isinstance(cell, XlsxText) and _utf16_length(cell.text) > XLSX_MAX_CELL_CHARS:
                raise XlsxLimitError(f"한 칸의 글이 엑셀 한도({XLSX_MAX_CELL_CHARS:,}자)를 넘는다.")


def _xml_text(text: str) -> str:
    """칸 글을 XML 본문에 넣을 수 있게 바꾼다."""
    guarded = _XML_ESCAPE_LOOKALIKE_RE.sub("_x005F_", text)
    encoded = _XML_UNSAFE_CHAR_RE.sub(lambda m: f"_x{ord(m.group()):04X}_", guarded)
    return escape(encoded)


def _col_letters(index: int) -> str:
    """0-based 열 번호를 엑셀 열 문자(`0`→`A`, `26`→`AA`)로 바꾼다."""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _display_width(text: str) -> int:
    """가장 긴 줄의 화면 너비. 한글·한자 같은 전각 글자는 두 칸으로 센다."""
    widest = 0
    for line in text.splitlines() or [""]:
        width = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in line)
        widest = max(widest, width)
    return widest


def _safe_sheet_names(names: Sequence[str]) -> list[str]:
    """엑셀이 받는 시트 이름으로 다듬는다(금지 글자, 31자, 겹치는 이름)."""
    taken: set[str] = set()
    safe: list[str] = []
    for index, raw in enumerate(names):
        base = _SHEET_NAME_FORBIDDEN_RE.sub("_", raw).strip().strip("'")
        base = base[:_SHEET_NAME_MAX_CHARS].strip().strip("'")
        if not base or base.lower() == "history":  # History는 엑셀이 따로 쓰는 이름이다.
            base = f"Sheet{index + 1}"
        candidate = base
        suffix = 2
        while candidate.lower() in taken:
            tail = f"_{suffix}"
            candidate = base[: _SHEET_NAME_MAX_CHARS - len(tail)] + tail
            suffix += 1
        taken.add(candidate.lower())
        safe.append(candidate)
    return safe


class _StyleBook:
    """`xl/styles.xml`에 들어갈 칸 서식 목록. 0번은 서식 없는 기본 칸이다."""

    def __init__(self) -> None:
        self._xfs: list[tuple[int, bool, bool]] = [(0, False, False)]
        self._xf_index: dict[tuple[int, bool, bool], int] = {(0, False, False): 0}
        self._custom_num_fmts: dict[str, int] = {}

    def num_fmt_id(self, format_code: str) -> int:
        builtin = _BUILTIN_NUM_FMT_IDS.get(format_code)
        if builtin is not None:
            return builtin
        if format_code not in self._custom_num_fmts:
            self._custom_num_fmts[format_code] = _FIRST_CUSTOM_NUM_FMT_ID + len(
                self._custom_num_fmts
            )
        return self._custom_num_fmts[format_code]

    def xf(self, num_fmt_id: int, *, quote_prefix: bool = False, wrap: bool = False) -> int:
        key = (num_fmt_id, quote_prefix, wrap)
        if key not in self._xf_index:
            self._xf_index[key] = len(self._xfs)
            self._xfs.append(key)
        return self._xf_index[key]

    def to_xml(self) -> str:
        parts = [_XML_DECL, f'<styleSheet xmlns="{_NS_MAIN}">']
        if self._custom_num_fmts:
            parts.append(f'<numFmts count="{len(self._custom_num_fmts)}">')
            for code, fmt_id in self._custom_num_fmts.items():
                parts.append(f'<numFmt numFmtId="{fmt_id}" formatCode={quoteattr(code)}/>')
            parts.append("</numFmts>")
        parts.append('<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>')
        parts.append(
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
        )
        parts.append(
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border>'
            "</borders>"
        )
        parts.append(
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
            "</cellStyleXfs>"
        )
        parts.append(f'<cellXfs count="{len(self._xfs)}">')
        for num_fmt_id, quote_prefix, wrap in self._xfs:
            attrs = f'numFmtId="{num_fmt_id}" fontId="0" fillId="0" borderId="0" xfId="0"'
            if num_fmt_id != 0:
                attrs += ' applyNumberFormat="1"'
            if quote_prefix:
                attrs += ' quotePrefix="1"'
            if wrap:
                parts.append(f'<xf {attrs} applyAlignment="1"><alignment wrapText="1"/></xf>')
            else:
                parts.append(f"<xf {attrs}/>")
        parts.append("</cellXfs>")
        parts.append(
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        )
        parts.append("</styleSheet>")
        return "".join(parts)


class _SharedStrings:
    """`xl/sharedStrings.xml`에 들어갈 글 목록. 같은 글은 한 번만 담는다."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self._ref_count = 0

    def add(self, text: str) -> int:
        self._ref_count += 1
        return self._index.setdefault(text, len(self._index))

    def to_xml(self) -> str:
        parts = [
            _XML_DECL,
            f'<sst xmlns="{_NS_MAIN}" count="{self._ref_count}" uniqueCount="{len(self._index)}">',
        ]
        parts.extend(
            f'<si><t xml:space="preserve">{_xml_text(text)}</t></si>' for text in self._index
        )
        parts.append("</sst>")
        return "".join(parts)


def _column_widths(rows: Sequence[Sequence[XlsxCell]]) -> list[int]:
    widths: list[int] = []
    for row in rows:
        for col, cell in enumerate(row):
            if col >= len(widths):
                widths.extend([0] * (col + 1 - len(widths)))
            if cell is None:
                continue
            shown = cell.text if isinstance(cell, XlsxText) else (cell.display or cell.value)
            widths[col] = max(widths[col], _display_width(shown))
    return widths


def _sheet_xml_chunks(
    rows: Sequence[Sequence[XlsxCell]],
    styles: _StyleBook,
    strings: _SharedStrings,
) -> list[str]:
    widths = _column_widths(rows)
    chunks = [_XML_DECL, f'<worksheet xmlns="{_NS_MAIN}">']
    if rows and widths:
        chunks.append(f'<dimension ref="A1:{_col_letters(len(widths) - 1)}{len(rows)}"/>')
        chunks.append("<cols>")
        for col, width in enumerate(widths):
            fitted = min(max(width + 2, _MIN_COL_WIDTH), _MAX_COL_WIDTH)
            chunks.append(
                f'<col min="{col + 1}" max="{col + 1}" width="{fitted}" customWidth="1"/>'
            )
        chunks.append("</cols>")
    chunks.append("<sheetData>")
    for row_index, row in enumerate(rows):
        cells: list[str] = []
        for col, cell in enumerate(row):
            if cell is None or (isinstance(cell, XlsxText) and cell.text == ""):
                continue
            ref = f"{_col_letters(col)}{row_index + 1}"
            if isinstance(cell, XlsxText):
                as_text_format = (
                    cell.force_text and _utf16_length(cell.text) <= _TEXT_NUM_FMT_MAX_CHARS
                )
                style = styles.xf(
                    _TEXT_NUM_FMT_ID if as_text_format else 0,
                    quote_prefix=cell.force_text,
                    wrap="\n" in cell.text,
                )
                style_attr = f' s="{style}"' if style else ""
                cells.append(f'<c r="{ref}"{style_attr} t="s"><v>{strings.add(cell.text)}</v></c>')
            else:
                style = styles.xf(styles.num_fmt_id(cell.format_code))
                cells.append(f'<c r="{ref}" s="{style}"><v>{escape(cell.value)}</v></c>')
        if cells:
            chunks.append(f'<row r="{row_index + 1}">{"".join(cells)}</row>')
    chunks.append("</sheetData></worksheet>")
    return chunks


def _zip_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def write_xlsx_workbook(out_path: Path, sheets: Sequence[XlsxSheet]) -> None:
    """`sheets`를 시트로 담은 `.xlsx` 파일을 `out_path`에 쓴다.

    시트 한도를 넘는 시트가 있으면 파일을 만들기 전에 `XlsxLimitError`를 낸다.
    같은 폴더의 임시 파일에 다 쓴 뒤 `out_path`로 바꾸므로, 쓰다가 실패해도 `out_path`에는
    쓰다 만 파일이 남지 않고 이전 파일이 그대로 있다.
    """
    if not sheets:
        raise ValueError("엑셀 파일에는 시트가 하나 이상 있어야 한다.")
    for sheet in sheets:
        check_xlsx_sheet_limits(sheet.rows)

    names = _safe_sheet_names([sheet.name for sheet in sheets])
    styles = _StyleBook()
    strings = _SharedStrings()

    content_types = [
        _XML_DECL,
        f'<Types xmlns="{_NS_CONTENT_TYPES}">',
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        f'<Override PartName="/xl/workbook.xml" ContentType="{_CT_PREFIX}.sheet.main+xml"/>',
    ]
    workbook = [_XML_DECL, f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><sheets>']
    workbook_rels = [_XML_DECL, f'<Relationships xmlns="{_NS_PKG_REL}">']
    for number, name in enumerate(names, start=1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
            f'ContentType="{_CT_PREFIX}.worksheet+xml"/>'
        )
        workbook.append(f'<sheet name={quoteattr(name)} sheetId="{number}" r:id="rId{number}"/>')
        workbook_rels.append(
            f'<Relationship Id="rId{number}" Type="{_NS_REL}/worksheet" '
            f'Target="worksheets/sheet{number}.xml"/>'
        )
    styles_rid = len(names) + 1
    strings_rid = len(names) + 2
    content_types.extend(
        [
            f'<Override PartName="/xl/styles.xml" ContentType="{_CT_PREFIX}.styles+xml"/>',
            f'<Override PartName="/xl/sharedStrings.xml" '
            f'ContentType="{_CT_PREFIX}.sharedStrings+xml"/>',
            "</Types>",
        ]
    )
    workbook.append("</sheets></workbook>")
    workbook_rels.extend(
        [
            f'<Relationship Id="rId{styles_rid}" Type="{_NS_REL}/styles" Target="styles.xml"/>',
            f'<Relationship Id="rId{strings_rid}" Type="{_NS_REL}/sharedStrings" '
            'Target="sharedStrings.xml"/>',
            "</Relationships>",
        ]
    )
    root_rels = (
        f'{_XML_DECL}<Relationships xmlns="{_NS_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일 이름은 호출마다 다르게 하고 새로 만들 때만 연다. 같은 자리에 쓰는 다른
    # 실행이나 사용자가 둔 `<이름>.tmp` 파일을 건드리지 않기 위해서다.
    tmp_path = out_path.with_name(f"{out_path.name}.{secrets.token_hex(8)}.tmp")
    created = False
    try:
        with zipfile.ZipFile(tmp_path, "x") as zf:
            created = True
            zf.writestr(_zip_entry("[Content_Types].xml"), "".join(content_types))
            zf.writestr(_zip_entry("_rels/.rels"), root_rels)
            zf.writestr(_zip_entry("xl/workbook.xml"), "".join(workbook))
            zf.writestr(_zip_entry("xl/_rels/workbook.xml.rels"), "".join(workbook_rels))
            for number, sheet in enumerate(sheets, start=1):
                chunks = _sheet_xml_chunks(sheet.rows, styles, strings)
                zf.writestr(_zip_entry(f"xl/worksheets/sheet{number}.xml"), "".join(chunks))
            # 서식과 글 목록은 시트를 다 쓴 뒤에야 내용이 정해진다.
            zf.writestr(_zip_entry("xl/styles.xml"), styles.to_xml())
            zf.writestr(_zip_entry("xl/sharedStrings.xml"), strings.to_xml())
        os.replace(tmp_path, out_path)
    except BaseException:
        if created:
            tmp_path.unlink(missing_ok=True)
        raise


__all__ = [
    "XLSX_MAX_CELL_CHARS",
    "XLSX_MAX_COLS",
    "XLSX_MAX_ROWS",
    "XlsxCell",
    "XlsxLimitError",
    "XlsxNumber",
    "XlsxSheet",
    "XlsxText",
    "check_xlsx_sheet_limits",
    "write_xlsx_workbook",
]
