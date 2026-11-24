"""무의존성 OOXML SpreadsheetML(`.xlsx`) 패키지·공유 문자열·스타일/날짜 서식 리더.

외부 서드파티 없이 Python 표준 라이브러리(`zipfile`, `xml.etree.ElementTree`)만으로
`xl/workbook.xml`, `xl/sharedStrings.xml`, `xl/styles.xml`, 시트 XML, 관계(`.rels`)를
안전하게 읽는다. ZIP 폭탄 방어 상한과 OLE2 암호화(`.xlsx` EncryptedPackage) 가드를 포함한다.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.pptx.ooxml import (
    MAX_MEDIA_BYTES,
    MAX_XML_BYTES,
    Relationship,
    local_name,
)


def _normalize_part_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _resolve_rel_target(owner_part: str, target: str) -> str:
    cleaned = target.replace("\\", "/")
    if cleaned.startswith("/"):
        return cleaned.lstrip("/")
    base = owner_part.rsplit("/", 1)[0] if "/" in owner_part else ""
    parts = [p for p in (f"{base}/{cleaned}" if base else cleaned).split("/") if p and p != "."]
    stack: list[str] = []
    for p in parts:
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    return "/".join(stack)


def _parse_xml_bytes(raw: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_SHARED_STRINGS_BYTES = 64 * 1024 * 1024
_MAX_SHEET_XML_BYTES = 64 * 1024 * 1024

_CELL_REF_RE = re.compile(r"^\$?([A-Za-z]{1,4})\$?(\d+)$")

# 표준 Excel 내장 날짜/시간 numFmtId (ECMA-376 Part 1 §18.8.30 + 한국/동아시아 확장)
_BUILTIN_DATE_FMT_IDS = frozenset(
    {
        14,
        15,
        16,
        17,
        22,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        34,
        35,
        36,
        50,
        51,
        52,
        53,
        54,
        55,
        56,
        57,
        58,
    }
)
_BUILTIN_TIME_FMT_IDS = frozenset({18, 19, 20, 21, 45, 46, 47})
_BUILTIN_DATETIME_FMT_IDS = frozenset({22})
_BUILTIN_PERCENT_FMT_IDS = frozenset({9, 10})
_BUILTIN_COMMA_FMT_IDS = frozenset({3, 4, 37, 38, 39, 40, 41, 42, 43, 44})

_QUOTED_OR_BRACKET_RE = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')


def col_letters_to_index(letters: str) -> int:
    """Excel 열 문자(`A`→0, `Z`→25, `AA`→26)를 0-based 인덱스로 변환한다."""
    acc = 0
    for ch in letters.upper():
        acc = acc * 26 + (ord(ch) - ord("A") + 1)
    return max(0, acc - 1)


def parse_cell_ref(ref: str) -> tuple[int, int] | None:
    """`B12` 같은 셀 좌표를 0-based `(row, col)`로 변환한다."""
    m = _CELL_REF_RE.match(ref.strip())
    if not m:
        return None
    col = col_letters_to_index(m.group(1))
    row = int(m.group(2)) - 1
    if row < 0 or col < 0:
        return None
    return (row, col)


def parse_cell_range(ref_range: str) -> tuple[int, int, int, int] | None:
    """`A1:C3` 범위를 0-based inclusive `(r0, c0, r1, c1)`로 변환한다."""
    parts = ref_range.strip().split(":")
    if len(parts) == 1:
        rc = parse_cell_ref(parts[0])
        return (rc[0], rc[1], rc[0], rc[1]) if rc is not None else None
    if len(parts) != 2:
        return None
    p0 = parse_cell_ref(parts[0])
    p1 = parse_cell_ref(parts[1])
    if p0 is None or p1 is None:
        return None
    return (min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1]))


@dataclass(frozen=True)
class XlsxSheetRef:
    """워크북의 개별 워크시트 참조 정보."""

    sheet_index: int  # 1-based 표시 순서
    name: str
    sheet_id: str
    part_path: str
    state: str = "visible"


@dataclass(frozen=True)
class XlsxCellStyle:
    """`xl/styles.xml` `<cellXfs>` 항목 요약."""

    num_fmt_id: int = 0
    fmt_code: str = ""
    has_border: bool = False
    is_bold: bool = False
    font_size_pt: float | None = None


@dataclass(frozen=True)
class XlsxCoreMetadata:
    """`docProps/core.xml` 및 `docProps/app.xml` 메타데이터."""

    title: str | None = None
    author: str | None = None
    creator: str | None = None


@dataclass
class XlsxStyleCatalog:
    """`xl/styles.xml`에서 해석한 숫자 서식·테두리·글꼴 카탈로그."""

    cell_xfs: list[XlsxCellStyle] = field(default_factory=list)
    date1904: bool = False

    def get_style(self, style_idx: int | None) -> XlsxCellStyle | None:
        if style_idx is None or style_idx < 0 or style_idx >= len(self.cell_xfs):
            return None
        return self.cell_xfs[style_idx]


def _classify_format_code(num_fmt_id: int, fmt_code: str) -> str:
    """Excel 숫자 서식에서 `datetime`/`date`/`time`/`percent`/`comma`/`general`을 판별한다."""
    if num_fmt_id in _BUILTIN_DATETIME_FMT_IDS:
        return "datetime"
    if num_fmt_id in _BUILTIN_DATE_FMT_IDS:
        return "date"
    if num_fmt_id in _BUILTIN_TIME_FMT_IDS:
        return "time"
    if num_fmt_id in _BUILTIN_PERCENT_FMT_IDS:
        return "percent"
    if num_fmt_id in _BUILTIN_COMMA_FMT_IDS:
        return "comma"
    if not fmt_code or fmt_code.lower() == "general":
        return "general"

    # 양수 섹션(첫 번째 `;` 이전)에서 리터럴/색상/로캘 태그 제거
    first_sec = fmt_code.split(";", 1)[0]
    cleaned = _QUOTED_OR_BRACKET_RE.sub("", first_sec).lower()
    if "%" in cleaned:
        return "percent"
    has_date = any(tok in cleaned for tok in ("yy", "dd", "년", "월", "일"))
    has_time = any(tok in cleaned for tok in ("h", "ss", "am/pm", "시", "분"))
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    if "," in cleaned and ("0" in cleaned or "#" in cleaned):
        return "comma"
    return "general"


def _serial_to_datetime(serial: float, *, date1904: bool) -> datetime.datetime | None:
    """Excel 직렬 날짜 숫자를 `datetime`으로 변환한다(1900 윤년 버그 및 1904 시스템 보정)."""
    if serial < 0 or serial > 2958466:  # ~9999년 상한
        return None
    if date1904:
        base = datetime.datetime(1904, 1, 1)
        days = int(serial)
    else:
        base = datetime.datetime(1899, 12, 31)
        days = int(serial)
        if days >= 60:
            days -= 1  # Excel 1900-02-29 호환 보정
    frac_sec = round((serial - int(serial)) * 86400)
    try:
        return base + datetime.timedelta(days=days, seconds=frac_sec)
    except OverflowError:
        return None


def _format_decimal_clean(raw_str: str, *, comma: bool) -> str:
    """원본 숫자 문자열의 정밀도를 보존하면서 IEEE-754 부동소수 잔차만 안전하게 정리한다."""
    try:
        dec = Decimal(raw_str)
    except InvalidOperation:
        return raw_str
    if not dec.is_finite():
        return raw_str
    if dec == 0:
        return "0"

    norm = dec.normalize()
    norm_tuple = norm.as_tuple()
    sig_len = len(norm_tuple.digits)

    # Excel이 IEEE-754 float64로 직렬화한 16~17자리 잔차(예: 0.30000000000000004 → 0.3)만 정리.
    # 15자리 이하(1e-10, 1.0000000005)와 18자리 이상 고정밀 소수는 원 정밀도를 보존한다.
    has_frac_or_exp = ("." in raw_str) or ("e" in raw_str.lower())
    if 16 <= sig_len <= 17 and has_frac_or_exp:
        num = float(raw_str)
        if math.isfinite(num):
            cand_str = f"{num:.15g}"
            try:
                cand_dec = Decimal(cand_str)
            except InvalidOperation:
                cand_dec = norm
            cand_sig = len(cand_dec.normalize().as_tuple().digits)
            if (
                cand_sig <= 13
                and math.isclose(float(cand_str), num, rel_tol=1e-14, abs_tol=0.0)
                and (sig_len - cand_sig >= 3)
            ):
                dec = cand_dec
                norm = dec.normalize()
                norm_tuple = norm.as_tuple()

    exp = norm_tuple.exponent
    assert isinstance(exp, int)
    digits_len = len(norm_tuple.digits)
    raw_has_exp = "e" in raw_str.lower()

    if exp >= 0:
        if (digits_len + exp <= 20) and not (raw_has_exp and exp >= 6):
            iv = int(norm)
            return f"{iv:,}" if comma else str(iv)
        return str(norm).replace("E+", "e+").replace("E", "e")

    if abs(norm) < Decimal("1e-6") and (raw_has_exp or abs(exp) > 10):
        return str(norm).replace("E", "e")

    formatted = f"{norm:f}"
    if comma and "." in formatted:
        int_part, dec_part = formatted.split(".", 1)
        try:
            return f"{int(int_part):,}.{dec_part}"
        except ValueError:
            return formatted
    return formatted


def format_excel_number(
    raw_value: str,
    style: XlsxCellStyle | None,
    *,
    date1904: bool = False,
) -> str:
    """Excel 원시 숫자 문자열을 `styles.xml` 서식(날짜·시간·백분율·천단위·소수)에 맞게 포맷한다."""
    s = raw_value.strip()
    if not s:
        return ""
    try:
        num = float(s)
    except ValueError:
        return s

    kind = (
        _classify_format_code(style.num_fmt_id, style.fmt_code) if style is not None else "general"
    )

    if kind in ("date", "datetime", "time") and math.isfinite(num):
        dt = _serial_to_datetime(num, date1904=date1904)
        if dt is not None:
            if kind == "time" or num < 1.0:
                return dt.strftime("%H:%M:%S")
            if kind == "datetime" or (dt.hour or dt.minute or dt.second):
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")

    if kind == "percent":
        try:
            pct_dec = Decimal(s) * Decimal(100)
        except InvalidOperation:
            return s
        return _format_decimal_clean(str(pct_dec), comma=False) + "%"

    return _format_decimal_clean(s, comma=(kind == "comma"))


def _extract_si_text(si_elem: ET.Element) -> str:
    """`<si>` 또는 `<is>` 내부 `<t>` 텍스트를 합친다(`<rPh>` 발음 기호 루비는 제외)."""
    parts: list[str] = []
    for child in si_elem:
        tag = local_name(child.tag)
        if tag == "t" and child.text:
            parts.append(child.text)
        elif tag == "r":
            for sub in child:
                if local_name(sub.tag) == "t" and sub.text:
                    parts.append(sub.text)
    return "".join(parts)


class XlsxPackage:
    """`.xlsx` OOXML ZIP 아카이브의 안전한 읽기 래퍼."""

    def __init__(self, source_path: Path | str) -> None:
        self.source_path = Path(source_path)
        if not self.source_path.is_file():
            raise ParserInputError(f"XLSX 파일이 존재하지 않는다: {self.source_path}")
        raw_head = self.source_path.read_bytes()
        self.file_size = len(raw_head)
        self.sha256 = hashlib.sha256(raw_head).hexdigest()
        if raw_head.startswith(_OLE2_MAGIC):
            raise ParserInputError(
                f"암호화된 XLSX(OLE2 EncryptedPackage) 파일이다: {self.source_path.name}"
            )
        if not zipfile.is_zipfile(self.source_path):
            raise ParserInputError(f"유효한 XLSX(ZIP) 파일이 아니다: {self.source_path.name}")
        try:
            self._zf = zipfile.ZipFile(self.source_path, "r")
        except zipfile.BadZipFile as exc:
            raise ParserInputError(f"손상된 XLSX 파일이다: {self.source_path.name}") from exc

        self._name_map: dict[str, str] = {
            _normalize_part_path(name).lower(): name for name in self._zf.namelist()
        }
        self._xml_cache: dict[str, ET.Element | None] = {}
        self._rels_cache: dict[str, dict[str, Relationship]] = {}
        self._shared_strings_cache: list[str] | None = None
        self._styles_cache: XlsxStyleCatalog | None = None
        self._date1904: bool = False

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> XlsxPackage:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _resolve_member(self, part_path: str) -> str | None:
        norm = _normalize_part_path(part_path).lower()
        return self._name_map.get(norm)

    def has_part(self, part_path: str) -> bool:
        return self._resolve_member(part_path) is not None

    def read_bytes(self, part_path: str, *, max_bytes: int = MAX_MEDIA_BYTES) -> bytes | None:
        member = self._resolve_member(part_path)
        if member is None:
            return None
        info = self._zf.getinfo(member)
        if info.file_size > max_bytes:
            raise ParserInputError(
                f"XLSX 내부 파트 크기가 허용 상한({max_bytes}B)을 초과했다: "
                f"{part_path} ({info.file_size}B)"
            )
        with self._zf.open(info, "r") as fp:
            data = fp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ParserInputError(f"XLSX 내부 파트 압축 해제 크기가 상한을 초과했다: {part_path}")
        return data

    def open_stream(self, part_path: str, *, max_bytes: int = _MAX_SHEET_XML_BYTES) -> bytes | None:
        """대용량 시트/공유문자열 XML을 확장 상한으로 안전하게 읽는다."""
        return self.read_bytes(part_path, max_bytes=max_bytes)

    def read_xml(self, part_path: str) -> ET.Element | None:
        key = _normalize_part_path(part_path).lower()
        if key in self._xml_cache:
            return self._xml_cache[key]
        raw = self.read_bytes(part_path, max_bytes=MAX_XML_BYTES)
        if raw is None:
            self._xml_cache[key] = None
            return None
        elem = _parse_xml_bytes(raw)
        self._xml_cache[key] = elem
        return elem

    def read_rels(self, owner_part: str) -> dict[str, Relationship]:
        norm = _normalize_part_path(owner_part)
        if norm in self._rels_cache:
            return self._rels_cache[norm]
        if "/" in norm:
            parent, name = norm.rsplit("/", 1)
            rels_part = f"{parent}/_rels/{name}.rels"
        else:
            rels_part = f"_rels/{norm}.rels"

        rels: dict[str, Relationship] = {}
        root = self.read_xml(rels_part)
        if root is not None:
            for child in root:
                if local_name(child.tag) != "Relationship":
                    continue
                rid = child.attrib.get("Id", "")
                rtype = child.attrib.get("Type", "")
                target = child.attrib.get("Target", "")
                tmode = child.attrib.get("TargetMode", "")
                if not rid or not target:
                    continue
                is_ext = tmode.lower() == "external"
                resolved = target if is_ext else _resolve_rel_target(norm, target)
                rels[rid] = Relationship(
                    r_id=rid,
                    rel_type=rtype,
                    target=resolved,
                    is_external=is_ext,
                )
        self._rels_cache[norm] = rels
        return rels

    def list_sheets(self) -> list[XlsxSheetRef]:
        """`xl/workbook.xml`에서 워크시트 목록과 `date1904` 플래그를 읽는다."""
        wb_part = "xl/workbook.xml"
        root = self.read_xml(wb_part)
        if root is None:
            raise ParserInputError(
                f"XLSX 워크북 정의(xl/workbook.xml)를 찾을 수 없다: {self.source_path.name}"
            )
        for el in root.iter():
            if local_name(el.tag) == "workbookPr":
                self._date1904 = el.attrib.get("date1904", "").lower() in ("1", "true")
                break

        rels = self.read_rels(wb_part)
        sheets: list[XlsxSheetRef] = []
        idx = 0
        for el in root.iter():
            if local_name(el.tag) != "sheet":
                continue
            name = el.attrib.get("name", "").strip() or f"Sheet{idx + 1}"
            state = el.attrib.get("state", "visible").strip() or "visible"
            if state == "veryHidden":
                continue
            sheet_id = el.attrib.get("sheetId", str(idx + 1))
            rid = ""
            for k, v in el.attrib.items():
                if local_name(k) == "id":
                    rid = v
                    break
            rel = rels.get(rid)
            if rel is None or not self.has_part(rel.target):
                continue
            # 차트시트(xl/chartsheets/...)가 아닌 일반 워크시트 우선 포함
            idx += 1
            sheets.append(
                XlsxSheetRef(
                    sheet_index=idx,
                    name=name,
                    sheet_id=sheet_id,
                    part_path=rel.target,
                    state=state,
                )
            )
        if not sheets:
            raise ParserInputError(
                f"XLSX 파일에 읽을 수 있는 워크시트가 없다: {self.source_path.name}"
            )
        return sheets

    def read_shared_strings(self) -> list[str]:
        """`xl/sharedStrings.xml`을 파싱해 공유 문자열 테이블을 반환한다."""
        if self._shared_strings_cache is not None:
            return self._shared_strings_cache
        raw = self.open_stream("xl/sharedStrings.xml", max_bytes=_MAX_SHARED_STRINGS_BYTES)
        if not raw:
            self._shared_strings_cache = []
            return self._shared_strings_cache
        root = _parse_xml_bytes(raw)
        if root is None:
            self._shared_strings_cache = []
            return self._shared_strings_cache
        strings = [_extract_si_text(el) for el in root if local_name(el.tag) == "si"]
        self._shared_strings_cache = strings
        return strings

    def read_styles(self) -> XlsxStyleCatalog:
        """`xl/styles.xml`에서 숫자 서식·테두리·글꼴 카탈로그를 구축한다."""
        if self._styles_cache is not None:
            return self._styles_cache

        catalog = XlsxStyleCatalog(date1904=self._date1904)
        root = self.read_xml("xl/styles.xml")
        if root is None:
            self._styles_cache = catalog
            return catalog

        custom_num_fmts: dict[int, str] = {}
        fonts: list[tuple[bool, float | None]] = []
        borders_has_line: list[bool] = []

        for child in root:
            ctag = local_name(child.tag)
            if ctag == "numFmts":
                for nf in child:
                    if local_name(nf.tag) != "numFmt":
                        continue
                    fid_s = nf.attrib.get("numFmtId", "")
                    code = nf.attrib.get("formatCode", "")
                    if fid_s.isdigit():
                        custom_num_fmts[int(fid_s)] = code
            elif ctag == "fonts":
                for f_el in child:
                    if local_name(f_el.tag) != "font":
                        continue
                    is_bold = False
                    f_sz: float | None = None
                    for sub in f_el:
                        stag = local_name(sub.tag)
                        if stag == "b":
                            val = sub.attrib.get("val", "1").lower()
                            is_bold = val not in ("0", "false")
                        elif stag == "sz":
                            with contextlib.suppress(ValueError):
                                f_sz = float(sub.attrib.get("val", ""))
                    fonts.append((is_bold, f_sz))
            elif ctag == "borders":
                for b_el in child:
                    if local_name(b_el.tag) != "border":
                        continue
                    has_line = any(
                        local_name(sub.tag) in ("left", "right", "top", "bottom")
                        and sub.attrib.get("style", "") not in ("", "none")
                        for sub in b_el
                    )
                    borders_has_line.append(has_line)

        for child in root:
            if local_name(child.tag) != "cellXfs":
                continue
            for xf in child:
                if local_name(xf.tag) != "xf":
                    continue
                nf_id = int(xf.attrib.get("numFmtId", "0") or "0")
                font_id = int(xf.attrib.get("fontId", "0") or "0")
                border_id = int(xf.attrib.get("borderId", "0") or "0")
                is_bold, f_sz = fonts[font_id] if 0 <= font_id < len(fonts) else (False, None)
                has_border = (
                    borders_has_line[border_id] if 0 <= border_id < len(borders_has_line) else False
                )
                catalog.cell_xfs.append(
                    XlsxCellStyle(
                        num_fmt_id=nf_id,
                        fmt_code=custom_num_fmts.get(nf_id, ""),
                        has_border=has_border,
                        is_bold=is_bold,
                        font_size_pt=f_sz,
                    )
                )

        self._styles_cache = catalog
        return catalog

    def read_core_metadata(self) -> XlsxCoreMetadata:
        """`docProps/core.xml`과 `docProps/app.xml`에서 제목·작성자·생성기를 읽는다."""
        title: str | None = None
        author: str | None = None
        creator: str | None = None

        core = self.read_xml("docProps/core.xml")
        if core is not None:
            for el in core:
                tag = local_name(el.tag)
                txt = (el.text or "").strip()
                if not txt:
                    continue
                if tag == "title" and not title:
                    title = txt
                elif tag in ("creator", "lastModifiedBy") and not author:
                    author = txt

        app = self.read_xml("docProps/app.xml")
        if app is not None:
            for el in app:
                if local_name(el.tag) == "Application" and (el.text or "").strip():
                    creator = (el.text or "").strip()
                    break

        return XlsxCoreMetadata(title=title, author=author, creator=creator)


__all__ = [
    "XlsxCellStyle",
    "XlsxCoreMetadata",
    "XlsxPackage",
    "XlsxSheetRef",
    "XlsxStyleCatalog",
    "col_letters_to_index",
    "format_excel_number",
    "parse_cell_range",
    "parse_cell_ref",
]
