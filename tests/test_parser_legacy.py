"""구형 바이너리 OLE2 문서(`.hwp`, `.doc`, `.ppt`) 파서 및 CFB 리더 단위·통합 테스트."""

from __future__ import annotations

import hashlib
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.parser import (
    CfbFile,
    CropStatus,
    DocDocumentAdapter,
    HeuristicRasterLayoutDetector,
    HwpDocumentAdapter,
    ParseOptions,
    ParserInputError,
    PptDocumentAdapter,
    SourceFormat,
    get_adapter_for_path,
    inspect_document,
    parse_and_export,
    parse_document,
    supported_extensions,
)
from autoetl.parser.image_io import encode_png
from autoetl.parser.legacy.cfb import extract_officeart_blips, read_ole_summary_info

# ---------------------------------------------------------------------------
# 순수 Python MS-CFB v3 합성 라이터 (FAT + MiniFAT + 중첩 스토리지 지원)
# ---------------------------------------------------------------------------

_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF
_SECTOR_SIZE = 512
_MINI_SECTOR_SIZE = 64
_MINI_CUTOFF = 4096


def _pack_dir_entry(
    name: str,
    obj_type: int,
    *,
    left_id: int = _NOSTREAM,
    right_id: int = _NOSTREAM,
    child_id: int = _NOSTREAM,
    start_sector: int = _ENDOFCHAIN,
    stream_size: int = 0,
) -> bytes:
    encoded = (name + "\x00").encode("utf-16le")
    name_len = min(64, len(encoded))
    name_field = encoded[:64].ljust(64, b"\x00")
    buf = bytearray(128)
    buf[0:64] = name_field
    struct.pack_into("<H", buf, 64, name_len)
    buf[66] = obj_type
    buf[67] = 1  # black node
    struct.pack_into("<III", buf, 68, left_id, right_id, child_id)
    struct.pack_into("<IQ", buf, 116, start_sector, stream_size)
    return bytes(buf)


def _write_cfb_v3(path: Path, streams: dict[str, bytes]) -> Path:
    """`{"BodyText/Section0": b"..."}` 스트림 맵으로 유효한 OLE2 CFB v3 파일을 생성한다."""
    # 1. 디렉터리 트리 구성 (root -> storages -> streams)
    root_children: dict[str, dict[str, object]] = {}
    for full_path, data in streams.items():
        parts = full_path.split("/")
        cur = root_children
        for seg in parts[:-1]:
            if seg not in cur:
                cur[seg] = {"type": 1, "children": {}, "data": b""}
            cur = cur[seg]["children"]  # type: ignore[assignment]
        cur[parts[-1]] = {"type": 2, "children": {}, "data": data}

    # 선형 디렉터리 테이블 인덱스 할당 (0 = Root Entry)
    nodes: list[dict[str, object]] = [
        {"name": "Root Entry", "type": 5, "children_map": root_children, "data": b""}
    ]

    def _assign_ids(children_map: dict[str, dict[str, object]]) -> list[int]:
        ids: list[int] = []
        for name, info in children_map.items():
            nid = len(nodes)
            nodes.append(
                {
                    "name": name,
                    "type": info["type"],
                    "children_map": info["children"],
                    "data": info["data"],
                }
            )
            ids.append(nid)
            if info["type"] == 1:
                info["child_ids"] = _assign_ids(info["children"])  # type: ignore[arg-type]
        return ids

    root_child_ids = _assign_ids(root_children)

    # 각 형제 리스트를 단순 오른쪽 편향 이진 탐색 트리(right_id 체인)로 연결
    left_map: dict[int, int] = {}
    right_map: dict[int, int] = {}
    child_map: dict[int, int] = {}

    def _link_siblings(parent_id: int, sib_ids: list[int]) -> None:
        if not sib_ids:
            return
        child_map[parent_id] = sib_ids[0]
        for idx, nid in enumerate(sib_ids):
            if idx + 1 < len(sib_ids):
                right_map[nid] = sib_ids[idx + 1]

    _link_siblings(0, root_child_ids)
    for nid, nd in enumerate(nodes):
        if nid > 0 and nd["type"] == 1:
            cmap = nd["children_map"]
            assert isinstance(cmap, dict)
            sibs: list[int] = []
            for cname, cinfo in cmap.items():
                for cid, cand in enumerate(nodes):
                    if (
                        cid > 0
                        and cand["name"] == cname
                        and cand["type"] == cinfo["type"]
                        and cand["children_map"] is cinfo["children"]
                    ):
                        sibs.append(cid)
                        break
            _link_siblings(nid, sibs)

    # 2. MiniStream (size < 4096) vs Regular FAT (size >= 4096) 배치
    sectors: list[bytes] = []
    fat: list[int] = []

    def _alloc_chain(raw: bytes) -> int:
        if not raw:
            return _ENDOFCHAIN
        start = len(sectors)
        n_sec = (len(raw) + _SECTOR_SIZE - 1) // _SECTOR_SIZE
        for i in range(n_sec):
            chunk = raw[i * _SECTOR_SIZE : (i + 1) * _SECTOR_SIZE].ljust(_SECTOR_SIZE, b"\x00")
            sectors.append(chunk)
            fat.append(start + i + 1 if i + 1 < n_sec else _ENDOFCHAIN)
        return start

    mini_stream_chunks: list[bytes] = []
    mini_fat: list[int] = []

    def _alloc_mini_chain(raw: bytes) -> int:
        if not raw:
            return _ENDOFCHAIN
        start = len(mini_stream_chunks)
        n_msec = (len(raw) + _MINI_SECTOR_SIZE - 1) // _MINI_SECTOR_SIZE
        for i in range(n_msec):
            chunk = raw[i * _MINI_SECTOR_SIZE : (i + 1) * _MINI_SECTOR_SIZE].ljust(
                _MINI_SECTOR_SIZE, b"\x00"
            )
            mini_stream_chunks.append(chunk)
            mini_fat.append(start + i + 1 if i + 1 < n_msec else _ENDOFCHAIN)
        return start

    start_sec_map: dict[int, int] = {}
    size_map: dict[int, int] = {}
    for nid, nd in enumerate(nodes):
        if nd["type"] == 2:
            raw = nd["data"]
            assert isinstance(raw, bytes)
            size_map[nid] = len(raw)
            if len(raw) == 0:
                start_sec_map[nid] = _ENDOFCHAIN
            elif len(raw) < _MINI_CUTOFF:
                start_sec_map[nid] = _alloc_mini_chain(raw)
            else:
                start_sec_map[nid] = _alloc_chain(raw)

    # MiniStream 컨테이너를 Root Entry에 할당
    mini_container_bytes = b"".join(mini_stream_chunks)
    if mini_container_bytes:
        start_sec_map[0] = _alloc_chain(mini_container_bytes)
        size_map[0] = len(mini_container_bytes)
    else:
        start_sec_map[0] = _ENDOFCHAIN
        size_map[0] = 0

    # MiniFAT 섹터 할당
    first_minifat_sec = _ENDOFCHAIN
    num_minifat_secs = 0
    if mini_fat:
        mf_bytes = b"".join(struct.pack("<I", v) for v in mini_fat)
        # 섹터 끝까지 FREESECT 패딩
        rem = len(mf_bytes) % _SECTOR_SIZE
        if rem:
            mf_bytes += struct.pack("<I", _FREESECT) * ((_SECTOR_SIZE - rem) // 4)
        first_minifat_sec = _alloc_chain(mf_bytes)
        num_minifat_secs = len(mf_bytes) // _SECTOR_SIZE

    # Directory 섹터 할당
    dir_entries_bytes = bytearray()
    for nid, nd in enumerate(nodes):
        obj_t = nd["type"]
        assert isinstance(obj_t, int)
        dir_entries_bytes.extend(
            _pack_dir_entry(
                str(nd["name"]),
                obj_t,
                left_id=left_map.get(nid, _NOSTREAM),
                right_id=right_map.get(nid, _NOSTREAM),
                child_id=child_map.get(nid, _NOSTREAM),
                start_sector=start_sec_map.get(nid, _ENDOFCHAIN),
                stream_size=size_map.get(nid, 0),
            )
        )
    while len(dir_entries_bytes) % _SECTOR_SIZE != 0:
        dir_entries_bytes.extend(b"\x00" * 128)
    first_dir_sec = _alloc_chain(bytes(dir_entries_bytes))

    # FAT 섹터 수 계산 및 자기 자신(FATSECT) 기록
    entries_per_fat_sec = _SECTOR_SIZE // 4
    num_fat_secs = 1
    while (len(fat) + num_fat_secs) > num_fat_secs * entries_per_fat_sec:
        num_fat_secs += 1
    assert num_fat_secs <= 109

    fat_sec_ids: list[int] = []
    for _ in range(num_fat_secs):
        fat_sec_ids.append(len(fat))
        fat.append(_FATSECT)

    fat_padded = list(fat)
    while len(fat_padded) < num_fat_secs * entries_per_fat_sec:
        fat_padded.append(_FREESECT)

    for i in range(num_fat_secs):
        chunk_entries = fat_padded[i * entries_per_fat_sec : (i + 1) * entries_per_fat_sec]
        sectors.append(b"".join(struct.pack("<I", v) for v in chunk_entries))

    # 512바이트 헤더 작성
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HH", header, 24, 0x003E, 0x0003)  # minor=0x3E, major=3 (v3)
    struct.pack_into("<H", header, 28, 0xFFFE)  # little endian
    struct.pack_into("<HH", header, 30, 9, 6)  # 512B / 64B
    struct.pack_into("<I", header, 44, num_fat_secs)
    struct.pack_into("<I", header, 48, first_dir_sec)
    struct.pack_into("<I", header, 56, _MINI_CUTOFF)
    struct.pack_into("<I", header, 60, first_minifat_sec)
    struct.pack_into("<I", header, 64, num_minifat_secs)
    struct.pack_into("<I", header, 68, _ENDOFCHAIN)  # first DIFAT
    struct.pack_into("<I", header, 72, 0)  # num DIFAT
    for i in range(109):
        val = fat_sec_ids[i] if i < len(fat_sec_ids) else _FREESECT
        struct.pack_into("<I", header, 76 + i * 4, val)

    path.write_bytes(bytes(header) + b"".join(sectors))
    return path


def _make_ole_summary_stream(
    *,
    title: str = "",
    author: str = "",
    creator: str = "",
) -> bytes:
    """`\\x05SummaryInformation` / `\\x05HwpSummaryInformation` 속성셋 바이너리를 생성한다."""
    props: list[tuple[int, bytes]] = []
    # CodePage (PID 1) = UTF-8 (65001)
    props.append((1, struct.pack("<IH", 2, 65001) + b"\x00\x00"))
    for pid, val in ((2, title), (4, author), (18, creator)):
        if val:
            enc = val.encode("utf-8") + b"\x00"
            pad = (4 - (len(enc) % 4)) % 4
            props.append((pid, struct.pack("<II", 30, len(enc)) + enc + b"\x00" * pad))

    # PropertySetStream header (48 bytes) + Section
    sec_off = 48
    sec_hdr_size = 8 + len(props) * 8
    prop_offsets: list[int] = []
    prop_blobs = bytearray()
    for _pid, blob in props:
        prop_offsets.append(sec_hdr_size + len(prop_blobs))
        prop_blobs.extend(blob)

    sec_size = sec_hdr_size + len(prop_blobs)
    sec = bytearray()
    sec.extend(struct.pack("<II", sec_size, len(props)))
    for (pid, _blob), off in zip(props, prop_offsets, strict=True):
        sec.extend(struct.pack("<II", pid, off))
    sec.extend(prop_blobs)

    hdr = bytearray(48)
    struct.pack_into("<HHI", hdr, 0, 0xFFFE, 0, 0x00020501)
    struct.pack_into("<I", hdr, 24, 1)  # 1 section
    # FMTID_SummaryInformation
    hdr[28:44] = b"\xe0\x85\x9f\xf2\xf9\x4f\x68\x10\xab\x91\x08\x00\x2b\x27\xb3\xd9"
    struct.pack_into("<I", hdr, 44, sec_off)
    return bytes(hdr) + bytes(sec)


def _make_solid_png(
    width: int = 96, height: int = 64, rgb: tuple[int, int, int] = (35, 120, 200)
) -> bytes:
    row = bytes(rgb) * width
    return encode_png(row * height, width, height)


def _make_synthetic_scanned_page_png(width: int = 360, height: int = 480) -> bytes:
    """상단 제목·중단 격자 표·하단 컬러 차트가 포함된 합성 스캔 페이지 PNG."""
    buf = bytearray(b"\xff\xff\xff" * (width * height))

    def set_px(x: int, y: int, r: int, g: int, b: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            idx = (y * width + x) * 3
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b

    for y in range(38, 56):
        if y % 5 != 0:
            for x in range(40, 300):
                if (x // 4) % 6 != 0:
                    set_px(x, y, 20, 20, 20)

    for y in (110, 145, 185, 225, 260):
        for x in range(35, 326):
            set_px(x, y, 15, 15, 15)
            set_px(x, min(height - 1, y + 1), 15, 15, 15)
    for x in (35, 130, 225, 325):
        for y in range(110, 261):
            set_px(x, y, 15, 15, 15)
            set_px(min(width - 1, x + 1), y, 15, 15, 15)
    for ry in (122, 160, 200, 238):
        for cx in (50, 145, 240):
            for y in range(ry, ry + 8):
                for x in range(cx, cx + 55):
                    if (x + y) % 3 != 0:
                        set_px(x, y, 30, 30, 30)

    for bx0, by0, bx1, by1, (r, g, b) in (
        (65, 350, 110, 430, (220, 60, 50)),
        (135, 330, 180, 430, (40, 130, 220)),
        (205, 370, 250, 430, (50, 180, 90)),
    ):
        for y in range(by0, by1):
            for x in range(bx0, bx1):
                set_px(x, y, r, g, b)

    return encode_png(bytes(buf), width, height)


# ---------------------------------------------------------------------------
# HWP 5.0 합성 바이너리 헬퍼
# ---------------------------------------------------------------------------


def _hwp_rec(tag_id: int, level: int, payload: bytes) -> bytes:
    size = len(payload)
    if size < 0xFFF:
        hdr = (tag_id & 0x3FF) | ((level & 0x3FF) << 10) | ((size & 0xFFF) << 20)
        return struct.pack("<I", hdr) + payload
    hdr = (tag_id & 0x3FF) | ((level & 0x3FF) << 10) | (0xFFF << 20)
    return struct.pack("<II", hdr, size) + payload


def _u16_str(s: str) -> bytes:
    return struct.pack("<H", len(s)) + s.encode("utf-16le")


def _hwp_para_header(para_shape_id: int = 0, style_id: int = 0, col_div: int = 0) -> bytes:
    buf = bytearray(22)
    struct.pack_into("<HBB", buf, 8, para_shape_id, style_id, col_div)
    return bytes(buf)


def _hwp_para_text(text: str) -> bytes:
    return text.encode("utf-16le") + struct.pack("<H", 13)


def _hwp_ctrl_id(cid: str) -> bytes:
    assert len(cid) == 4
    return cid.encode("latin1")[::-1]


def _deflate_raw(data: bytes) -> bytes:
    comp = zlib.compressobj(level=6, wbits=-15)
    return comp.compress(data) + comp.flush()


def _build_rich_sample_hwp(path: Path) -> Path:
    """개요·번호·코드·수식·덧말·글자겹침·각주·숨은설명·병합표·그림·OLE·스캔2p HWP 5.0 생성."""
    small_png = _make_solid_png(120, 80, (25, 95, 185))
    scan_png = _make_synthetic_scanned_page_png(360, 480)

    # 1. FileHeader (256B, compressed=1)
    fh = bytearray(256)
    fh[0:17] = b"HWP Document File"
    struct.pack_into("<BBBB", fh, 32, 0, 7, 0, 5)  # 5.0.7.0
    struct.pack_into("<I", fh, 36, 0x01)  # compressed

    # 2. DocInfo
    di = bytearray()
    # BinData 1 (small_png) & BinData 2 (scan_png), EMBEDDING(1), uncompressed(2)
    di.extend(_hwp_rec(0x12, 0, struct.pack("<HH", (2 << 4) | 1, 1) + _u16_str("png")))
    di.extend(_hwp_rec(0x12, 0, struct.pack("<HH", (2 << 4) | 1, 2) + _u16_str("png")))
    # FaceName 0 = 함초롬바탕, 1 = D2Coding
    di.extend(_hwp_rec(0x13, 0, b"\x00" + _u16_str("함초롬바탕")))
    di.extend(_hwp_rec(0x13, 0, b"\x00" + _u16_str("D2Coding")))
    # BorderFill 1 = 4면 실선
    bf = bytearray(26)
    bf[2:6] = b"\x01\x01\x01\x01"
    di.extend(_hwp_rec(0x14, 0, bytes(bf)))
    # CharShape 0 (10pt normal), 1 (16pt bold), 2 (10pt monospace D2Coding)
    for fid, size_hu, attr in ((0, 1000, 0), (0, 1600, 0x02), (1, 1000, 0)):
        cs = bytearray(54)
        struct.pack_into("<7H", cs, 0, fid, fid, fid, fid, fid, fid, fid)
        struct.pack_into("<iI", cs, 42, size_hu, attr)
        di.extend(_hwp_rec(0x15, 0, bytes(cs)))
    # Numbering 1: level 1 = DIGIT "^1."
    num_pl = bytearray()
    for _lvl in range(7):
        num_pl.extend(struct.pack("<IHHI", 0, 0, 0, 0))
        num_pl.extend(_u16_str("^1."))
    num_pl.extend(struct.pack("<H", 1))
    di.extend(_hwp_rec(0x17, 0, bytes(num_pl)))
    # ParaShape 0 (NONE), 1 (OUTLINE lvl 0), 2 (NUMBER id=1 lvl 0)
    for h_type, h_lvl, h_ref in ((0, 0, 0), (1, 0, 0), (2, 0, 1)):
        ps = bytearray(32)
        attr1 = ((h_type & 0x03) << 23) | ((h_lvl & 0x07) << 25)
        struct.pack_into("<I", ps, 0, attr1)
        struct.pack_into("<H", ps, 24, h_ref)
        di.extend(_hwp_rec(0x19, 0, bytes(ps)))
    # Styles 0=바탕글, 1=개요 1, 2=코드
    for name, eng, p_ref, c_ref in (
        ("바탕글", "Normal", 0, 0),
        ("개요 1", "Outline 1", 1, 1),
        ("코드", "Code", 0, 2),
    ):
        st = _u16_str(name) + _u16_str(eng) + struct.pack("<BBHHH", 0, 0, 1042, p_ref, c_ref)
        di.extend(_hwp_rec(0x1A, 0, st))

    # 3. BodyText/Section0
    sec = bytearray()
    # P1: Section def + Header/Footer + Heading 1
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header(para_shape_id=1, style_id=1)))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("1. 구형 바이너리 호환성 개요")))
    sec.extend(_hwp_rec(0x44, 1, struct.pack("<II", 0, 1)))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("secd") + b"\x00" * 34))
    page_def = (
        struct.pack("<6I", 59528, 84188, 8504, 8504, 5668, 4252)
        + b"\x00" * 12
        + struct.pack("<I", 0)
    )
    sec.extend(_hwp_rec(0x49, 2, page_def))
    # head / foot controls
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("head") + b"\x00" * 4))
    sec.extend(_hwp_rec(0x48, 2, struct.pack("<HI", 1, 0) + b"\x00" * 10))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("2026년도 구형 문서 파서 기술서")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("foot") + b"\x00" * 4))
    sec.extend(_hwp_rec(0x48, 2, struct.pack("<HI", 1, 0) + b"\x00" * 10))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("Auto ETL 표준 플랫폼")))

    # P2: Numbered list items
    for item_txt in ("첫 번째 번호 항목", "두 번째 번호 항목"):
        sec.extend(_hwp_rec(0x42, 0, _hwp_para_header(para_shape_id=2, style_id=0)))
        sec.extend(_hwp_rec(0x43, 1, _hwp_para_text(item_txt)))

    # P3: Inline controls (footnote, endnote, hyperlink, dutmal, tcps compose)
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("상세 규격은 공식 문서를 참고한다. ")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("fn  ") + struct.pack("<HH", 1, 0)))
    sec.extend(_hwp_rec(0x48, 2, struct.pack("<HI", 1, 0) + b"\x00" * 10))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("한컴 HWP 5.0 바이너리 공개 명세")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("en  ") + struct.pack("<HH", 1, 0)))
    sec.extend(_hwp_rec(0x48, 2, struct.pack("<HI", 1, 0) + b"\x00" * 10))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("부록 A. CFB 섹터 배치")))
    hlk_payload = _hwp_ctrl_id("%hlk") + "https\\://autoetl.example.kr/spec;0;0;".encode("utf-16le")
    sec.extend(_hwp_rec(0x47, 1, hlk_payload))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("tdut") + _u16_str("한글") + _u16_str("HWP")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("tcps") + _u16_str("②40")))

    # P4: Empty paragraph carrying hidden comment (tcmt — regression for hwp_hidden_comment.hwp)
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("tcmt")))
    sec.extend(_hwp_rec(0x48, 2, b"\x00" * 16))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("검토자 메모: 빈 문단 숨은설명 보존")))

    # P5: Equation-only paragraph + Code style paragraph
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("")))
    sec.extend(_hwp_rec(0x47, 1, _hwp_ctrl_id("eqed") + b"\x00" * 4))
    eq_script = "{a + b} over c"
    sec.extend(
        _hwp_rec(0x58, 2, struct.pack("<IH", 0, len(eq_script)) + eq_script.encode("utf-16le"))
    )

    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header(para_shape_id=0, style_id=2)))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("uv run autoetl parse notice.hwp")))
    sec.extend(_hwp_rec(0x44, 1, struct.pack("<II", 0, 2)))

    # P6: Table with caption + merged header cell (2x3)
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("")))
    tbl_ctrl = bytearray(40)
    tbl_ctrl[0:4] = _hwp_ctrl_id("tbl ")
    struct.pack_into("<II", tbl_ctrl, 16, 40000, 12000)
    sec.extend(_hwp_rec(0x47, 1, bytes(tbl_ctrl)))
    # Caption LIST_HEADER before HWPTAG_TABLE
    sec.extend(_hwp_rec(0x48, 2, b"\x00" * 16))
    sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 3, _hwp_para_text("표 1. 구형 형식 지원 현황")))
    # HWPTAG_TABLE: 2 rows, 3 cols, borderFill=1
    tbl_pl = bytearray(24)
    struct.pack_into("<IHH", tbl_pl, 0, 0, 2, 3)
    struct.pack_into("<H", tbl_pl, 22, 1)  # 18 + 2*2 = 22
    sec.extend(_hwp_rec(0x4D, 2, bytes(tbl_pl)))

    def _emit_cell(col: int, row: int, cspan: int, rspan: int, text: str) -> None:
        lh = bytearray(36)
        struct.pack_into("<HHHHII", lh, 8, col, row, cspan, rspan, 12000, 3000)
        struct.pack_into("<H", lh, 32, 1)
        sec.extend(_hwp_rec(0x48, 2, bytes(lh)))
        sec.extend(_hwp_rec(0x42, 2, _hwp_para_header()))
        sec.extend(_hwp_rec(0x43, 3, _hwp_para_text(text)))

    _emit_cell(0, 0, 1, 1, "형식")
    _emit_cell(1, 0, 2, 1, "지원 전략")
    _emit_cell(0, 1, 1, 1, "HWP 5.0")
    _emit_cell(1, 1, 1, 1, "네이티브")
    _emit_cell(2, 1, 1, 1, "무의존성")

    # P7: Embedded Picture (BIN0001.png referenced twice -> tests cache) + OLE object ($ole / 0x54)
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header()))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("")))
    for _ in range(2):
        gso_pl = bytearray(40)
        gso_pl[0:4] = _hwp_ctrl_id("gso ")
        struct.pack_into("<II", gso_pl, 16, 18000, 12000)
        sec.extend(_hwp_rec(0x47, 1, bytes(gso_pl)))
        sec.extend(_hwp_rec(0x4C, 2, _hwp_ctrl_id("$pic") + b"\x00" * 32))
        pic_pl = bytearray(76)
        struct.pack_into("<H", pic_pl, 71, 1)  # bin_id = 1
        sec.extend(_hwp_rec(0x55, 3, bytes(pic_pl)))

    ole_gso = bytearray(40)
    ole_gso[0:4] = _hwp_ctrl_id("gso ")
    struct.pack_into("<II", ole_gso, 16, 22000, 14000)
    sec.extend(_hwp_rec(0x47, 1, bytes(ole_gso)))
    sec.extend(_hwp_rec(0x4C, 2, _hwp_ctrl_id("$ole") + b"\x00" * 32))
    sec.extend(_hwp_rec(0x54, 3, b"\x01\x00\x00\x00" + b"\x00" * 22))

    # P8: Explicit page break -> Page 2 full-page scanned PNG (BIN0002.png)
    sec.extend(_hwp_rec(0x42, 0, _hwp_para_header(col_div=0x04)))
    sec.extend(_hwp_rec(0x43, 1, _hwp_para_text("")))
    scan_gso = bytearray(40)
    scan_gso[0:4] = _hwp_ctrl_id("gso ")
    struct.pack_into("<II", scan_gso, 16, 52000, 74000)
    sec.extend(_hwp_rec(0x47, 1, bytes(scan_gso)))
    sec.extend(_hwp_rec(0x4C, 2, _hwp_ctrl_id("$pic") + b"\x00" * 32))
    scan_pic_pl = bytearray(76)
    struct.pack_into("<H", scan_pic_pl, 71, 2)  # bin_id = 2
    sec.extend(_hwp_rec(0x55, 3, bytes(scan_pic_pl)))

    streams = {
        "FileHeader": bytes(fh),
        "DocInfo": _deflate_raw(bytes(di)),
        "BodyText/Section0": _deflate_raw(bytes(sec)),
        "BinData/BIN0001.png": small_png,
        "BinData/BIN0002.png": scan_png,
        "\x05HwpSummaryInformation": _make_ole_summary_stream(
            title="HWP 5.0 통합 테스트 문서",
            author="홍길동",
            creator="Hancom Office Hangul",
        ),
    }
    return _write_cfb_v3(path, streams)


# ---------------------------------------------------------------------------
# MS-DOC (.doc) & MS-PPT (.ppt) 합성 바이너리 헬퍼
# ---------------------------------------------------------------------------


def _wrap_officeart_png_blip(png_bytes: bytes) -> bytes:
    """PNG 바이트를 OfficeArt `BlipPNG`(0xF01E, 1-UID 17바이트 헤더) 레코드로 감싼다."""
    # ver=0, inst=0x6E0 (single UID), recType=0xF01E
    ver_inst = (0x6E0 << 4) | 0x0
    payload = b"\xab" * 16 + b"\xff" + png_bytes
    return struct.pack("<HHI", ver_inst, 0xF01E, len(payload)) + payload


def _build_rich_sample_doc(path: Path, *, encrypted: bool = False) -> Path:
    """MS-DOC 97-2003 합성 바이너리(CP1252+UTF-16LE 피스, 표, 하이퍼링크, 각주, 머리글, BLIP)."""
    # Piece 1 (CP1252 compressed): Title + Hyperlink field + Table (2x2)
    p1_str = (
        "1. Executive Summary\r"
        'See \x13 HYPERLINK "https://autoetl.example.kr/doc" \x14Official Spec\x15.\r'
        "Metric\x07Value\x07\x07"
        "Accuracy\x0799.5%\x07\x07"
    )
    # Piece 2 (UTF-16LE): Korean body + Footnote subdoc + Header subdoc
    body_ko = "구형 워드 바이너리 한글 본문 문단입니다.\r"
    ftn_str = "각주 1: MS-DOC 피스 테이블 직접 복원\r"
    hdd_str = "2026 구형 문서 호환성 보고서\r"

    p1_bytes = p1_str.encode("cp1252")
    p2_str = body_ko + ftn_str + hdd_str
    p2_bytes = p2_str.encode("utf-16le")

    ccp_text = len(p1_str) + len(body_ko)
    ccp_ftn = len(ftn_str)
    ccp_hdd = len(hdd_str)
    total_cp = ccp_text + ccp_ftn + ccp_hdd

    # FIB: 32B base + 2B csw + 28B rgW + 2B cslw + 88B rgLw + 2B cbFcLcb + 744B rgFcLcb
    csw = 14
    cslw = 22
    cb_fclcb = 93
    text_offset = 1024  # aligned offset inside WordDocument

    word_doc = bytearray(text_offset)
    struct.pack_into("<H", word_doc, 0, 0xA5EC)  # wIdent
    flags = 0x0200 | (0x0100 if encrypted else 0)  # fWhichTblStm=1 (1Table)
    struct.pack_into("<H", word_doc, 10, flags)
    struct.pack_into("<H", word_doc, 32, csw)
    cslw_off = 34 + csw * 2
    struct.pack_into("<H", word_doc, cslw_off, cslw)
    lw_base = cslw_off + 2
    struct.pack_into("<i", word_doc, lw_base + 3 * 4, ccp_text)
    struct.pack_into("<i", word_doc, lw_base + 4 * 4, ccp_ftn)
    struct.pack_into("<i", word_doc, lw_base + 5 * 4, ccp_hdd)

    fclcb_cnt_off = lw_base + cslw * 4
    struct.pack_into("<H", word_doc, fclcb_cnt_off, cb_fclcb)
    fclcb_base = fclcb_cnt_off + 2

    p1_off = text_offset
    p2_off = p1_off + len(p1_bytes)
    if p2_off % 2 == 1:
        p1_bytes += b"\x00"
        p2_off += 1
    word_doc.extend(p1_bytes)
    word_doc.extend(p2_bytes)

    # 1Table: Clx = 0x02 + uint32 len + PlcPcd (3 CPs + 2 PCDs of 8B each)
    cp_arr = struct.pack("<3I", 0, len(p1_str), total_cp)
    fc1 = (p1_off * 2) | 0x40000000  # compressed flag bit 30
    fc2 = p2_off  # UTF-16LE
    pcd1 = struct.pack("<HIH", 0, fc1, 0)
    pcd2 = struct.pack("<HIH", 0, fc2, 0)
    plc_pcd = cp_arr + pcd1 + pcd2
    clx = b"\x02" + struct.pack("<I", len(plc_pcd)) + plc_pcd
    table_stm = clx

    struct.pack_into("<II", word_doc, fclcb_base + 33 * 8, 0, len(clx))

    streams = {
        "WordDocument": bytes(word_doc),
        "1Table": table_stm,
        "Data": _wrap_officeart_png_blip(_make_solid_png(110, 70, (180, 70, 40))),
        "\x05SummaryInformation": _make_ole_summary_stream(
            title="Legacy Word Report",
            author="Kim Analyst",
            creator="Microsoft Word 97-2003",
        ),
    }
    return _write_cfb_v3(path, streams)


def _ppt_rec(ver: int, inst: int, rec_type: int, payload: bytes) -> bytes:
    vi = (ver & 0x0F) | ((inst & 0x0FFF) << 4)
    return struct.pack("<HHI", vi, rec_type, len(payload)) + payload


def _build_rich_sample_ppt(
    path: Path,
    *,
    encrypted_token: bool = False,
    encrypted_stream: bool = False,
) -> Path:
    """MS-PPT 97-2003 합성 바이너리(2슬라이드, 노트, 머리글/바닥글, Escher 2x2 표, BLIP)."""
    # Slide 1: per-slide HeadersFooters + Escher 2x2 table
    s1_hf = _ppt_rec(
        0xF,
        0,
        0x0FD9,
        _ppt_rec(0, 0, 0x0FDA, struct.pack("<HH", 0, 0x25))
        + _ppt_rec(0, 0, 0x0FBA, "2026-11-23".encode("utf-16le"))
        + _ppt_rec(0, 2, 0x0FBA, "Slide 1 Custom Footer".encode("utf-16le")),
    )

    # Escher table SpgrContainer (fIsTable pid=927 val=1) with 2x2 cells
    tbl_hdr_opt = _ppt_rec(3, 1, 0xF00B, struct.pack("<HI", 927, 1))
    tbl_hdr_sp = _ppt_rec(0xF, 0, 0xF004, tbl_hdr_opt)
    cell_specs = [
        (400, 800, 2400, 1200, "항목"),
        (2400, 800, 4400, 1200, "실적"),
        (400, 1200, 2400, 1600, "매출"),
        (2400, 1200, 4400, 1600, "120억"),
    ]
    cell_sps = bytearray()
    for x0, y0, x1, y1, txt in cell_specs:
        anchor = _ppt_rec(0, 0, 0xF00F, struct.pack("<4i", x0, y0, x1, y1))
        tbox = _ppt_rec(
            0xF,
            0,
            0xF00D,
            _ppt_rec(0, 0, 0x0F9F, struct.pack("<I", 4))
            + _ppt_rec(0, 0, 0x0FA0, txt.encode("utf-16le")),
        )
        cell_sps.extend(_ppt_rec(0xF, 0, 0xF004, anchor + tbox))
    tbl_spgr = _ppt_rec(0xF, 0, 0xF003, tbl_hdr_sp + bytes(cell_sps))
    s1_ppd = _ppt_rec(0xF, 0, 0x040C, _ppt_rec(0xF, 0, 0xF002, _ppt_rec(0xF, 0, 0xF003, tbl_spgr)))
    slide1_rec = _ppt_rec(
        0xF,
        0,
        0x03EE,
        _ppt_rec(2, 0, 0x03EF, struct.pack("<5I", 0, 0, 0, 0, 256)) + s1_hf + s1_ppd,
    )

    # Slide 2: inherits global footer
    slide2_rec = _ppt_rec(
        0xF,
        0,
        0x03EE,
        _ppt_rec(2, 0, 0x03EF, struct.pack("<5I", 0, 0, 0, 0, 0)),
    )

    # Document container (persist 1)
    doc_atom = _ppt_rec(1, 0, 0x03E9, struct.pack("<ii", 5760, 4320) + b"\x00" * 32)
    global_hf = _ppt_rec(
        0xF,
        3,
        0x0FD9,
        _ppt_rec(0, 0, 0x0FDA, struct.pack("<HH", 0, 0x30))
        + _ppt_rec(0, 1, 0x0FBA, "Global Slide Header".encode("utf-16le"))
        + _ppt_rec(0, 2, 0x0FBA, "Global Slide Footer".encode("utf-16le")),
    )
    # MainMaster SlideListWithText (inst=1) — 반드시 무시되어야 함!
    master_slwt = _ppt_rec(
        0xF,
        1,
        0x0FF0,
        _ppt_rec(0, 0, 0x03F3, struct.pack("<5I", 99, 0, 0, 999, 0))
        + _ppt_rec(0, 0, 0x0FA8, b"Click to edit Master title style"),
    )
    # Slides SlideListWithText (inst=0)
    slides_slwt = _ppt_rec(
        0xF,
        0,
        0x0FF0,
        _ppt_rec(0, 0, 0x03F3, struct.pack("<5I", 2, 0, 0, 256, 0))
        + _ppt_rec(0, 0, 0x0F9F, struct.pack("<I", 0))  # title
        + _ppt_rec(0, 0, 0x0FA0, "분분기 실적 요약".encode("utf-16le"))
        + _ppt_rec(0, 0, 0x0F9F, struct.pack("<I", 1))  # body
        + _ppt_rec(0, 0, 0x0FA0, "핵심 성과 지표 달성".encode("utf-16le"))
        + _ppt_rec(0, 0, 0x03F3, struct.pack("<5I", 3, 0, 0, 257, 0))
        + _ppt_rec(0, 0, 0x0F9F, struct.pack("<I", 0))
        + _ppt_rec(0, 0, 0x0FA8, b"Next Quarter Roadmap")
        + _ppt_rec(0, 0, 0x0F9F, struct.pack("<I", 1))
        + _ppt_rec(0, 0, 0x0FA8, b"Expand legacy binary support"),
    )
    # Notes SlideListWithText (inst=2)
    notes_slwt = _ppt_rec(
        0xF,
        2,
        0x0FF0,
        _ppt_rec(0, 0, 0x03F3, struct.pack("<5I", 4, 0, 0, 256, 0))
        + _ppt_rec(0, 0, 0x0FA0, "발표자 노트: 표 수치를 강조할 것".encode("utf-16le")),
    )
    doc_rec = _ppt_rec(
        0xF,
        0,
        0x03E8,
        doc_atom + global_hf + master_slwt + slides_slwt + notes_slwt,
    )

    # Offsets inside PowerPoint Document
    doc_off = 0
    s1_off = len(doc_rec)
    s2_off = s1_off + len(slide1_rec)
    pdir_off = s2_off + len(slide2_rec)

    # PersistDirectoryAtom: pid 1..3 -> doc_off, s1_off, s2_off
    pdir_payload = struct.pack("<I3I", (3 << 20) | 1, doc_off, s1_off, s2_off)
    pdir_rec = _ppt_rec(0, 0, 0x1772, pdir_payload)
    edit_off = pdir_off + len(pdir_rec)

    # UserEditAtom (28 bytes payload)
    ue_payload = struct.pack("<IIIIIII", 257, 0x03000000, 0, pdir_off, 1, 0, 0)
    ue_rec = _ppt_rec(0, 0, 0x0FF5, ue_payload)

    ppt_doc = doc_rec + slide1_rec + slide2_rec + pdir_rec + ue_rec

    # Current User stream (record header 8B + 20B atom)
    token = 0xF3D1C4DF if encrypted_token else 0xE391C05F
    cu_atom = struct.pack("<IIIIHH", 20, token, edit_off, 4, 0x03F4, 0) + b"user"
    cu_stream = _ppt_rec(0, 0, 0x0FF6, cu_atom)

    streams: dict[str, bytes] = {
        "PowerPoint Document": ppt_doc,
        "Current User": cu_stream,
        "Pictures": _wrap_officeart_png_blip(_make_solid_png(140, 90, (50, 160, 80))),
        "\x05SummaryInformation": _make_ole_summary_stream(
            title="Legacy Deck Summary",
            author="Park Presenter",
            creator="Microsoft PowerPoint 97-2003",
        ),
    }
    if encrypted_stream:
        streams["EncryptedSummary"] = b"encrypted"
    return _write_cfb_v3(path, streams)


# ---------------------------------------------------------------------------
# 테스트 케이스
# ---------------------------------------------------------------------------


def test_legacy_extensions_registered() -> None:
    """`.hwp`, `.doc`, `.ppt` 어댑터가 기본 파이프라인 레지스트리에 등록되어 있다."""
    exts = supported_extensions()
    for ext in (".hwp", ".doc", ".ppt"):
        assert ext in exts
    assert isinstance(get_adapter_for_path(Path("a.hwp")), HwpDocumentAdapter)
    assert isinstance(get_adapter_for_path(Path("a.doc")), DocDocumentAdapter)
    assert isinstance(get_adapter_for_path(Path("a.ppt")), PptDocumentAdapter)
    assert get_adapter_for_path(Path("a.hwp")).format == SourceFormat.HWP
    assert get_adapter_for_path(Path("a.doc")).format == SourceFormat.DOC
    assert get_adapter_for_path(Path("a.ppt")).format == SourceFormat.PPT


def test_cfb_v3_reader_minifat_fat_summary_and_blips(tmp_path: Path) -> None:
    """`CfbFile`이 MiniFAT(<4KB)·FAT(>=4KB)·중첩 스트림·SummaryInfo·BLIP을 정확히 읽는다."""
    png = _make_solid_png(64, 48, (10, 80, 150))
    big_stream = b"AUTOETL-FAT-DATA" * 400  # 6400B >= 4096B -> regular FAT
    small_stream = b"mini-sector-payload"
    cfb_path = _write_cfb_v3(
        tmp_path / "sample.ole",
        {
            "BodyText/Section0": big_stream,
            "Small": small_stream,
            "Pictures": _wrap_officeart_png_blip(png),
            "\x05SummaryInformation": _make_ole_summary_stream(
                title="테스트 제목",
                author="작성자A",
                creator="TestApp 1.0",
            ),
        },
    )

    with CfbFile(cfb_path) as cfb:
        names = cfb.list_streams()
        assert "BodyText/Section0" in names
        assert "Small" in names
        assert cfb.read_stream("BodyText/Section0") == big_stream
        assert cfb.read_stream("small") == small_stream  # case-insensitive lookup

        summary = read_ole_summary_info(cfb, ("\x05SummaryInformation",))
        assert summary.title == "테스트 제목"
        assert summary.author == "작성자A"
        assert summary.creator_app == "TestApp 1.0"

        blips = extract_officeart_blips(cfb.read_stream("Pictures") or b"")
        assert len(blips) == 1
        assert blips[0].extension == ".png"
        assert blips[0].pixel_width == 64
        assert blips[0].pixel_height == 48
        assert blips[0].raw_bytes == png

    # 비 OLE2 파일 및 존재하지 않는 파일 가드
    bad_file = tmp_path / "not_ole.hwp"
    bad_file.write_bytes(b"NOT-AN-OLE2-FILE" * 64)
    with pytest.raises(ParserInputError, match="OLE2"):
        CfbFile(bad_file)
    with pytest.raises(ParserInputError, match="존재하지 않는다"):
        CfbFile(tmp_path / "missing.hwp")


def test_hwp_native_rich_document_end_to_end(tmp_path: Path) -> None:
    """`rhwp` 기반 HWP 어댑터가 개요·번호·수식·각주·병합표·그림·레이아웃 분할을 처리한다."""
    from autoetl.parser import RhwpPageRenderer

    hwp_path = _build_rich_sample_hwp(tmp_path / "rich.hwp")
    detector = HeuristicRasterLayoutDetector()

    plan = inspect_document(hwp_path, layout_detector=detector)
    assert plan.document.format == SourceFormat.HWP
    assert plan.document.title == "HWP 5.0 통합 테스트 문서"
    assert plan.document.author == "홍길동"
    assert plan.document.creator is not None and plan.document.creator.startswith("rhwp/")
    assert plan.summary.total_pages == 2
    assert any(o.title == "1. 구형 바이너리 호환성 개요" for o in plan.outline)
    assert plan.pages[1].category.value == "scanned_image"
    assert any(f"레이아웃 탐지({detector.name})" in r for r in plan.pages[1].reasons)

    out_dir = tmp_path / "out_hwp"
    doc, manifest = parse_and_export(
        hwp_path,
        out_dir,
        options=ParseOptions(include_comments=True, extract_crops=True),
        layout_detector=detector,
    )
    assert manifest.parsed_json.is_file()
    assert manifest.document_markdown.is_file()

    p1 = doc.pages[0]
    joined_p1 = "\n".join(b.text for b in p1.blocks)
    assert "1. 구형 바이너리 호환성 개요" in joined_p1
    assert "첫 번째 번호 항목" in joined_p1
    assert "두 번째 번호 항목" in joined_p1
    assert "공식 문서를 참고한다" in joined_p1
    # 각주(1p) · 미주(마지막 2p)
    assert "[각주 1]" in joined_p1 and "HWP 5.0 바이너리 공개 명세" in joined_p1
    joined_p2 = "\n".join(b.text for b in doc.pages[1].blocks)
    assert "[미주 1]" in joined_p2 and "CFB 섹터 배치" in joined_p2
    # 수식 & 코드 블록
    assert any("a + b" in b.text for b in p1.blocks)
    assert any("uv run autoetl parse notice.hwp" in b.text for b in p1.blocks)
    # 머리글·바닥글 분리
    hf_text = " ".join(b.text for b in p1.header_footer_blocks)
    assert "2026년도 구형 문서 파서 기술서" in hf_text
    assert "Auto ETL 표준 플랫폼" in hf_text
    # 병합 표 + 캡션
    assert len(p1.tables) == 1
    tbl = p1.tables[0]
    assert tbl.caption == "표 1. 구형 형식 지원 현황"
    assert tbl.has_merged_cells is True
    assert tbl.headers == ["형식", "지원 전략", ""]
    assert tbl.rows == [["HWP 5.0", "네이티브", "무의존성"]]
    # Page 2 스캔 그림 + 레이아웃 탐지 분할 + RhwpPageRenderer 네이티브 PNG 렌더링
    assert len(doc.pages[1].visual_regions) >= 2
    assert all(
        v.crop_status in (CropStatus.EMBEDDED_EXTRACTED, CropStatus.SAVED)
        for v in doc.pages[1].visual_regions
    )
    rendered = RhwpPageRenderer().render_region(hwp_path, 1, None, dpi=96)
    assert rendered is not None
    assert rendered.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_hwp_security_guards(tmp_path: Path) -> None:
    """암호화·DRM·배포용(ViewText)·손상된 FileHeader 및 위장 HWPX 문서를 안전하게 차단한다."""
    for name, flags, extra_streams, match_pat in (
        ("enc.hwp", 0x02, {"BodyText/Section0": b""}, "암호화"),
        ("drm.hwp", 0x10, {"BodyText/Section0": b""}, "DRM"),
        # 배포용 문서는 실제 한컴 파일처럼 더미 BodyText/Section0과 ViewText/Section0을 동시에 보유
        (
            "dist.hwp",
            0x05,
            {"BodyText/Section0": _deflate_raw(b""), "ViewText/Section0": b"encrypted"},
            "배포용",
        ),
        ("no_sec.hwp", 0x00, {}, "BodyText/Section"),
    ):
        fh = bytearray(256)
        fh[0:17] = b"HWP Document File"
        struct.pack_into("<I", fh, 36, flags)
        p = _write_cfb_v3(tmp_path / name, {"FileHeader": bytes(fh), **extra_streams})
        with pytest.raises(ParserInputError, match=match_pat):
            parse_document(p)

    bad_fh = _write_cfb_v3(tmp_path / "bad_fh.hwp", {"FileHeader": b"WRONG HEADER" * 10})
    with pytest.raises(ParserInputError, match="FileHeader"):
        parse_document(bad_fh)

    # .hwp 확장자를 쓴 암호화 HWPX(ZIP)도 HWPX 가드를 거쳐 차단되어야 함
    disguised_enc = tmp_path / "disguised.hwp"
    with zipfile.ZipFile(disguised_enc, "w") as zf:
        zf.writestr(
            "META-INF/manifest.xml",
            "<manifest><file-entry><encryption-data/></file-entry></manifest>",
        )
        zf.writestr("Contents/section0.xml", "<hs:sec/>")
    with pytest.raises(ParserInputError, match="암호화"):
        parse_document(disguised_enc)

    # OLE2도 ZIP도 아닌 위조 바이트 차단
    garbage = tmp_path / "garbage.hwp"
    garbage.write_bytes(b"NOT-AN-HWP-OR-ZIP-FILE")
    with pytest.raises(ParserInputError, match="OLE2"):
        parse_document(garbage)


def test_doc_native_tier2_rich_document_and_security_guard(tmp_path: Path) -> None:
    """Tier-2 순수 Python MS-DOC 파서가 한글/ASCII 피스·표·하이퍼링크·각주·PNG·가드를 처리한다."""
    doc_path = _build_rich_sample_doc(tmp_path / "report.doc")
    adapter = DocDocumentAdapter(use_libreoffice=False)

    parsed = adapter.parse(doc_path, options=ParseOptions(extract_crops=False))
    assert parsed.document.format == SourceFormat.DOC
    assert parsed.document.title == "Legacy Word Report"
    assert parsed.document.author == "Kim Analyst"
    assert len(parsed.pages) == 1

    p1 = parsed.pages[0]
    joined = "\n".join(b.text for b in p1.blocks)
    assert "1. Executive Summary" in joined
    assert "Official Spec (https://autoetl.example.kr/doc)" in joined
    assert "구형 워드 바이너리 한글 본문 문단입니다." in joined
    assert "MS-DOC 피스 테이블 직접 복원" in joined
    assert any("2026 구형 문서 호환성 보고서" in b.text for b in p1.header_footer_blocks)
    assert len(p1.tables) == 1
    assert p1.tables[0].headers == ["Metric", "Value"]
    assert p1.tables[0].rows == [["Accuracy", "99.5%"]]
    assert len(p1.visual_regions) == 1
    assert p1.visual_regions[0].image_width_px == 110

    # 암호화된 .doc 차단
    enc_path = _build_rich_sample_doc(tmp_path / "enc.doc", encrypted=True)
    with pytest.raises(ParserInputError, match="암호화"):
        adapter.parse(enc_path, options=ParseOptions())


def test_doc_tier1_libreoffice_upgrade_preserves_doc_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LibreOffice `.doc`→`.docx` 승격 후 원본 `SourceFormat.DOC`·파일명·SHA-256을 보존한다."""
    doc_path = _build_rich_sample_doc(tmp_path / "upgrade_src.doc")
    orig_sha = hashlib.sha256(doc_path.read_bytes()).hexdigest()

    def fake_convert(source_path: Path, target_ext: str, out_dir: Path, **kwargs: object) -> Path:
        assert target_ext == "docx"
        out_docx = out_dir / f"{source_path.stem}.docx"
        with zipfile.ZipFile(out_docx, "w") as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                "</Types>",
            )
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>LibreOffice 승격 본문</w:t></w:r></w:p></w:body>"
                "</w:document>",
            )
        return out_docx

    monkeypatch.setattr(
        "autoetl.parser.legacy.doc.find_libreoffice_binary", lambda _b: "/usr/bin/soffice"
    )
    monkeypatch.setattr("autoetl.parser.legacy.doc.convert_legacy_office_document", fake_convert)

    adapter = DocDocumentAdapter(use_libreoffice=True)
    parsed = adapter.parse(doc_path, options=ParseOptions(extract_crops=False))
    assert parsed.document.format == SourceFormat.DOC
    assert parsed.document.source_path == "upgrade_src.doc"
    assert parsed.document.sha256 == orig_sha
    assert parsed.routing_plan.document.format == SourceFormat.DOC
    assert any("LibreOffice 승격 본문" in b.text for b in parsed.pages[0].blocks)


def test_ppt_native_tier2_rich_deck_tables_headers_and_security_guard(tmp_path: Path) -> None:
    """Tier-2 MS-PPT 파서가 슬라이드·노트·머리글/바닥글·Escher 표·BLIP·암호화 가드를 처리한다."""
    ppt_path = _build_rich_sample_ppt(tmp_path / "deck.ppt")
    adapter = PptDocumentAdapter(use_libreoffice=False)

    parsed = adapter.parse(ppt_path, options=ParseOptions(extract_crops=False))
    assert parsed.document.format == SourceFormat.PPT
    assert parsed.document.title == "Legacy Deck Summary"
    assert parsed.document.author == "Park Presenter"
    assert len(parsed.pages) == 2

    # MainMaster 플레이스홀더 텍스트가 본문에 혼입되지 않았는지 확인
    all_text = "\n".join(b.text for pg in parsed.pages for b in pg.blocks)
    assert "Click to edit Master title style" not in all_text
    assert "분분기 실적 요약" in all_text
    assert "핵심 성과 지표 달성" in all_text
    assert "발표자 노트: 표 수치를 강조할 것" in all_text
    assert "Next Quarter Roadmap" in all_text

    # Slide 1: Escher 2x2 표 + per-slide 바닥글/날짜 + 전역 머리글 + Pictures BLIP 이미지
    s1 = parsed.pages[0]
    assert len(s1.tables) == 1
    assert s1.tables[0].headers == ["항목", "실적"]
    assert s1.tables[0].rows == [["매출", "120억"]]
    s1_hf = " ".join(b.text for b in s1.header_footer_blocks)
    assert "Global Slide Header" in s1_hf
    assert "Slide 1 Custom Footer" in s1_hf
    assert "2026-11-23" in s1_hf
    assert len(s1.visual_regions) == 1
    assert s1.visual_regions[0].image_width_px == 140

    # Slide 2: 전역 머리글/바닥글 상속
    s2 = parsed.pages[1]
    s2_hf = " ".join(b.text for b in s2.header_footer_blocks)
    assert "Global Slide Header" in s2_hf
    assert "Global Slide Footer" in s2_hf

    # 암호화된 .ppt 차단 (Current User 토큰 & EncryptedSummary 스트림)
    enc1 = _build_rich_sample_ppt(tmp_path / "enc_tok.ppt", encrypted_token=True)
    enc2 = _build_rich_sample_ppt(tmp_path / "enc_stm.ppt", encrypted_stream=True)
    with pytest.raises(ParserInputError, match="암호화"):
        adapter.parse(enc1, options=ParseOptions())
    with pytest.raises(ParserInputError, match="암호화"):
        adapter.parse(enc2, options=ParseOptions())


def test_ppt_tier1_libreoffice_upgrade_preserves_ppt_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LibreOffice `.ppt`→`.pptx` 승격 후 원본 `SourceFormat.PPT`·파일명·SHA-256을 보존한다."""
    ppt_path = _build_rich_sample_ppt(tmp_path / "upgrade_deck.ppt")
    orig_sha = hashlib.sha256(ppt_path.read_bytes()).hexdigest()

    def fake_convert(source_path: Path, target_ext: str, out_dir: Path, **kwargs: object) -> Path:
        assert target_ext == "pptx"
        out_pptx = out_dir / f"{source_path.stem}.pptx"
        ns_p = "http://schemas.openxmlformats.org/presentationml/2006/main"
        ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
        with zipfile.ZipFile(out_pptx, "w") as zf:
            zf.writestr(
                "ppt/presentation.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<p:presentation xmlns:p="{ns_p}" xmlns:r="{ns_r}">'
                '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
                '<p:sldSz cx="9144000" cy="6858000"/>'
                "</p:presentation>",
            )
            zf.writestr(
                "ppt/_rels/presentation.xml.rels",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{ns_r}/slide" Target="slides/slide1.xml"/>'
                "</Relationships>",
            )
            zf.writestr(
                "ppt/slides/slide1.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<p:sld xmlns:p="{ns_p}" xmlns:a="{ns_a}">'
                "<p:cSld><p:spTree><p:sp>"
                '<p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/>'
                '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="457200" y="274320"/>'
                '<a:ext cx="8229600" cy="1143000"/></a:xfrm></p:spPr>'
                "<p:txBody><a:bodyPr/><a:lstStyle/>"
                "<a:p><a:r><a:t>LibreOffice 승격 슬라이드</a:t></a:r></a:p>"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
        return out_pptx

    monkeypatch.setattr(
        "autoetl.parser.legacy.ppt.find_libreoffice_binary", lambda _b: "/usr/bin/soffice"
    )
    monkeypatch.setattr("autoetl.parser.legacy.ppt.convert_legacy_office_document", fake_convert)

    adapter = PptDocumentAdapter(use_libreoffice=True)
    parsed = adapter.parse(ppt_path, options=ParseOptions(extract_crops=False))
    assert parsed.document.format == SourceFormat.PPT
    assert parsed.document.source_path == "upgrade_deck.ppt"
    assert parsed.document.sha256 == orig_sha
    assert any("LibreOffice 승격 슬라이드" in b.text for b in parsed.pages[0].blocks)


def test_cli_parse_legacy_formats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`autoetl parse` CLI가 `.hwp`/`.doc`/`.ppt`의 `--plan-only`와 `--out`을 처리한다."""
    hwp_path = _build_rich_sample_hwp(tmp_path / "cli.hwp")
    doc_path = _build_rich_sample_doc(tmp_path / "cli.doc")
    ppt_path = _build_rich_sample_ppt(tmp_path / "cli.ppt")

    for src, fmt_tag, expected_text in (
        (hwp_path, "hwp", "1. 구형 바이너리 호환성 개요"),
        (doc_path, "doc", "구형 워드 바이너리 한글 본문"),
        (ppt_path, "ppt", "분분기 실적 요약"),
    ):
        rc_plan = main(["parse", str(src), "--plan-only"])
        assert rc_plan == 0
        plan_out = capsys.readouterr().out
        assert fmt_tag in plan_out.lower()

        out_dir = tmp_path / f"bundle_{fmt_tag}"
        rc_parse = main(["parse", str(src), "--include-comments", "--out", str(out_dir)])
        assert rc_parse == 0
        capsys.readouterr()
        assert (out_dir / "parsed.json").is_file()
        assert (out_dir / "document.md").is_file()
        md = (out_dir / "document.md").read_text(encoding="utf-8")
        assert expected_text in md
