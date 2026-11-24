"""OOXML(.pptx) 저수준 ZIP 패키지 리더, 관계(rels) 해석, 레이아웃/마스터 상속 유틸리티.

추가 서드파티 의존성(`python-pptx`, `lxml`, `Pillow`) 없이 Python 표준 라이브러리
(`zipfile`, `xml.etree.ElementTree`, `struct`, `posixpath`)만으로 안전하게 `.pptx`
아카이브를 읽는다.
"""

from __future__ import annotations

import hashlib
import posixpath
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.contracts import BBox, DocumentMetadata, SourceFormat

EMU_PER_PT = 12700.0
DEFAULT_SLIDE_WIDTH_PT = 960.0  # 16:9 widescreen (12192000 EMU)
DEFAULT_SLIDE_HEIGHT_PT = 540.0  # 16:9 widescreen (6858000 EMU)

MAX_XML_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
NS_EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
NS_P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
NS_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS_A14 = "http://schemas.microsoft.com/office/drawing/2010/main"
NS_ASVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
NS_CX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
NS_P15 = "http://schemas.microsoft.com/office/powerpoint/2012/main"
NS_P188 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
NS_VML = "urn:schemas-microsoft-com:vml"
NS_OFFICE = "urn:schemas-microsoft-com:office:office"
NS_SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

REL_TYPE_SLIDE_LAYOUT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
)
REL_TYPE_SLIDE_MASTER = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
)
REL_TYPE_NOTES_SLIDE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
)
REL_TYPE_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_TYPE_MODERN_COMMENTS = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
REL_TYPE_COMMENT_AUTHORS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors"
)
REL_TYPE_MODERN_AUTHORS = "http://schemas.microsoft.com/office/2018/10/relationships/authors"
REL_TYPE_VML_DRAWING = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing"
)


def qn(ns: str, name: str) -> str:
    """XML 네임스페이스 정식 태그명(`{ns}name`)을 조합한다."""
    return f"{{{ns}}}{name}"


def local_name(tag: str) -> str:
    """네임스페이스 접두사를 뗀 XML 로컬 태그명을 반환한다."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def emu_to_pt(value: float | int | str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value) / EMU_PER_PT
    except (TypeError, ValueError):
        return default


def make_bbox(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    slide_w: float,
    slide_h: float,
) -> BBox:
    pw = max(1.0, slide_w)
    ph = max(1.0, slide_h)
    cx0 = clamp(min(x0, x1), 0.0, pw)
    ctop = clamp(min(top, bottom), 0.0, ph)
    cx1 = clamp(max(x0, x1), cx0, pw)
    cbot = clamp(max(top, bottom), ctop, ph)
    return BBox(
        x0=round(cx0, 2),
        top=round(ctop, 2),
        x1=round(cx1, 2),
        bottom=round(cbot, 2),
        norm_x0=round(clamp(cx0 / pw, 0.0, 1.0), 4),
        norm_top=round(clamp(ctop / ph, 0.0, 1.0), 4),
        norm_x1=round(clamp(cx1 / pw, 0.0, 1.0), 4),
        norm_bottom=round(clamp(cbot / ph, 0.0, 1.0), 4),
    )


def iter_children_resolving_mc(parent: ET.Element) -> Iterator[ET.Element]:
    """직접 자식 요소를 순회하되 `mc:AlternateContent`는 중복 없이 한 분기만 풀어낸다.

    PowerPoint는 수식·신규 도형·차트 등을 `<mc:AlternateContent>` 안에
    `<mc:Choice>`와 `<mc:Fallback>`으로 이중 기록한다. 둘 다 읽으면 중복 추출되므로
    Choice에 해석 가능한 요소가 있으면 Choice를, 아니면 Fallback을 선택한다.
    """
    for child in parent:
        if child.tag == qn(NS_MC, "AlternateContent"):
            choices = child.findall(qn(NS_MC, "Choice"))
            fallback = child.find(qn(NS_MC, "Fallback"))
            picked: ET.Element | None = None
            for ch in choices:
                if len(ch) > 0:
                    picked = ch
                    break
            if picked is None and fallback is not None:
                picked = fallback
            if picked is not None:
                yield from iter_children_resolving_mc(picked)
        else:
            yield child


@dataclass(frozen=True)
class Relationship:
    """OOXML `.rels` 파일의 개별 관계 항목."""

    r_id: str
    rel_type: str
    target: str
    is_external: bool = False


@dataclass(frozen=True)
class SlideRef:
    """프레젠테이션 내 슬라이드 1장의 순서 번호·ID·아카이브 경로."""

    slide_number: int  # 1-based
    slide_id: str
    part_path: str


@dataclass(frozen=True)
class SectionRef:
    """프레젠테이션 섹션 구분(PowerPoint 2010+ `p14:sectionLst`)."""

    name: str
    slide_ids: tuple[str, ...]


@dataclass(frozen=True)
class CommentAuthor:
    """슬라이드 주석 작성자 정보(`commentAuthors.xml` / 현대 스레드 주석 `authors.xml`)."""

    author_id: str
    name: str
    initials: str = ""


@dataclass(frozen=True)
class PlaceholderStyle:
    """슬라이드 레이아웃·마스터에서 상속받는 플레이스홀더 기하 및 기본 글꼴 크기."""

    ph_type: str | None
    ph_idx: str | None
    rect_pt: tuple[float, float, float, float] | None = None  # (x0, top, x1, bottom)
    default_font_size: float | None = None


@dataclass
class PlaceholderLookup:
    """단일 슬라이드 레이아웃+마스터의 플레이스홀더 상속 인덱스."""

    by_type_and_idx: dict[tuple[str, str], PlaceholderStyle] = field(default_factory=dict)
    by_idx: dict[str, PlaceholderStyle] = field(default_factory=dict)
    by_type: dict[str, PlaceholderStyle] = field(default_factory=dict)
    title_default_font: float | None = None
    body_default_font: float | None = None

    def resolve(
        self, ph_type: str | None, ph_idx: str | None
    ) -> tuple[tuple[float, float, float, float] | None, float | None]:
        """`(rect_pt, default_font_size)`를 `(type, idx) → idx → type` 우선순위로 조회한다."""
        style: PlaceholderStyle | None = None
        if ph_type and ph_idx:
            style = self.by_type_and_idx.get((ph_type, ph_idx))
        if style is None and ph_idx:
            style = self.by_idx.get(ph_idx)
        if style is None and ph_type:
            style = self.by_type.get(ph_type)

        rect = style.rect_pt if style is not None else None
        font = style.default_font_size if style is not None else None
        if font is None and ph_type:
            if ph_type in ("title", "ctrTitle"):
                font = self.title_default_font
            elif ph_type in ("body", "subTitle", "obj"):
                font = self.body_default_font
        return rect, font


def _resolve_zip_path(source_part: str, target: str) -> str | None:
    """OOXML 상대/절대 타깃 경로를 ZIP 내부 정규 POSIX 경로로 안전하게 변환한다."""
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


def _extract_xfrm_rect_pt(sp_pr: ET.Element | None) -> tuple[float, float, float, float] | None:
    if sp_pr is None:
        return None
    xfrm = sp_pr.find(qn(NS_A, "xfrm"))
    if xfrm is None:
        return None
    off = xfrm.find(qn(NS_A, "off"))
    ext = xfrm.find(qn(NS_A, "ext"))
    if off is None or ext is None:
        return None
    x0 = emu_to_pt(off.attrib.get("x"))
    top = emu_to_pt(off.attrib.get("y"))
    w = emu_to_pt(ext.attrib.get("cx"))
    h = emu_to_pt(ext.attrib.get("cy"))
    if w <= 0.0 and h <= 0.0:
        return None
    return (x0, top, x0 + max(0.0, w), top + max(0.0, h))


def _extract_lst_style_font_pt(tx_body: ET.Element | None) -> float | None:
    if tx_body is None:
        return None
    lst = tx_body.find(qn(NS_A, "lstStyle"))
    if lst is None:
        return None
    for lvl_tag in ("lvl1pPr", "defPPr"):
        ppr = lst.find(qn(NS_A, lvl_tag))
        if ppr is not None:
            def_rpr = ppr.find(qn(NS_A, "defRPr"))
            if def_rpr is not None and "sz" in def_rpr.attrib:
                try:
                    return round(int(def_rpr.attrib["sz"]) / 100.0, 2)
                except ValueError:
                    pass
    return None


def _extract_tx_style_font_pt(tx_styles: ET.Element | None, style_tag: str) -> float | None:
    if tx_styles is None:
        return None
    st = tx_styles.find(qn(NS_P, style_tag))
    if st is None:
        return None
    for lvl_tag in ("lvl1pPr", "defPPr"):
        ppr = st.find(qn(NS_A, lvl_tag))
        if ppr is not None:
            def_rpr = ppr.find(qn(NS_A, "defRPr"))
            if def_rpr is not None and "sz" in def_rpr.attrib:
                try:
                    return round(int(def_rpr.attrib["sz"]) / 100.0, 2)
                except ValueError:
                    pass
    return None


def _collect_placeholders_from_xml(
    root: ET.Element,
    lookup: PlaceholderLookup,
    *,
    overwrite: bool,
) -> None:
    c_sld = root.find(qn(NS_P, "cSld"))
    sp_tree = c_sld.find(qn(NS_P, "spTree")) if c_sld is not None else None
    if sp_tree is None:
        return
    for elem in iter_children_resolving_mc(sp_tree):
        if local_name(elem.tag) != "sp":
            continue
        nv = elem.find(qn(NS_P, "nvSpPr"))
        nv_pr = nv.find(qn(NS_P, "nvPr")) if nv is not None else None
        ph = nv_pr.find(qn(NS_P, "ph")) if nv_pr is not None else None
        if ph is None:
            continue
        ph_type = ph.attrib.get("type")
        ph_idx = ph.attrib.get("idx")
        rect = _extract_xfrm_rect_pt(elem.find(qn(NS_P, "spPr")))
        font = _extract_lst_style_font_pt(elem.find(qn(NS_P, "txBody")))
        style = PlaceholderStyle(
            ph_type=ph_type,
            ph_idx=ph_idx,
            rect_pt=rect,
            default_font_size=font,
        )

        def _merge(existing: PlaceholderStyle | None, new_s: PlaceholderStyle) -> PlaceholderStyle:
            if existing is None:
                return new_s
            if overwrite:
                return PlaceholderStyle(
                    ph_type=new_s.ph_type or existing.ph_type,
                    ph_idx=new_s.ph_idx or existing.ph_idx,
                    rect_pt=new_s.rect_pt or existing.rect_pt,
                    default_font_size=new_s.default_font_size or existing.default_font_size,
                )
            return PlaceholderStyle(
                ph_type=existing.ph_type or new_s.ph_type,
                ph_idx=existing.ph_idx or new_s.ph_idx,
                rect_pt=existing.rect_pt or new_s.rect_pt,
                default_font_size=existing.default_font_size or new_s.default_font_size,
            )

        if ph_type and ph_idx:
            key = (ph_type, ph_idx)
            lookup.by_type_and_idx[key] = _merge(lookup.by_type_and_idx.get(key), style)
        if ph_idx:
            lookup.by_idx[ph_idx] = _merge(lookup.by_idx.get(ph_idx), style)
        if ph_type:
            lookup.by_type[ph_type] = _merge(lookup.by_type.get(ph_type), style)


def probe_image_dimensions(data: bytes, extension: str = "") -> tuple[int | None, int | None]:
    """PNG·JPEG·GIF·BMP·WebP 헤더에서 픽셀 크기 `(width, height)`를 순수 Python으로 읽는다."""
    if len(data) < 10:
        return None, None
    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return (w, h) if (w > 0 and h > 0) else (None, None)
    # GIF
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h) if (w > 0 and h > 0) else (None, None)
    # BMP
    if data.startswith(b"BM") and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return (abs(w), abs(h)) if (w != 0 and h != 0) else (None, None)
    # WebP
    if data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
    # JPEG
    if data.startswith(b"\xff\xd8"):
        pos = 2
        n = len(data)
        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while pos + 4 <= n:
            if data[pos] != 0xFF:
                pos += 1
                continue
            while pos < n and data[pos] == 0xFF:
                pos += 1
            if pos >= n:
                break
            marker = data[pos]
            pos += 1
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                continue
            if pos + 2 > n:
                break
            seg_len = struct.unpack(">H", data[pos : pos + 2])[0]
            if seg_len < 2 or pos + seg_len > n:
                break
            if marker in sof_markers and seg_len >= 7:
                h, w = struct.unpack(">HH", data[pos + 3 : pos + 7])
                return (w, h) if (w > 0 and h > 0) else (None, None)
            pos += seg_len
    _ = extension
    return None, None


class PptxPackage:
    """`.pptx` ZIP 아카이브를 안전하게 열고 XML·관계·미디어·상속 레이아웃을 조회한다."""

    def __init__(self, source_path: Path) -> None:
        if not source_path.exists():
            raise ParserInputError(f"PPTX 파일이 존재하지 않는다: {source_path.name}")
        if not source_path.is_file():
            raise ParserInputError(f"PPTX 입력 경로가 파일이 아니다: {source_path.name}")

        raw_head = b""
        try:
            with source_path.open("rb") as fh:
                raw_head = fh.read(8)
        except OSError as exc:
            raise ParserInputError(f"PPTX 파일을 읽을 수 없다: {source_path.name}") from exc

        if raw_head.startswith(OLE_MAGIC):
            raise ParserInputError(
                "암호화된 PPTX이거나 구형 바이너리 PPT(OLE) 형식이다 — "
                "암호를 해제하거나 최신 OOXML(.pptx)로 저장한 뒤 다시 시도하라."
            )

        self._source_path = source_path
        self._file_size = source_path.stat().st_size
        self._sha256 = self._compute_sha256(source_path)

        try:
            self._zf = zipfile.ZipFile(source_path, "r")
        except zipfile.BadZipFile as exc:
            raise ParserInputError(
                f"유효한 PPTX(OOXML ZIP) 파일이 아니다: {source_path.name}"
            ) from exc
        except OSError as exc:
            raise ParserInputError(f"PPTX 아카이브를 열 수 없다: {source_path.name}") from exc

        self._names = set(self._zf.namelist())
        self._rels_cache: dict[str, dict[str, Relationship]] = {}
        self._layout_cache: dict[str, PlaceholderLookup] = {}

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def close(self) -> None:
        self._zf.close()

    def __enter__(self) -> PptxPackage:
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
            return ET.fromstring(data)
        except ET.ParseError:
            return None

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

    def resolve_placeholder_lookup(self, slide_part: str) -> PlaceholderLookup:
        """슬라이드의 `slideLayout` + `slideMaster` 상속 플레이스홀더 인덱스를 반환(캐시됨)."""
        slide_rels = self.read_rels(slide_part)
        layout_part = next(
            (
                r.target
                for r in slide_rels.values()
                if not r.is_external and r.rel_type == REL_TYPE_SLIDE_LAYOUT
            ),
            None,
        )
        if layout_part is None:
            return PlaceholderLookup()
        if layout_part in self._layout_cache:
            return self._layout_cache[layout_part]

        lookup = PlaceholderLookup()
        # 1. 마스터 기본값 먼저 채우기
        layout_rels = self.read_rels(layout_part)
        master_part = next(
            (
                r.target
                for r in layout_rels.values()
                if not r.is_external and r.rel_type == REL_TYPE_SLIDE_MASTER
            ),
            None,
        )
        if master_part is not None:
            master_root = self.read_xml(master_part)
            if master_root is not None:
                _collect_placeholders_from_xml(master_root, lookup, overwrite=False)
                tx_styles = master_root.find(qn(NS_P, "txStyles"))
                lookup.title_default_font = _extract_tx_style_font_pt(tx_styles, "titleStyle")
                lookup.body_default_font = _extract_tx_style_font_pt(tx_styles, "bodyStyle")

        # 2. 슬라이드 레이아웃으로 구체값 덮어쓰기
        layout_root = self.read_xml(layout_part)
        if layout_root is not None:
            _collect_placeholders_from_xml(layout_root, lookup, overwrite=True)

        self._layout_cache[layout_part] = lookup
        return lookup

    def read_presentation_structure(
        self,
    ) -> tuple[DocumentMetadata, float, float, list[SlideRef], list[SectionRef]]:
        """프레젠테이션 메타데이터, 슬라이드 크기(pt), 정렬된 슬라이드 목록·섹션을 읽는다."""
        pres_part = "ppt/presentation.xml"
        pres_root = self.read_xml(pres_part)
        if pres_root is None:
            raise ParserInputError(
                f"PPTX 내부에 `ppt/presentation.xml`이 없거나 손상됐다: {self._source_path.name}"
            )

        # 슬라이드 크기
        sld_sz = pres_root.find(qn(NS_P, "sldSz"))
        slide_w = DEFAULT_SLIDE_WIDTH_PT
        slide_h = DEFAULT_SLIDE_HEIGHT_PT
        if sld_sz is not None:
            w_val = emu_to_pt(sld_sz.attrib.get("cx"))
            h_val = emu_to_pt(sld_sz.attrib.get("cy"))
            if w_val > 10.0 and h_val > 10.0:
                slide_w = round(w_val, 2)
                slide_h = round(h_val, 2)

        # 슬라이드 순서(sldIdLst → presentation.xml.rels)
        pres_rels = self.read_rels(pres_part)
        slides: list[SlideRef] = []
        sld_id_lst = pres_root.find(qn(NS_P, "sldIdLst"))
        if sld_id_lst is not None:
            for idx, sld_el in enumerate(sld_id_lst.findall(qn(NS_P, "sldId")), 1):
                r_id = sld_el.attrib.get(qn(NS_R, "id"), "")
                sld_id = sld_el.attrib.get("id", str(idx))
                rel = pres_rels.get(r_id)
                if rel is not None and not rel.is_external and self.has_part(rel.target):
                    slides.append(
                        SlideRef(
                            slide_number=len(slides) + 1, slide_id=sld_id, part_path=rel.target
                        )
                    )

        if not slides:
            raise ParserInputError(
                f"PPTX 파일에 분석 가능한 슬라이드가 없다: {self._source_path.name}"
            )

        # 선택적 섹션 목록(p14:sectionLst)
        sections: list[SectionRef] = []
        for sec_el in pres_root.iter(qn(NS_P14, "section")):
            sec_name = (sec_el.attrib.get("name") or "").strip()
            sec_ids: list[str] = []
            for sid_el in sec_el.iter(qn(NS_P14, "sldId")):
                sid = sid_el.attrib.get("id")
                if sid:
                    sec_ids.append(sid)
            if sec_name and sec_ids:
                sections.append(SectionRef(name=sec_name, slide_ids=tuple(sec_ids)))

        # 문서 메타데이터(core.xml, app.xml)
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

        metadata = DocumentMetadata(
            source_path=self._source_path.name,
            format=SourceFormat.PPTX,
            file_size_bytes=self._file_size,
            sha256=self._sha256,
            page_count=len(slides),
            title=title,
            author=author,
            creator=creator_app,
            producer=creator_app,
            has_outline=False,  # 슬라이드 제목·섹션 추출 후 갱신
            is_encrypted=False,
        )
        return metadata, slide_w, slide_h, slides, sections

    def read_comment_authors(self) -> dict[str, CommentAuthor]:
        """구형(`commentAuthors.xml`) 및 현대(`authors.xml`) 주석 작성자 목록을 읽는다."""
        authors: dict[str, CommentAuthor] = {}
        pres_rels = self.read_rels("ppt/presentation.xml")
        candidate_parts: list[str] = []
        for rel in pres_rels.values():
            if not rel.is_external and rel.rel_type in (
                REL_TYPE_COMMENT_AUTHORS,
                REL_TYPE_MODERN_AUTHORS,
            ):
                candidate_parts.append(rel.target)
        for fallback in ("ppt/commentAuthors.xml", "ppt/authors.xml"):
            if self.has_part(fallback) and fallback not in candidate_parts:
                candidate_parts.append(fallback)

        for part in candidate_parts:
            root = self.read_xml(part)
            if root is None:
                continue
            for el in root.iter():
                tag = local_name(el.tag)
                if tag in ("cmAuthor", "author"):
                    aid = (el.attrib.get("id") or "").strip()
                    name = (el.attrib.get("name") or "").strip()
                    initials = (el.attrib.get("initials") or "").strip()
                    if aid and (name or initials):
                        authors[aid] = CommentAuthor(
                            author_id=aid,
                            name=name or initials,
                            initials=initials,
                        )
        return authors

    def read_vml_image_targets(self, slide_rels: dict[str, Relationship]) -> dict[str, str]:
        """슬라이드의 `vmlDrawing*.vml`에서 `spid`/`id` → 미디어 파트 경로 매핑을 추출한다."""
        mapping: dict[str, str] = {}
        for rel in slide_rels.values():
            if rel.is_external or rel.rel_type != REL_TYPE_VML_DRAWING:
                continue
            vml_root = self.read_xml(rel.target)
            if vml_root is None:
                continue
            vml_rels = self.read_rels(rel.target)
            for shape_el in vml_root.iter():
                if local_name(shape_el.tag) != "shape":
                    continue
                spid = (shape_el.attrib.get(qn(NS_OFFICE, "spid")) or "").strip()
                shape_id = (shape_el.attrib.get("id") or "").strip()
                rel_id = ""
                for sub in shape_el.iter():
                    if local_name(sub.tag) == "imagedata":
                        rel_id = (
                            sub.attrib.get(qn(NS_OFFICE, "relid"))
                            or sub.attrib.get(qn(NS_R, "id"))
                            or ""
                        ).strip()
                        if rel_id:
                            break
                if not rel_id:
                    continue
                img_rel = vml_rels.get(rel_id)
                if img_rel is None or img_rel.is_external or not self.has_part(img_rel.target):
                    continue
                if spid:
                    mapping[spid] = img_rel.target
                if shape_id:
                    mapping[shape_id] = img_rel.target
        return mapping
