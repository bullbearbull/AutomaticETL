"""KS X 6101 OWPML(`.hwpx`) 저수준 ZIP 패키지 리더 및 `header.xml` 스타일·번호·테두리 카탈로그.

추가 서드파티 의존성 없이 Python 표준 라이브러리(`zipfile`, `xml.etree.ElementTree`,
`posixpath`)만으로 한컴오피스/표준 `.hwpx` 패키지의 `META-INF/container.xml`,
`Contents/content.hpf`, `Contents/header.xml`, `Contents/section*.xml`, `BinData/*`를
안전하게 파싱한다.
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
from autoetl.parser.contracts import DocumentMetadata, ParseStrategy, SourceFormat
from autoetl.parser.pptx.ooxml import (
    MAX_MEDIA_BYTES,
    MAX_XML_BYTES,
    OLE_MAGIC,
    Relationship,
    clamp,
    local_name,
    make_bbox,
    probe_image_dimensions,
)

# 1 pt = 100 HWPUNIT (1 inch = 72 pt = 7200 HWPUNIT)
HWPUNIT_PER_PT = 100.0
DEFAULT_PAGE_WIDTH_PT = 595.28  # A4 (59528 HWPUNIT)
DEFAULT_PAGE_HEIGHT_PT = 841.88  # A4 (84188 HWPUNIT)
DEFAULT_MARGIN_LEFT_PT = 85.04  # 30mm (8504 HWPUNIT)
DEFAULT_MARGIN_RIGHT_PT = 85.04
DEFAULT_MARGIN_TOP_PT = 56.68  # 20mm (5668 HWPUNIT)
DEFAULT_MARGIN_BOTTOM_PT = 42.52  # 15mm (4252 HWPUNIT)

_HEADING_STYLE_PREFIXES = (
    "개요",
    "제목",
    "차례",
    "outline",
    "heading",
    "title",
    "toc",
    "부제",
    "subtitle",
)

_CODE_STYLE_TOKENS = (
    "code",
    "코드",
    "source",
    "소스",
    "verbatim",
    "console",
    "preformatted",
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
        "나눔고딕코딩",
        "source code pro",
    }
)


def hwpunit_to_pt(value: float | int | str | None, default: float = 0.0) -> float:
    """OWPML HWPUNIT(1/100 pt)을 포인트(pt)로 변환한다."""
    if value is None:
        return default
    try:
        return round(float(value) / HWPUNIT_PER_PT, 2)
    except (TypeError, ValueError):
        return default


def _resolve_zip_path(base_part: str, target: str) -> str | None:
    cleaned = target.strip().replace("\\", "/")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        norm = posixpath.normpath(cleaned.lstrip("/"))
    else:
        base_dir = posixpath.dirname(base_part)
        norm = posixpath.normpath(posixpath.join(base_dir, cleaned))
    if norm.startswith("..") or norm == ".":
        return None
    return norm


def iter_hwpx_children(elem: ET.Element) -> list[ET.Element]:
    """`<hp:switch>` 요소를 만나면 첫 유효 `<hp:case>` 또는 `<hp:default>`만 선택한다."""
    out: list[ET.Element] = []
    for ch in elem:
        if local_name(ch.tag) == "switch":
            chosen: ET.Element | None = None
            default_el: ET.Element | None = None
            for sub in ch:
                stag = local_name(sub.tag)
                if stag == "case" and len(sub) > 0:
                    chosen = sub
                    break
                if stag == "default":
                    default_el = sub
            target = chosen if chosen is not None else default_el
            if target is not None:
                out.extend(iter_hwpx_children(target))
        else:
            out.append(ch)
    return out


def _get_attr_local(elem: ET.Element, attr_name: str, default: str = "") -> str:
    """네임스페이스 접두사 유무와 관계없이 속성 값을 읽는다."""
    if attr_name in elem.attrib:
        return elem.attrib[attr_name]
    for k, v in elem.attrib.items():
        if local_name(k) == attr_name:
            return v
    return default


def _parse_style_heading_level(name: str, eng_name: str) -> int | None:
    for raw in (name, eng_name):
        cleaned = raw.strip().lower()
        if not cleaned:
            continue
        if cleaned in ("제목", "표제", "title", "문서 제목", "차례 제목", "toc heading"):
            return 1
        if cleaned in ("부제", "부제목", "subtitle"):
            return 2
        for prefix in _HEADING_STYLE_PREFIXES:
            if cleaned.startswith(prefix):
                rest = cleaned[len(prefix) :].strip(" _-.")
                if rest.isdigit():
                    lvl = int(rest)
                    if 1 <= lvl <= 9:
                        if prefix in ("차례", "toc"):
                            return min(6, lvl + 1)
                        return min(6, lvl)
    return None


@dataclass(frozen=True)
class HwpxCharProp:
    """`<hh:charPr>` 문자 모양 정의."""

    char_pr_id: str
    font_size_pt: float
    is_bold: bool = False
    is_italic: bool = False
    is_monospace: bool = False


@dataclass(frozen=True)
class HwpxParaProp:
    """`<hh:paraPr>` 문단 모양 정의."""

    para_pr_id: str
    heading_type: str = "NONE"  # NONE, OUTLINE, NUMBER, BULLET
    heading_id_ref: str = "0"
    heading_level: int = 0  # 0-based level


@dataclass(frozen=True)
class HwpxStyleDef:
    """`<hh:style>` 스타일 정의."""

    style_id: str
    name: str
    eng_name: str
    para_pr_id: str
    char_pr_id: str
    heading_level: int | None = None
    is_code_style: bool = False


@dataclass(frozen=True)
class HwpxNumberingLevel:
    """`<hh:numbering>` 내부 `<hh:paraHead>` 단계별 번호 서식."""

    level: int  # 1-based
    num_format: str = "DIGIT"  # DIGIT, LATIN_SMALL, LATIN_CAPITAL, ROMAN_SMALL, HANGUL_SYLLABLE 등
    start: int = 1
    template: str = ""


@dataclass(frozen=True)
class HwpxBorderSpec:
    """`<hh:borderFill>` 테두리 선 유무 요약."""

    border_fill_id: str
    has_top: bool = False
    has_bottom: bool = False
    has_left: bool = False
    has_right: bool = False


class HwpxHeaderCatalog:
    """`Contents/header.xml`에서 추출한 글꼴·글자모양·문단모양·스타일·번호·테두리 카탈로그."""

    def __init__(
        self,
        *,
        char_props: dict[str, HwpxCharProp] | None = None,
        para_props: dict[str, HwpxParaProp] | None = None,
        styles: dict[str, HwpxStyleDef] | None = None,
        numberings: dict[tuple[str, int], HwpxNumberingLevel] | None = None,
        bullets: dict[str, str] | None = None,
        border_fills: dict[str, HwpxBorderSpec] | None = None,
        default_font_size_pt: float = 10.0,
    ) -> None:
        self.char_props = char_props or {}
        self.para_props = para_props or {}
        self.styles = styles or {}
        self.numberings = numberings or {}
        self.bullets = bullets or {}
        self.border_fills = border_fills or {}
        self.default_font_size_pt = default_font_size_pt

    @classmethod
    def from_xml(cls, root: ET.Element | None) -> HwpxHeaderCatalog:
        if root is None:
            return cls()

        # 1. 글꼴 목록(<hh:fontfaces>) -> (lang, font_id) -> is_monospace
        mono_font_ids: set[tuple[str, str]] = set()
        for fontface in root.iter():
            if local_name(fontface.tag) == "fontface":
                lang = _get_attr_local(fontface, "lang", "HANGUL").upper()
                for font_el in fontface:
                    if local_name(font_el.tag) == "font":
                        fid = _get_attr_local(font_el, "id")
                        face = _get_attr_local(font_el, "face").strip().lower()
                        if fid and face in _MONOSPACE_FONTS:
                            mono_font_ids.add((lang, fid))

        # 2. 문자 모양(<hh:charProperties>/<hh:charPr>)
        char_props: dict[str, HwpxCharProp] = {}
        font_sizes: list[float] = []
        for el in root.iter():
            if local_name(el.tag) != "charPr":
                continue
            cid = _get_attr_local(el, "id")
            if not cid:
                continue
            raw_h = _get_attr_local(el, "height", "1000")
            sz_pt = hwpunit_to_pt(raw_h, 10.0)
            if sz_pt <= 0.0:
                sz_pt = 10.0
            font_sizes.append(sz_pt)

            is_bold = _get_attr_local(el, "bold", "0").lower() not in ("0", "false", "")
            is_italic = _get_attr_local(el, "italic", "0").lower() not in ("0", "false", "")
            is_mono = False

            for ch in iter_hwpx_children(el):
                ctag = local_name(ch.tag)
                if ctag == "bold":
                    val = _get_attr_local(ch, "val", "1").lower()
                    is_bold = val not in ("0", "false", "off")
                elif ctag == "italic":
                    val = _get_attr_local(ch, "val", "1").lower()
                    is_italic = val not in ("0", "false", "off")
                elif ctag == "fontRef":
                    h_ref = _get_attr_local(ch, "hangul")
                    l_ref = _get_attr_local(ch, "latin")
                    if ("HANGUL", h_ref) in mono_font_ids or ("LATIN", l_ref) in mono_font_ids:
                        is_mono = True

            char_props[cid] = HwpxCharProp(
                char_pr_id=cid,
                font_size_pt=sz_pt,
                is_bold=is_bold,
                is_italic=is_italic,
                is_monospace=is_mono,
            )

        # 3. 문단 모양(<hh:paraProperties>/<hh:paraPr>)
        para_props: dict[str, HwpxParaProp] = {}
        for el in root.iter():
            if local_name(el.tag) != "paraPr":
                continue
            pid = _get_attr_local(el, "id")
            if not pid:
                continue
            h_type = "NONE"
            h_id_ref = "0"
            h_level = 0
            for ch in iter_hwpx_children(el):
                if local_name(ch.tag) == "heading":
                    h_type = _get_attr_local(ch, "type", "NONE").upper()
                    h_id_ref = _get_attr_local(ch, "idRef", "0")
                    with contextlib.suppress(ValueError):
                        h_level = max(0, int(_get_attr_local(ch, "level", "0")))
                    break
            para_props[pid] = HwpxParaProp(
                para_pr_id=pid,
                heading_type=h_type,
                heading_id_ref=h_id_ref,
                heading_level=h_level,
            )

        # 4. 스타일(<hh:styles>/<hh:style>)
        styles: dict[str, HwpxStyleDef] = {}
        for el in root.iter():
            if local_name(el.tag) != "style":
                continue
            sid = _get_attr_local(el, "id")
            if not sid:
                continue
            name = _get_attr_local(el, "name")
            eng_name = _get_attr_local(el, "engName")
            p_ref = _get_attr_local(el, "paraPrIDRef", "0")
            c_ref = _get_attr_local(el, "charPrIDRef", "0")

            h_lvl = _parse_style_heading_level(name, eng_name)
            p_prop = para_props.get(p_ref)
            if h_lvl is None and p_prop is not None and p_prop.heading_type == "OUTLINE":
                h_lvl = min(6, p_prop.heading_level + 1)

            combined_name = f"{name} {eng_name}".lower()
            c_prop = char_props.get(c_ref)
            is_code = any(tok in combined_name for tok in _CODE_STYLE_TOKENS) or bool(
                c_prop is not None and c_prop.is_monospace
            )
            styles[sid] = HwpxStyleDef(
                style_id=sid,
                name=name,
                eng_name=eng_name,
                para_pr_id=p_ref,
                char_pr_id=c_ref,
                heading_level=h_lvl,
                is_code_style=is_code,
            )

        # 5. 번호 매기기(<hh:numberings>/<hh:numbering>) 및 글머리표(<hh:bullets>/<hh:bullet>)
        numberings: dict[tuple[str, int], HwpxNumberingLevel] = {}
        for el in root.iter():
            if local_name(el.tag) != "numbering":
                continue
            nid = _get_attr_local(el, "id")
            if not nid:
                continue
            base_start = 1
            with contextlib.suppress(ValueError):
                base_start = max(1, int(_get_attr_local(el, "start", "1")))
            for ch in iter_hwpx_children(el):
                if local_name(ch.tag) == "paraHead":
                    lvl = 1
                    with contextlib.suppress(ValueError):
                        lvl = max(1, int(_get_attr_local(ch, "level", "1")))
                    num_fmt = _get_attr_local(ch, "numFormat", "DIGIT").upper()
                    lvl_start = base_start
                    raw_s = _get_attr_local(ch, "start")
                    if raw_s:
                        with contextlib.suppress(ValueError):
                            lvl_start = max(1, int(raw_s))
                    tmpl = (ch.text or "").strip()
                    numberings[(nid, lvl)] = HwpxNumberingLevel(
                        level=lvl,
                        num_format=num_fmt,
                        start=lvl_start,
                        template=tmpl,
                    )

        bullets: dict[str, str] = {}
        for el in root.iter():
            if local_name(el.tag) == "bullet":
                bid = _get_attr_local(el, "id")
                bchar = _get_attr_local(el, "char", "•").strip() or "•"
                if bid:
                    bullets[bid] = bchar

        # 6. 테두리·배경(<hh:borderFills>/<hh:borderFill>)
        border_fills: dict[str, HwpxBorderSpec] = {}
        for el in root.iter():
            if local_name(el.tag) != "borderFill":
                continue
            bf_id = _get_attr_local(el, "id")
            if not bf_id:
                continue
            has_t = False
            has_b = False
            has_l = False
            has_r = False
            for ch in iter_hwpx_children(el):
                ctag = local_name(ch.tag)
                b_type = _get_attr_local(ch, "type", "NONE").upper()
                active = b_type not in ("NONE", "NULL", "0", "")
                if ctag == "topBorder":
                    has_t = active
                elif ctag == "bottomBorder":
                    has_b = active
                elif ctag == "leftBorder":
                    has_l = active
                elif ctag == "rightBorder":
                    has_r = active
            border_fills[bf_id] = HwpxBorderSpec(
                border_fill_id=bf_id,
                has_top=has_t,
                has_bottom=has_b,
                has_left=has_l,
                has_right=has_r,
            )

        normal_style = styles.get("0")
        default_font = 10.0
        if normal_style and normal_style.char_pr_id in char_props:
            default_font = char_props[normal_style.char_pr_id].font_size_pt
        elif "0" in char_props:
            default_font = char_props["0"].font_size_pt

        return cls(
            char_props=char_props,
            para_props=para_props,
            styles=styles,
            numberings=numberings,
            bullets=bullets,
            border_fills=border_fills,
            default_font_size_pt=default_font,
        )

    def resolve_table_border_strategy(self, border_fill_ids: list[str]) -> ParseStrategy:
        """표 셀들의 `borderFillIDRef` 분포로부터 격자선/가로선(음영)/무선 표 전략을 판정한다."""
        specs = [self.border_fills[bid] for bid in border_fill_ids if bid in self.border_fills]
        if not specs:
            return ParseStrategy.RULED_TABLE
        n = len(specs)
        h_cnt = sum(1 for s in specs if s.has_top or s.has_bottom)
        v_cnt = sum(1 for s in specs if s.has_left or s.has_right)
        if h_cnt == 0 and v_cnt == 0:
            return ParseStrategy.BORDERLESS_TABLE
        if h_cnt >= max(1, int(n * 0.4)) and v_cnt <= int(n * 0.2):
            return ParseStrategy.HORIZONTAL_RULED_TABLE
        return ParseStrategy.RULED_TABLE


@dataclass
class HwpxPackage:
    """`.hwpx` OWPML ZIP 패키지를 안전하게 열고 메타데이터·헤더·섹션·바이너리를 제공한다."""

    source_path: Path
    content_hpf_part: str = field(init=False, default="Contents/content.hpf")
    header_part: str = field(init=False, default="Contents/header.xml")
    section_parts: tuple[str, ...] = field(init=False, default=())
    manifest_by_id: dict[str, str] = field(init=False, default_factory=dict)
    _file_size: int = field(init=False)
    _sha256: str = field(init=False)
    _zf: zipfile.ZipFile = field(init=False)
    _names: set[str] = field(init=False)
    _lower_name_map: dict[str, str] = field(init=False, default_factory=dict)
    _title: str | None = field(init=False, default=None)
    _author: str | None = field(init=False, default=None)
    _creator_app: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if not self.source_path.exists():
            raise ParserInputError(f"HWPX 파일이 존재하지 않는다: {self.source_path.name}")
        if not self.source_path.is_file():
            raise ParserInputError(f"HWPX 입력 경로가 파일이 아니다: {self.source_path.name}")

        try:
            with self.source_path.open("rb") as fh:
                raw_head = fh.read(8)
        except OSError as exc:
            raise ParserInputError(f"HWPX 파일을 읽을 수 없다: {self.source_path.name}") from exc

        if raw_head.startswith(OLE_MAGIC):
            raise ParserInputError(
                "암호화된 문서이거나 구형 바이너리 한글(.hwp, OLE2) 형식이다 — "
                "최신 개방형 한글 표준(.hwpx)으로 저장한 뒤 다시 시도하라."
            )

        self._file_size = self.source_path.stat().st_size
        self._sha256 = self._compute_sha256(self.source_path)

        try:
            self._zf = zipfile.ZipFile(self.source_path, "r")
        except zipfile.BadZipFile as exc:
            raise ParserInputError(
                f"유효한 HWPX(OWPML ZIP) 파일이 아니다: {self.source_path.name}"
            ) from exc
        except OSError as exc:
            raise ParserInputError(f"HWPX 아카이브를 열 수 없다: {self.source_path.name}") from exc

        self._names = set(self._zf.namelist())
        self._lower_name_map = {n.lower(): n for n in self._names}

        if self._is_encrypted_container():
            self.close()
            raise ParserInputError(
                f"암호화되거나 배포용으로 잠긴 HWPX 문서다: {self.source_path.name}"
            )

        self._discover_package_structure()
        if not self.section_parts:
            self.close()
            raise ParserInputError(
                f"HWPX 본문 섹션(Contents/section*.xml)을 찾을 수 없다: {self.source_path.name}"
            )

    def _is_encrypted_container(self) -> bool:
        for cand in ("META-INF/manifest.xml", "META-INF/container.xml"):
            actual = self._resolve_existing_part(cand)
            if actual is None:
                continue
            raw = self.read_bytes(actual, max_bytes=MAX_XML_BYTES)
            if raw and (b"encryption-data" in raw or b"algorithm-name" in raw):
                return True
        return False

    def _resolve_existing_part(self, candidate: str) -> str | None:
        cleaned = candidate.strip().lstrip("/").replace("\\", "/")
        if not cleaned:
            return None
        if cleaned in self._names:
            return cleaned
        return self._lower_name_map.get(cleaned.lower())

    def _discover_package_structure(self) -> None:
        # 1. META-INF/container.xml에서 content.hpf 경로 탐색
        container_part = self._resolve_existing_part("META-INF/container.xml")
        if container_part is not None:
            c_root = self.read_xml(container_part)
            if c_root is not None:
                for el in c_root.iter():
                    if local_name(el.tag) == "rootfile":
                        full_path = _get_attr_local(el, "full-path")
                        media_type = _get_attr_local(el, "media-type").lower()
                        if full_path and (
                            full_path.lower().endswith(".hpf") or "hwpml-package" in media_type
                        ):
                            resolved = self._resolve_existing_part(full_path)
                            if resolved:
                                self.content_hpf_part = resolved
                                break

        hpf_actual = self._resolve_existing_part(self.content_hpf_part)
        spine_idrefs: list[str] = []
        last_save_by: str | None = None
        if hpf_actual is not None:
            self.content_hpf_part = hpf_actual
            hpf_root = self.read_xml(hpf_actual)
            if hpf_root is not None:
                for el in hpf_root.iter():
                    tag = local_name(el.tag)
                    if tag == "title" and el.text and el.text.strip():
                        self._title = el.text.strip()
                    elif tag == "meta":
                        m_name = _get_attr_local(el, "name").lower()
                        raw_text = (el.text or "").strip()
                        raw_attr = _get_attr_local(el, "content").strip()
                        m_content = (
                            raw_text
                            if raw_text
                            else (raw_attr if raw_attr.lower() != "text" else "")
                        )
                        if m_content:
                            if m_name in ("creator", "author") and not self._author:
                                self._author = m_content
                            elif m_name == "lastsaveby" and not last_save_by:
                                last_save_by = m_content
                            elif m_name in ("generator", "application") and not self._creator_app:
                                self._creator_app = m_content
                    elif tag == "item":
                        item_id = _get_attr_local(el, "id")
                        href = _get_attr_local(el, "href")
                        if item_id and href:
                            resolved_href = self._resolve_hpf_href(hpf_actual, href)
                            if resolved_href:
                                self.manifest_by_id[item_id] = resolved_href
                    elif tag == "itemref":
                        idref = _get_attr_local(el, "idref")
                        if idref:
                            spine_idrefs.append(idref)

        if not self._author and last_save_by:
            self._author = last_save_by

        if not self._creator_app:
            ver_part = self._resolve_existing_part("version.xml")
            if ver_part is not None:
                v_root = self.read_xml(ver_part)
                if v_root is not None:
                    app_name = _get_attr_local(v_root, "application").strip()
                    if app_name:
                        self._creator_app = app_name

        # 2. header.xml 결정
        if "header" in self.manifest_by_id:
            self.header_part = self.manifest_by_id["header"]
        else:
            for item_id, path in self.manifest_by_id.items():
                if item_id.lower() == "header" or path.lower().endswith("header.xml"):
                    self.header_part = path
                    break
            else:
                fallback_hdr = self._resolve_existing_part("Contents/header.xml")
                if fallback_hdr:
                    self.header_part = fallback_hdr

        # 3. section*.xml 순서 결정(spine 우선, 없으면 manifest 또는 아카이브 파일명 순)
        sections: list[str] = []
        for idref in spine_idrefs:
            target = self.manifest_by_id.get(idref)
            if (
                target
                and target != self.header_part
                and posixpath.basename(target).lower().startswith("section")
                and target.lower().endswith(".xml")
                and target not in sections
            ):
                sections.append(target)

        if not sections:
            for item_id, target in sorted(self.manifest_by_id.items()):
                base = posixpath.basename(target).lower()
                if (
                    (item_id.lower().startswith("section") or base.startswith("section"))
                    and base.endswith(".xml")
                    and target not in sections
                ):
                    sections.append(target)

        if not sections:
            cand_names = [
                n
                for n in self._names
                if posixpath.basename(n).lower().startswith("section")
                and n.lower().endswith(".xml")
            ]

            def _sec_sort_key(p: str) -> tuple[int, str]:
                stem = posixpath.splitext(posixpath.basename(p))[0].lower()
                digits = "".join(ch for ch in stem if ch.isdigit())
                return (int(digits) if digits else 9999, p)

            sections = sorted(cand_names, key=_sec_sort_key)

        self.section_parts = tuple(sections)

    def _resolve_hpf_href(self, hpf_part: str, href: str) -> str | None:
        # 1) 아카이브 루트 기준 시도 (예: "Contents/section0.xml", "BinData/image1.png")
        direct = self._resolve_existing_part(href)
        if direct is not None:
            return direct
        # 2) content.hpf가 있는 디렉터리(Contents/) 기준 상대 경로 시도 (예: "section0.xml")
        rel = _resolve_zip_path(hpf_part, href)
        if rel is not None:
            existing = self._resolve_existing_part(rel)
            if existing is not None:
                return existing
        return None

    def resolve_binary_item(self, binary_id: str) -> str | None:
        """`binaryItemIDRef`(`image1` 등)로부터 아카이브 내 실제 `BinData/*` 경로를 찾는다."""
        if not binary_id:
            return None
        if binary_id in self.manifest_by_id:
            return self.manifest_by_id[binary_id]
        lower_id = binary_id.lower()
        for k, v in self.manifest_by_id.items():
            if k.lower() == lower_id:
                return v
        for n in self._names:
            base = posixpath.basename(n)
            stem, _ = posixpath.splitext(base)
            if stem.lower() == lower_id or base.lower() == lower_id:
                return n
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

    def __enter__(self) -> HwpxPackage:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def has_part(self, part_path: str) -> bool:
        return self._resolve_existing_part(part_path) is not None

    def read_bytes(self, part_path: str, *, max_bytes: int = MAX_MEDIA_BYTES) -> bytes | None:
        actual = self._resolve_existing_part(part_path)
        if actual is None:
            return None
        info = self._zf.getinfo(actual)
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
            return ET.fromstring(data)
        except ET.ParseError:
            return None

    def read_rels(self, _part_path: str) -> dict[str, Relationship]:
        return {}

    def read_header_catalog(self) -> HwpxHeaderCatalog:
        return HwpxHeaderCatalog.from_xml(self.read_xml(self.header_part))

    def read_metadata(self, *, page_count: int) -> DocumentMetadata:
        return DocumentMetadata(
            source_path=self.source_path.name,
            format=SourceFormat.HWPX,
            file_size_bytes=self._file_size,
            sha256=self._sha256,
            page_count=max(1, page_count),
            title=self._title,
            author=self._author,
            creator=self._creator_app,
            producer=self._creator_app,
            has_outline=False,
            is_encrypted=False,
        )


__all__ = [
    "DEFAULT_MARGIN_BOTTOM_PT",
    "DEFAULT_MARGIN_LEFT_PT",
    "DEFAULT_MARGIN_RIGHT_PT",
    "DEFAULT_MARGIN_TOP_PT",
    "DEFAULT_PAGE_HEIGHT_PT",
    "DEFAULT_PAGE_WIDTH_PT",
    "HWPUNIT_PER_PT",
    "MAX_MEDIA_BYTES",
    "MAX_XML_BYTES",
    "OLE_MAGIC",
    "HwpxBorderSpec",
    "HwpxCharProp",
    "HwpxHeaderCatalog",
    "HwpxNumberingLevel",
    "HwpxPackage",
    "HwpxParaProp",
    "HwpxStyleDef",
    "clamp",
    "hwpunit_to_pt",
    "iter_hwpx_children",
    "local_name",
    "make_bbox",
    "probe_image_dimensions",
]
