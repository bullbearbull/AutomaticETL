"""OOXML(.docx) 저수준 ZIP 패키지 리더, 스타일·번호매기기·각주/미주·주석·머리글/바닥글 카탈로그.

추가 서드파티 의존성(`python-docx`, `lxml`, `Pillow`) 없이 Python 표준 라이브러리
(`zipfile`, `xml.etree.ElementTree`, `posixpath`)만으로 안전하게 `.docx`
WordprocessingML 패키지를 읽는다.
"""

from __future__ import annotations

import contextlib
import hashlib
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.contracts import DocumentMetadata, SourceFormat
from autoetl.parser.pptx.ooxml import (
    EMU_PER_PT,
    MAX_MEDIA_BYTES,
    MAX_XML_BYTES,
    NS_A,
    NS_ASVG,
    NS_C,
    NS_CX,
    NS_DC,
    NS_DGM,
    NS_EP,
    NS_M,
    NS_MC,
    NS_OFFICE,
    NS_PKG_REL,
    NS_R,
    NS_SSML,
    NS_VML,
    OLE_MAGIC,
    Relationship,
    clamp,
    emu_to_pt,
    iter_children_resolving_mc,
    local_name,
    make_bbox,
    probe_image_dimensions,
    qn,
)

TWIPS_PER_PT = 20.0
DEFAULT_PAGE_WIDTH_PT = 595.3  # A4 (11906 twips)
DEFAULT_PAGE_HEIGHT_PT = 841.9  # A4 (16838 twips)
DEFAULT_MARGIN_PT = 72.0  # 1 inch (1440 twips)

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
NS_WPG = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
NS_WPC = "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"

REL_TYPE_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
REL_TYPE_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
REL_TYPE_FOOTNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
REL_TYPE_ENDNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
REL_TYPE_DOCX_COMMENTS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
)
REL_TYPE_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
REL_TYPE_NUMBERING = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"

_HEADING_PREFIXES = (
    "heading",
    "제목",
    "titre",
    "überschrift",
    "encabezado",
    "cabeçalho",
)

_CODE_STYLE_TOKENS = (
    "code",
    "source",
    "preformatted",
    "htmlpre",
    "macro",
    "verbatim",
    "console",
)

_MONOSPACE_FONTS = frozenset(
    {
        "consolas",
        "courier",
        "courier new",
        "d2coding",
        "fira code",
        "jetbrains mono",
        "lucida console",
        "menlo",
        "monaco",
        "nanumgothiccoding",
        "source code pro",
    }
)


_STRICT_NS_MAP: dict[str, str] = {
    "http://purl.oclc.org/ooxml/wordprocessingml/main": NS_W,
    "http://purl.oclc.org/ooxml/officeDocument/relationships": NS_R,
    "http://purl.oclc.org/ooxml/officeDocument/math": NS_M,
    "http://purl.oclc.org/ooxml/drawingml/main": NS_A,
    "http://purl.oclc.org/ooxml/drawingml/wordprocessingDrawing": NS_WP,
    "http://purl.oclc.org/ooxml/drawingml/picture": NS_PIC,
    "http://purl.oclc.org/ooxml/drawingml/chart": NS_C,
    "http://purl.oclc.org/ooxml/drawingml/diagram": NS_DGM,
    "http://purl.oclc.org/ooxml/officeDocument/extendedProperties": NS_EP,
    "http://purl.oclc.org/ooxml/spreadsheetml/main": NS_SSML,
}

_STRICT_REL_PREFIX = "http://purl.oclc.org/ooxml/officeDocument/relationships/"
_TRANSITIONAL_REL_PREFIX = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _normalize_strict_xml_tree(root: ET.Element) -> ET.Element:
    """ISO/IEC 29500 Strict OOXML 네임스페이스를 Transitional 네임스페이스로 정규화한다."""
    for el in root.iter():
        tag = el.tag
        if tag.startswith("{"):
            end_idx = tag.find("}")
            if end_idx > 1:
                ns = tag[1:end_idx]
                mapped = _STRICT_NS_MAP.get(ns)
                if mapped is not None:
                    el.tag = f"{{{mapped}}}{tag[end_idx + 1 :]}"
        if el.attrib:
            new_attrib: dict[str, str] = {}
            changed = False
            for k, v in el.attrib.items():
                if k.startswith("{"):
                    end_idx = k.find("}")
                    if end_idx > 1:
                        ns = k[1:end_idx]
                        mapped = _STRICT_NS_MAP.get(ns)
                        if mapped is not None:
                            new_attrib[f"{{{mapped}}}{k[end_idx + 1 :]}"] = v
                            changed = True
                            continue
                new_attrib[k] = v
            if changed:
                el.attrib = new_attrib
    return root


def twips_to_pt(value: float | int | str | None, default: float = 0.0) -> float:
    """WordprocessingML twip(1/20 pt) 또는 ST_UniversalMeasure(`pt`/`in` 등)를 pt로 변환한다."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value) / TWIPS_PER_PT
    s = str(value).strip().lower()
    if not s:
        return default
    unit_multipliers: tuple[tuple[str, float], ...] = (
        ("dxa", 1.0 / TWIPS_PER_PT),
        ("pt", 1.0),
        ("in", 72.0),
        ("cm", 72.0 / 2.54),
        ("mm", 72.0 / 25.4),
        ("pc", 12.0),
        ("pi", 12.0),
    )
    for suffix, mult in unit_multipliers:
        if s.endswith(suffix):
            num_part = s[: -len(suffix)].strip()
            try:
                return float(num_part) * mult
            except ValueError:
                return default
    try:
        return float(s) / TWIPS_PER_PT
    except ValueError:
        return default


def half_pt_to_pt(value: float | int | str | None) -> float | None:
    """WordprocessingML 반포인트(`w:sz`, 1/2 pt)를 포인트(pt)로 변환한다."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower().endswith("pt"):
        try:
            val = float(value.strip()[:-2])
            return round(val, 2) if val > 0.0 else None
        except ValueError:
            return None
    try:
        val = float(value) / 2.0
    except (TypeError, ValueError):
        return None
    return round(val, 2) if val > 0.0 else None


def _resolve_zip_path(source_part: str, target: str) -> str | None:
    cleaned = target.strip().replace("\\", "/")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        norm = posixpath.normpath(cleaned.lstrip("/"))
    else:
        base_dir = posixpath.dirname(source_part)
        norm = posixpath.normpath(posixpath.join(base_dir, cleaned))
    if norm.startswith("..") or norm == ".":
        return None
    return norm


def _rels_path_for(part_path: str) -> str:
    base_dir = posixpath.dirname(part_path)
    filename = posixpath.basename(part_path)
    if base_dir:
        return f"{base_dir}/_rels/{filename}.rels"
    return f"_rels/{filename}.rels"


def _parse_heading_level_from_label(label: str) -> int | None:
    cleaned = label.strip().lower()
    if not cleaned:
        return None
    if cleaned in ("title", "표제", "문서 제목"):
        return 1
    if cleaned in ("subtitle", "부제", "부제목"):
        return 2
    for prefix in _HEADING_PREFIXES:
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix) :].strip(" _-.")
            if rest.isdigit():
                lvl = int(rest)
                if 1 <= lvl <= 9:
                    return min(6, lvl)
    return None


@dataclass(frozen=True)
class ResolvedParagraphStyle:
    """스타일 상속 체인을 해석한 문단 스타일 속성."""

    style_id: str
    name: str
    heading_level: int | None = None
    font_size_pt: float | None = None
    is_bold: bool | None = None
    is_italic: bool | None = None
    is_code_style: bool = False
    is_list_style: bool = False
    num_id: str | None = None
    ilvl: int = 0


@dataclass
class _RawStyleDef:
    style_id: str
    name: str
    based_on: str | None = None
    outline_lvl: int | None = None
    font_size_pt: float | None = None
    is_bold: bool | None = None
    is_italic: bool | None = None
    font_family: str | None = None
    num_id: str | None = None
    ilvl: int | None = None


class StyleCatalog:
    """`word/styles.xml`의 문단·문자 스타일 상속(`basedOn`) 및 기본 글꼴 카탈로그."""

    def __init__(
        self,
        raw_styles: dict[str, _RawStyleDef],
        *,
        default_font_size_pt: float | None = None,
    ) -> None:
        self._raw = raw_styles
        self.default_font_size_pt = default_font_size_pt or 11.0
        self._cache: dict[str, ResolvedParagraphStyle] = {}

    @classmethod
    def from_xml(cls, root: ET.Element | None) -> StyleCatalog:
        if root is None:
            return cls({}, default_font_size_pt=11.0)

        default_font: float | None = None
        doc_defaults = root.find(qn(NS_W, "docDefaults"))
        if doc_defaults is not None:
            rpr_def = doc_defaults.find(qn(NS_W, "rPrDefault"))
            rpr = rpr_def.find(qn(NS_W, "rPr")) if rpr_def is not None else None
            default_font = _extract_rpr_font_size(rpr)

        raw_styles: dict[str, _RawStyleDef] = {}
        for st_el in root.findall(qn(NS_W, "style")):
            sid = (
                st_el.attrib.get(qn(NS_W, "styleId")) or st_el.attrib.get("styleId") or ""
            ).strip()
            if not sid:
                continue
            name_el = st_el.find(qn(NS_W, "name"))
            name = (
                (name_el.attrib.get(qn(NS_W, "val")) or name_el.attrib.get("val") or sid).strip()
                if name_el is not None
                else sid
            )
            based_el = st_el.find(qn(NS_W, "basedOn"))
            based_on = (
                (based_el.attrib.get(qn(NS_W, "val")) or based_el.attrib.get("val") or "").strip()
                if based_el is not None
                else None
            ) or None

            ppr = st_el.find(qn(NS_W, "pPr"))
            rpr = st_el.find(qn(NS_W, "rPr"))

            outline_lvl: int | None = None
            num_id: str | None = None
            ilvl: int | None = None
            if ppr is not None:
                out_el = ppr.find(qn(NS_W, "outlineLvl"))
                if out_el is not None:
                    raw_out = out_el.attrib.get(qn(NS_W, "val")) or out_el.attrib.get("val")
                    if raw_out is not None:
                        with contextlib.suppress(ValueError):
                            val = int(raw_out)
                            if 0 <= val <= 8:
                                outline_lvl = min(6, val + 1)
                num_pr = ppr.find(qn(NS_W, "numPr"))
                if num_pr is not None:
                    num_id_el = num_pr.find(qn(NS_W, "numId"))
                    ilvl_el = num_pr.find(qn(NS_W, "ilvl"))
                    if num_id_el is not None:
                        num_id = (
                            num_id_el.attrib.get(qn(NS_W, "val"))
                            or num_id_el.attrib.get("val")
                            or ""
                        ).strip() or None
                    if ilvl_el is not None:
                        raw_lvl = ilvl_el.attrib.get(qn(NS_W, "val")) or ilvl_el.attrib.get("val")
                        if raw_lvl is not None:
                            with contextlib.suppress(ValueError):
                                ilvl = max(0, int(raw_lvl))

            font_size = _extract_rpr_font_size(rpr)
            is_bold = _extract_rpr_toggle(rpr, "b")
            is_italic = _extract_rpr_toggle(rpr, "i")
            font_family = _extract_rpr_font_family(rpr)

            raw_styles[sid] = _RawStyleDef(
                style_id=sid,
                name=name,
                based_on=based_on,
                outline_lvl=outline_lvl,
                font_size_pt=font_size,
                is_bold=is_bold,
                is_italic=is_italic,
                font_family=font_family,
                num_id=num_id,
                ilvl=ilvl,
            )

        return cls(raw_styles, default_font_size_pt=default_font)

    def resolve(self, style_id: str | None) -> ResolvedParagraphStyle | None:
        if not style_id:
            return None
        if style_id in self._cache:
            return self._cache[style_id]
        if style_id not in self._raw:
            inferred_lvl = _parse_heading_level_from_label(style_id)
            low = style_id.lower()
            resolved = ResolvedParagraphStyle(
                style_id=style_id,
                name=style_id,
                heading_level=inferred_lvl,
                font_size_pt=self.default_font_size_pt,
                is_code_style=any(tok in low for tok in _CODE_STYLE_TOKENS),
                is_list_style=("list" in low or "목록" in low),
            )
            self._cache[style_id] = resolved
            return resolved

        chain: list[_RawStyleDef] = []
        visited: set[str] = set()
        cur_id: str | None = style_id
        while cur_id and cur_id in self._raw and cur_id not in visited:
            visited.add(cur_id)
            st = self._raw[cur_id]
            chain.append(st)
            cur_id = st.based_on

        top = chain[0]
        heading_lvl: int | None = None
        font_size: float | None = None
        is_bold: bool | None = None
        is_italic: bool | None = None
        font_family: str | None = None
        num_id: str | None = None
        ilvl: int | None = None
        is_code = False
        is_list = False

        for st in chain:
            if heading_lvl is None:
                heading_lvl = (
                    _parse_heading_level_from_label(st.name)
                    or _parse_heading_level_from_label(st.style_id)
                    or st.outline_lvl
                )
            if font_size is None and st.font_size_pt is not None:
                font_size = st.font_size_pt
            if is_bold is None and st.is_bold is not None:
                is_bold = st.is_bold
            if is_italic is None and st.is_italic is not None:
                is_italic = st.is_italic
            if font_family is None and st.font_family is not None:
                font_family = st.font_family
            if num_id is None and st.num_id is not None:
                num_id = st.num_id
            if ilvl is None and st.ilvl is not None:
                ilvl = st.ilvl
            combined_label = f"{st.style_id} {st.name}".lower().replace(" ", "")
            if any(tok in combined_label for tok in _CODE_STYLE_TOKENS):
                is_code = True
            if "list" in combined_label or "bullet" in combined_label or "목록" in combined_label:
                is_list = True

        if font_family and font_family.lower() in _MONOSPACE_FONTS:
            is_code = True

        resolved = ResolvedParagraphStyle(
            style_id=top.style_id,
            name=top.name,
            heading_level=heading_lvl,
            font_size_pt=font_size or self.default_font_size_pt,
            is_bold=is_bold,
            is_italic=is_italic,
            is_code_style=is_code,
            is_list_style=is_list,
            num_id=num_id,
            ilvl=ilvl or 0,
        )
        self._cache[style_id] = resolved
        return resolved


@dataclass(frozen=True)
class ListLevelFormat:
    """`word/numbering.xml`의 단일 번호/불릿 레벨 정의."""

    num_fmt: str  # "bullet", "decimal", "lowerLetter", "upperRoman", "none", ...
    lvl_text: str = ""
    start: int = 1

    @property
    def is_ordered(self) -> bool:
        return self.num_fmt not in ("bullet", "none", "")


class NumberingCatalog:
    """`word/numbering.xml`의 `(numId, ilvl)` → 불릿/번호 형식 해석 카탈로그."""

    def __init__(self, num_map: dict[tuple[str, int], ListLevelFormat]) -> None:
        self._map = num_map

    @classmethod
    def from_xml(cls, root: ET.Element | None) -> NumberingCatalog:
        if root is None:
            return cls({})

        abstract_levels: dict[tuple[str, int], ListLevelFormat] = {}
        for abs_el in root.findall(qn(NS_W, "abstractNum")):
            abs_id = (
                abs_el.attrib.get(qn(NS_W, "abstractNumId"))
                or abs_el.attrib.get("abstractNumId")
                or ""
            ).strip()
            if not abs_id:
                continue
            for lvl_el in abs_el.findall(qn(NS_W, "lvl")):
                lvl_fmt = _parse_lvl_format(lvl_el)
                if lvl_fmt is not None:
                    ilvl, fmt_obj = lvl_fmt
                    abstract_levels[(abs_id, ilvl)] = fmt_obj

        num_map: dict[tuple[str, int], ListLevelFormat] = {}
        for num_el in root.findall(qn(NS_W, "num")):
            num_id = (
                num_el.attrib.get(qn(NS_W, "numId")) or num_el.attrib.get("numId") or ""
            ).strip()
            if not num_id:
                continue
            abs_ref = num_el.find(qn(NS_W, "abstractNumId"))
            abs_id = (
                (abs_ref.attrib.get(qn(NS_W, "val")) or abs_ref.attrib.get("val") or "").strip()
                if abs_ref is not None
                else ""
            )
            if abs_id:
                for (a_id, ilvl), fmt_obj in abstract_levels.items():
                    if a_id == abs_id:
                        num_map[(num_id, ilvl)] = fmt_obj
            for ovr_el in num_el.findall(qn(NS_W, "lvlOverride")):
                raw_ovr_ilvl = (
                    ovr_el.attrib.get(qn(NS_W, "ilvl")) or ovr_el.attrib.get("ilvl") or "0"
                )
                try:
                    ovr_ilvl = max(0, int(raw_ovr_ilvl))
                except ValueError:
                    ovr_ilvl = 0
                ovr_lvl = ovr_el.find(qn(NS_W, "lvl"))
                if ovr_lvl is not None:
                    lvl_fmt = _parse_lvl_format(ovr_lvl)
                    if lvl_fmt is not None:
                        ilvl, fmt_obj = lvl_fmt
                        num_map[(num_id, ilvl)] = fmt_obj
                        ovr_ilvl = ilvl
                start_ovr = ovr_el.find(qn(NS_W, "startOverride"))
                if start_ovr is not None:
                    raw_so = start_ovr.attrib.get(qn(NS_W, "val")) or start_ovr.attrib.get("val")
                    if raw_so is not None:
                        with contextlib.suppress(ValueError):
                            so_val = int(raw_so)
                            base_fmt = num_map.get((num_id, ovr_ilvl)) or ListLevelFormat(
                                num_fmt="decimal",
                                lvl_text=f"%{ovr_ilvl + 1}.",
                                start=so_val,
                            )
                            num_map[(num_id, ovr_ilvl)] = ListLevelFormat(
                                num_fmt=base_fmt.num_fmt,
                                lvl_text=base_fmt.lvl_text,
                                start=so_val,
                            )

        return cls(num_map)

    def resolve(self, num_id: str | None, ilvl: int) -> ListLevelFormat | None:
        if not num_id or num_id == "0":
            return None
        return self._map.get((num_id, ilvl)) or self._map.get((num_id, 0))


def _parse_lvl_format(lvl_el: ET.Element) -> tuple[int, ListLevelFormat] | None:
    raw_ilvl = lvl_el.attrib.get(qn(NS_W, "ilvl")) or lvl_el.attrib.get("ilvl") or "0"
    try:
        ilvl = max(0, int(raw_ilvl))
    except ValueError:
        ilvl = 0
    fmt_el = lvl_el.find(qn(NS_W, "numFmt"))
    num_fmt = (
        (fmt_el.attrib.get(qn(NS_W, "val")) or fmt_el.attrib.get("val") or "bullet").strip()
        if fmt_el is not None
        else "bullet"
    )
    txt_el = lvl_el.find(qn(NS_W, "lvlText"))
    lvl_text = (
        (txt_el.attrib.get(qn(NS_W, "val")) or txt_el.attrib.get("val") or "").strip()
        if txt_el is not None
        else ""
    )
    start_el = lvl_el.find(qn(NS_W, "start"))
    start_val = 1
    if start_el is not None:
        raw_start = start_el.attrib.get(qn(NS_W, "val")) or start_el.attrib.get("val")
        if raw_start is not None:
            with contextlib.suppress(ValueError):
                start_val = int(raw_start)
    return ilvl, ListLevelFormat(num_fmt=num_fmt, lvl_text=lvl_text, start=start_val)


def _extract_rpr_font_size(rpr: ET.Element | None) -> float | None:
    if rpr is None:
        return None
    for tag_name in ("sz", "szCs"):
        sz_el = rpr.find(qn(NS_W, tag_name))
        if sz_el is not None:
            raw = sz_el.attrib.get(qn(NS_W, "val")) or sz_el.attrib.get("val")
            pt = half_pt_to_pt(raw)
            if pt is not None:
                return pt
    return None


def _extract_rpr_toggle(rpr: ET.Element | None, tag_name: str) -> bool | None:
    if rpr is None:
        return None
    el = rpr.find(qn(NS_W, tag_name))
    if el is None:
        return None
    val = (el.attrib.get(qn(NS_W, "val")) or el.attrib.get("val") or "1").strip().lower()
    return val not in ("0", "false", "off", "none")


def _extract_rpr_font_family(rpr: ET.Element | None) -> str | None:
    if rpr is None:
        return None
    fonts_el = rpr.find(qn(NS_W, "rFonts"))
    if fonts_el is None:
        return None
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        val = fonts_el.attrib.get(qn(NS_W, attr)) or fonts_el.attrib.get(attr)
        if val and val.strip():
            return val.strip()
    return None


@dataclass(frozen=True)
class DocxCommentEntry:
    """`word/comments.xml`의 단일 주석 항목."""

    comment_id: str
    author: str
    initials: str
    date: str
    text: str


@dataclass
class DocxPackage:
    """`.docx` ZIP 아카이브를 안전하게 열고 본문·관계·스타일·각주·주석·미디어를 조회한다."""

    source_path: Path
    main_document_part: str = field(init=False, default="word/document.xml")
    _file_size: int = field(init=False)
    _sha256: str = field(init=False)
    _zf: zipfile.ZipFile = field(init=False)
    _names: set[str] = field(init=False)
    _rels_cache: dict[str, dict[str, Relationship]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_path.exists():
            raise ParserInputError(f"DOCX 파일이 존재하지 않는다: {self.source_path.name}")
        if not self.source_path.is_file():
            raise ParserInputError(f"DOCX 입력 경로가 파일이 아니다: {self.source_path.name}")

        try:
            with self.source_path.open("rb") as fh:
                raw_head = fh.read(8)
        except OSError as exc:
            raise ParserInputError(f"DOCX 파일을 읽을 수 없다: {self.source_path.name}") from exc

        if raw_head.startswith(OLE_MAGIC):
            raise ParserInputError(
                "암호화된 DOCX이거나 구형 바이너리 DOC(OLE) 형식이다 — "
                "암호를 해제하거나 최신 OOXML(.docx)로 저장한 뒤 다시 시도하라."
            )

        self._file_size = self.source_path.stat().st_size
        self._sha256 = self._compute_sha256(self.source_path)

        try:
            self._zf = zipfile.ZipFile(self.source_path, "r")
        except zipfile.BadZipFile as exc:
            raise ParserInputError(
                f"유효한 DOCX(OOXML ZIP) 파일이 아니다: {self.source_path.name}"
            ) from exc
        except OSError as exc:
            raise ParserInputError(f"DOCX 아카이브를 열 수 없다: {self.source_path.name}") from exc

        self._names = set(self._zf.namelist())
        resolved_main = self._discover_main_document_part()
        if resolved_main is None:
            self.close()
            raise ParserInputError(
                f"DOCX 필수 파트(word/document.xml)가 없다: {self.source_path.name}"
            )
        self.main_document_part = resolved_main

    def _discover_main_document_part(self) -> str | None:
        """`_rels/.rels`의 `officeDocument` 관계 또는 기본 `word/document.xml` 경로를 찾는다."""
        if "_rels/.rels" in self._names:
            root = self.read_xml("_rels/.rels")
            if root is not None:
                for rel_el in root.findall(qn(NS_PKG_REL, "Relationship")):
                    rel_type = rel_el.attrib.get("Type", "")
                    raw_target = rel_el.attrib.get("Target", "")
                    if rel_type.endswith("/officeDocument") and raw_target:
                        cand = _resolve_zip_path("", raw_target)
                        if cand and cand in self._names:
                            return cand
        if "word/document.xml" in self._names:
            return "word/document.xml"
        return None

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> DocxPackage:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def has_part(self, part_path: str) -> bool:
        return part_path in self._names

    def read_bytes(self, part_path: str, *, max_bytes: int = MAX_MEDIA_BYTES) -> bytes | None:
        if part_path not in self._names:
            return None
        info = self._zf.getinfo(part_path)
        if info.file_size > max_bytes:
            return None
        try:
            data = self._zf.read(info)
        except (zipfile.BadZipFile, RuntimeError, OSError):
            return None
        if len(data) > max_bytes:
            return None
        return data

    def read_xml(self, part_path: str) -> ET.Element | None:
        data = self.read_bytes(part_path, max_bytes=MAX_XML_BYTES)
        if data is None:
            return None
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return None
        if b"http://purl.oclc.org/ooxml/" in data:
            return _normalize_strict_xml_tree(root)
        return root

    def read_rels(self, part_path: str) -> dict[str, Relationship]:
        if part_path in self._rels_cache:
            return self._rels_cache[part_path]
        rels_path = _rels_path_for(part_path)
        root = self.read_xml(rels_path)
        rels: dict[str, Relationship] = {}
        if root is not None:
            for rel_el in root.findall(qn(NS_PKG_REL, "Relationship")):
                r_id = rel_el.attrib.get("Id", "")
                rel_type = rel_el.attrib.get("Type", "")
                if rel_type.startswith(_STRICT_REL_PREFIX):
                    rel_type = _TRANSITIONAL_REL_PREFIX + rel_type[len(_STRICT_REL_PREFIX) :]
                raw_target = rel_el.attrib.get("Target", "")
                is_ext = rel_el.attrib.get("TargetMode", "").lower() == "external"
                if not r_id or not raw_target:
                    continue
                if is_ext:
                    target = raw_target
                else:
                    resolved = _resolve_zip_path(part_path, raw_target)
                    if resolved is None:
                        continue
                    target = resolved
                rels[r_id] = Relationship(
                    r_id=r_id,
                    rel_type=rel_type,
                    target=target,
                    is_external=is_ext,
                )
        self._rels_cache[part_path] = rels
        return rels

    def read_metadata(self, *, page_count: int) -> DocumentMetadata:
        """`docProps/core.xml` 및 `docProps/app.xml`에서 문서 메타데이터를 추출한다."""
        title: str | None = None
        author: str | None = None
        creator_app: str | None = None

        core_root = self.read_xml("docProps/core.xml")
        if core_root is not None:
            title_el = core_root.find(qn(NS_DC, "title"))
            if title_el is not None and title_el.text and title_el.text.strip():
                title = title_el.text.strip()
            author_el = core_root.find(qn(NS_DC, "creator"))
            if author_el is not None and author_el.text and author_el.text.strip():
                author = author_el.text.strip()

        app_root = self.read_xml("docProps/app.xml")
        if app_root is not None:
            app_el = app_root.find(qn(NS_EP, "Application"))
            if app_el is not None and app_el.text and app_el.text.strip():
                creator_app = app_el.text.strip()

        return DocumentMetadata(
            source_path=self.source_path.name,
            format=SourceFormat.DOCX,
            file_size_bytes=self._file_size,
            sha256=self._sha256,
            page_count=max(1, page_count),
            title=title,
            author=author,
            creator=creator_app,
            producer=creator_app,
            has_outline=False,
            is_encrypted=False,
        )

    def read_styles(self) -> StyleCatalog:
        doc_rels = self.read_rels(self.main_document_part)
        part = next(
            (
                r.target
                for r in doc_rels.values()
                if not r.is_external and r.rel_type == REL_TYPE_STYLES
            ),
            "word/styles.xml",
        )
        return StyleCatalog.from_xml(self.read_xml(part))

    def read_numbering(self) -> NumberingCatalog:
        doc_rels = self.read_rels(self.main_document_part)
        part = next(
            (
                r.target
                for r in doc_rels.values()
                if not r.is_external and r.rel_type == REL_TYPE_NUMBERING
            ),
            "word/numbering.xml",
        )
        return NumberingCatalog.from_xml(self.read_xml(part))


__all__ = [
    "DEFAULT_MARGIN_PT",
    "DEFAULT_PAGE_HEIGHT_PT",
    "DEFAULT_PAGE_WIDTH_PT",
    "EMU_PER_PT",
    "MAX_MEDIA_BYTES",
    "MAX_XML_BYTES",
    "NS_A",
    "NS_ASVG",
    "NS_C",
    "NS_CX",
    "NS_DGM",
    "NS_M",
    "NS_MC",
    "NS_OFFICE",
    "NS_PIC",
    "NS_R",
    "NS_SSML",
    "NS_VML",
    "NS_W",
    "NS_WP",
    "NS_WPC",
    "NS_WPG",
    "NS_WPS",
    "OLE_MAGIC",
    "REL_TYPE_DOCX_COMMENTS",
    "REL_TYPE_ENDNOTES",
    "REL_TYPE_FOOTER",
    "REL_TYPE_FOOTNOTES",
    "REL_TYPE_HEADER",
    "TWIPS_PER_PT",
    "DocxCommentEntry",
    "DocxPackage",
    "ListLevelFormat",
    "NumberingCatalog",
    "Relationship",
    "ResolvedParagraphStyle",
    "StyleCatalog",
    "clamp",
    "emu_to_pt",
    "half_pt_to_pt",
    "iter_children_resolving_mc",
    "local_name",
    "make_bbox",
    "probe_image_dimensions",
    "qn",
    "twips_to_pt",
]
