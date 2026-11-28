"""MS-CFB 리더(`autoetl.parser.legacy.cfb`) 오류·빈 입력·경계 값 엣지 테스트.

유효한 합성 CFB를 만든 뒤 헤더/디렉터리 바이트를 고쳐 손상 경로를 검증하고,
PropertySet 메타데이터·OfficeArt BLIP 추출의 드문 분기를 작은 입력으로 확인한다.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.image_io import encode_png
from autoetl.parser.legacy.cfb import (
    MAX_CFB_FILE_BYTES,
    CfbFile,
    OleSummaryInfo,
    _decode_ole_prop_string,
    _wrap_dib_as_bmp,
    extract_officeart_blips,
    read_ole_summary_info,
)

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
    buf[67] = 1
    struct.pack_into("<III", buf, 68, left_id, right_id, child_id)
    struct.pack_into("<IQ", buf, 116, start_sector, stream_size)
    return bytes(buf)


def _write_cfb_v3(path: Path, streams: dict[str, bytes]) -> Path:
    """스트림 맵으로 유효한 OLE2 CFB v3 파일을 생성하는 자체 합성 라이터."""
    root_children: dict[str, dict[str, object]] = {}
    for full_path, data in streams.items():
        parts = full_path.split("/")
        cur = root_children
        for seg in parts[:-1]:
            if seg not in cur:
                cur[seg] = {"type": 1, "children": {}, "data": b""}
            cur = cur[seg]["children"]  # type: ignore[assignment]
        cur[parts[-1]] = {"type": 2, "children": {}, "data": data}

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

    mini_container_bytes = b"".join(mini_stream_chunks)
    if mini_container_bytes:
        start_sec_map[0] = _alloc_chain(mini_container_bytes)
        size_map[0] = len(mini_container_bytes)
    else:
        start_sec_map[0] = _ENDOFCHAIN
        size_map[0] = 0

    first_minifat_sec = _ENDOFCHAIN
    num_minifat_secs = 0
    if mini_fat:
        mf_bytes = b"".join(struct.pack("<I", v) for v in mini_fat)
        rem = len(mf_bytes) % _SECTOR_SIZE
        if rem:
            mf_bytes += struct.pack("<I", _FREESECT) * ((_SECTOR_SIZE - rem) // 4)
        first_minifat_sec = _alloc_chain(mf_bytes)
        num_minifat_secs = len(mf_bytes) // _SECTOR_SIZE

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

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HH", header, 24, 0x003E, 0x0003)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<HH", header, 30, 9, 6)
    struct.pack_into("<I", header, 44, num_fat_secs)
    struct.pack_into("<I", header, 48, first_dir_sec)
    struct.pack_into("<I", header, 56, _MINI_CUTOFF)
    struct.pack_into("<I", header, 60, first_minifat_sec)
    struct.pack_into("<I", header, 64, num_minifat_secs)
    struct.pack_into("<I", header, 68, _ENDOFCHAIN)
    struct.pack_into("<I", header, 72, 0)
    for i in range(109):
        val = fat_sec_ids[i] if i < len(fat_sec_ids) else _FREESECT
        struct.pack_into("<I", header, 76 + i * 4, val)

    path.write_bytes(bytes(header) + b"".join(sectors))
    return path


def _make_ole_summary_stream(*, title: str = "", author: str = "", creator: str = "") -> bytes:
    props: list[tuple[int, bytes]] = []
    props.append((1, struct.pack("<IH", 2, 65001) + b"\x00\x00"))
    for pid, val in ((2, title), (4, author), (18, creator)):
        if val:
            enc = val.encode("utf-8") + b"\x00"
            pad = (4 - (len(enc) % 4)) % 4
            props.append((pid, struct.pack("<II", 30, len(enc)) + enc + b"\x00" * pad))

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
    struct.pack_into("<I", hdr, 24, 1)
    hdr[28:44] = b"\xe0\x85\x9f\xf2\xf9\x4f\x68\x10\xab\x91\x08\x00\x2b\x27\xb3\xd9"
    struct.pack_into("<I", hdr, 44, sec_off)
    return bytes(hdr) + bytes(sec)


def _wrap_officeart_png_blip(png_bytes: bytes) -> bytes:
    ver_inst = (0x6E0 << 4) | 0x0
    payload = b"\xab" * 16 + b"\xff" + png_bytes
    return struct.pack("<HHI", ver_inst, 0xF01E, len(payload)) + payload


def _png(width: int = 32, height: int = 24) -> bytes:
    return encode_png(bytes((70, 110, 150)) * width * height, width, height)


def _patch(path: Path, offset: int, data: bytes) -> None:
    raw = bytearray(path.read_bytes())
    raw[offset : offset + len(data)] = data
    path.write_bytes(bytes(raw))


# ---------------------------------------------------------------------------
# 열기 가드: 없음 · 디렉터리 · 크기 · 읽기 오류 · 헤더 손상
# ---------------------------------------------------------------------------


def test_cfb_missing_path_and_directory(tmp_path: Path) -> None:
    with pytest.raises(ParserInputError, match="존재하지 않는다"):
        CfbFile(tmp_path / "missing.hwp")
    with pytest.raises(ParserInputError, match="파일이 아니다"):
        CfbFile(tmp_path)


def test_cfb_too_small_and_too_large(tmp_path: Path) -> None:
    small = tmp_path / "small.ole"
    small.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(ParserInputError, match="크기 부족"):
        CfbFile(small)

    big = tmp_path / "big.ole"
    with big.open("wb") as fh:
        fh.truncate(MAX_CFB_FILE_BYTES + 1)
    with pytest.raises(ParserInputError, match="최대 허용 크기"):
        CfbFile(big)


def test_cfb_read_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_cfb_v3(tmp_path / "ok.ole", {"S": b"data"})

    def _raise(self: Path) -> bytes:
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    with pytest.raises(ParserInputError, match="읽을 수 없다"):
        CfbFile(path)


def test_cfb_bad_byte_order_and_sector_shifts(tmp_path: Path) -> None:
    base = _write_cfb_v3(tmp_path / "base.ole", {"S": b"data"})
    raw = base.read_bytes()

    bad_order = tmp_path / "order.ole"
    bad_order.write_bytes(raw)
    _patch(bad_order, 28, struct.pack("<H", 0x0000))
    with pytest.raises(ParserInputError, match="바이트 순서"):
        CfbFile(bad_order)

    bad_shift = tmp_path / "shift.ole"
    bad_shift.write_bytes(raw)
    _patch(bad_shift, 30, struct.pack("<H", 10))
    with pytest.raises(ParserInputError, match="섹터 크기"):
        CfbFile(bad_shift)

    bad_mini = tmp_path / "mini.ole"
    bad_mini.write_bytes(raw)
    _patch(bad_mini, 32, struct.pack("<H", 13))
    with pytest.raises(ParserInputError, match="섹터 크기"):
        CfbFile(bad_mini)


def test_cfb_root_entry_missing(tmp_path: Path) -> None:
    path = _write_cfb_v3(tmp_path / "root.ole", {"S": b"data"})
    raw = path.read_bytes()
    dir_sec = struct.unpack_from("<I", raw, 48)[0]
    dir_off = (dir_sec + 1) * 512
    _patch(path, dir_off + 66, b"\x00")  # root obj_type -> unknown
    with pytest.raises(ParserInputError, match="루트 디렉터리"):
        CfbFile(path)


def test_cfb_difat_extension_chain(tmp_path: Path) -> None:
    path = _write_cfb_v3(tmp_path / "difat.ole", {"S": b"x" * 5000})
    raw = bytearray(path.read_bytes())
    fat_sec = struct.unpack_from("<I", raw, 76)[0]
    new_sec = (len(raw) - 512) // 512
    entries = [fat_sec] + [_FREESECT] * 126 + [_ENDOFCHAIN]
    raw.extend(struct.pack(f"<{len(entries)}I", *entries))
    struct.pack_into("<I", raw, 68, new_sec)
    struct.pack_into("<I", raw, 72, 1)
    path.write_bytes(bytes(raw))
    with CfbFile(path) as cfb:
        assert cfb.read_stream("S") == b"x" * 5000


# ---------------------------------------------------------------------------
# 스트림 조회 · 체인 가드
# ---------------------------------------------------------------------------


def test_cfb_stream_lookup_case_backslash_and_missing(tmp_path: Path) -> None:
    path = _write_cfb_v3(tmp_path / "lookup.ole", {"BodyText/Section0": b"hello"})
    with CfbFile(path) as cfb:
        assert cfb.resolve_stream_name("bodytext\\section0") == "BodyText/Section0"
        assert cfb.resolve_stream_name("/BodyText/Section0") == "BodyText/Section0"
        assert cfb.has_stream("BODYTEXT/SECTION0")
        assert cfb.read_stream("nope") is None
        assert cfb.read_stream("BodyText/Section0") == b"hello"
        assert cfb.read_stream("BodyText/Section0", max_bytes=2) == b"he"


def test_cfb_empty_stream_and_chain_guards(tmp_path: Path) -> None:
    path = _write_cfb_v3(
        tmp_path / "guards.ole", {"Empty": b"", "Small": b"mini", "Big": b"y" * 5000}
    )
    with CfbFile(path) as cfb:
        assert cfb.read_stream("Empty") == b""
        assert cfb._read_sector(10**9) == b""
        assert cfb._read_fat_chain(_ENDOFCHAIN, max_bytes=100) == b""
        assert cfb._read_mini_chain(_ENDOFCHAIN, max_bytes=100) == b""
        assert cfb._read_fat_chain(10**6, max_bytes=100) == b""
        assert cfb.file_size == path.stat().st_size
        assert len(cfb.sha256) == 64


def test_cfb_close_releases_buffers(tmp_path: Path) -> None:
    path = _write_cfb_v3(tmp_path / "close.ole", {"S": b"data"})
    cfb = CfbFile(path)
    assert cfb.read_stream("S") == b"data"
    cfb.close()
    assert cfb._raw == b""
    assert cfb.read_stream("S") == b""


# ---------------------------------------------------------------------------
# PropertySet 문자열 디코딩 · 요약 메타데이터
# ---------------------------------------------------------------------------


def test_decode_ole_prop_string_empty_and_fallbacks() -> None:
    assert _decode_ole_prop_string(b"", "cp949") == ""
    assert _decode_ole_prop_string(b"abc\x00tail", "cp949") == "abc"
    assert _decode_ole_prop_string("한글".encode("cp949"), "cp949") == "한글"
    # 알 수 없는 인코딩 이름은 LookupError 후 utf-8/cp949/cp1252로 폴백
    assert _decode_ole_prop_string(b"plain", "no-such-codec") == "plain"
    assert _decode_ole_prop_string(b"\xff\xfe\xfd", "no-such-codec") != ""


def test_summary_info_valid_and_empty_stream(tmp_path: Path) -> None:
    path = _write_cfb_v3(
        tmp_path / "sum.ole",
        {
            "\x05SummaryInformation": _make_ole_summary_stream(
                title="제목", author="작성자", creator="App 1.0"
            )
        },
    )
    with CfbFile(path) as cfb:
        info = read_ole_summary_info(cfb)
        assert info.title == "제목"
        assert info.author == "작성자"
        assert info.creator_app == "App 1.0"

    empty = _write_cfb_v3(tmp_path / "empty.ole", {"\x05SummaryInformation": b""})
    with CfbFile(empty) as cfb:
        assert read_ole_summary_info(cfb) == OleSummaryInfo()


def _propset_header(blob: bytearray, **overrides: int) -> bytes:
    for off, val in overrides.items():
        struct.pack_into("<I", blob, int(off), val)
    return bytes(blob)


def test_summary_info_guard_paths(tmp_path: Path) -> None:
    valid = _make_ole_summary_stream(title="T")
    cases: list[bytes] = [
        valid[:40],  # too short
        _propset_header(bytearray(valid), **{"0": 0x0000}),  # bad byte order
        _propset_header(bytearray(valid), **{"24": 0}),  # zero sections
        _propset_header(bytearray(valid), **{"44": 10**6}),  # section offset beyond
    ]
    # num_props = 0 and > 512
    sec_off = struct.unpack_from("<I", valid, 44)[0]
    zero_props = bytearray(valid)
    struct.pack_into("<I", zero_props, sec_off + 4, 0)
    cases.append(bytes(zero_props))
    many_props = bytearray(valid)
    struct.pack_into("<I", many_props, sec_off + 4, 600)
    cases.append(bytes(many_props))

    for i, blob in enumerate(cases):
        p = _write_cfb_v3(tmp_path / f"guard{i}.ole", {"\x05SummaryInformation": blob})
        with CfbFile(p) as cfb:
            assert read_ole_summary_info(cfb) == OleSummaryInfo()


def test_summary_info_wide_string_and_author_fallback(tmp_path: Path) -> None:
    # VT_LPWSTR(31) 제목 + PID 8 작성자 폴백
    title_w = "넓은제목".encode("utf-16le") + b"\x00\x00"
    author = b"fallback" + b"\x00"
    props = [
        (2, struct.pack("<II", 31, len(title_w) // 2) + title_w),
        (8, struct.pack("<II", 30, len(author)) + author),
    ]
    sec_off = 48
    sec_hdr = 8 + len(props) * 8
    blobs = bytearray()
    offsets = []
    for _pid, blob in props:
        offsets.append(sec_hdr + len(blobs))
        blobs.extend(blob)
    sec = bytearray(struct.pack("<II", sec_hdr + len(blobs), len(props)))
    for (pid, _b), off in zip(props, offsets, strict=True):
        sec.extend(struct.pack("<II", pid, off))
    sec.extend(blobs)
    hdr = bytearray(48)
    struct.pack_into("<HHI", hdr, 0, 0xFFFE, 0, 0x00020501)
    struct.pack_into("<I", hdr, 24, 1)
    struct.pack_into("<I", hdr, 44, sec_off)
    blob = bytes(hdr) + bytes(sec)
    p = _write_cfb_v3(tmp_path / "wide.ole", {"\x05SummaryInformation": blob})
    with CfbFile(p) as cfb:
        info = read_ole_summary_info(cfb)
        assert info.title == "넓은제목"
        assert info.author == "fallback"


# ---------------------------------------------------------------------------
# DIB → BMP 래핑 · OfficeArt BLIP 추출
# ---------------------------------------------------------------------------


def _dib(width: int = 20, height: int = 10, bit_count: int = 24) -> bytes:
    dib = bytearray(40)
    struct.pack_into("<I", dib, 0, 40)
    struct.pack_into("<i", dib, 4, width)
    struct.pack_into("<i", dib, 8, height)
    struct.pack_into("<H", dib, 12, 1)
    struct.pack_into("<H", dib, 14, bit_count)
    dib += b"\x00" * (width * height * 3)
    return bytes(dib)


def test_wrap_dib_as_bmp_short_palette_and_truecolor() -> None:
    assert _wrap_dib_as_bmp(b"short") == b"short"
    truecolor = _wrap_dib_as_bmp(_dib(bit_count=24))
    assert truecolor.startswith(b"BM")
    assert struct.unpack_from("<I", truecolor, 10)[0] == 54  # pixel offset
    paletted = _wrap_dib_as_bmp(_dib(width=8, height=8, bit_count=8))
    assert paletted.startswith(b"BM")
    assert struct.unpack_from("<I", paletted, 10)[0] == 14 + 40 + 256 * 4


def _blip_record(rec_type: int, inst: int, img: bytes, *, two_uid: bool = False) -> bytes:
    uid = b"\xab" * 16 + b"\xff"
    if two_uid:
        uid += b"\xcd" * 16
    payload = uid + img
    return struct.pack("<HHI", (inst << 4), rec_type, len(payload)) + payload


def _fake_jpeg(width: int = 48, height: int = 32) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">H", 8) + b"\x08" + struct.pack(">HH", height, width) + b"\x01"
    return b"\xff\xd8" + sof + b"\xff\xd9"


def test_blips_empty_short_and_png(tmp_path: Path) -> None:
    assert extract_officeart_blips(b"") == []
    assert extract_officeart_blips(b"x" * 20) == []
    png = _png()
    blips = extract_officeart_blips(_wrap_officeart_png_blip(png))
    assert len(blips) == 1
    assert blips[0].extension == ".png"
    assert (blips[0].pixel_width, blips[0].pixel_height) == (32, 24)


def test_blips_jpeg_bmp_dib_and_two_uid() -> None:
    jpeg = _blip_record(0xF01D, 0x460, _fake_jpeg())
    bmp = _blip_record(0xF01F, 0x7A8, _wrap_dib_as_bmp(_dib()))
    dib = _blip_record(0xF01F, 0x7A8, _dib(width=24, height=12))
    two_uid = _blip_record(0xF01E, 0x6E1, _png(), two_uid=True)
    blips = extract_officeart_blips(jpeg + bmp + dib + two_uid)
    exts = [b.extension for b in blips]
    assert exts == [".jpg", ".bmp", ".bmp", ".png"]
    assert blips[0].pixel_width == 48
    assert blips[2].raw_bytes.startswith(b"BM")  # DIB이 BMP로 래핑됨


def test_blips_dedup_max_images_and_small_skip() -> None:
    png = _png()
    rec = _wrap_officeart_png_blip(png)
    assert len(extract_officeart_blips(rec + rec)) == 1  # 해시 중복 제거
    assert len(extract_officeart_blips(rec + rec, max_images=1)) == 1
    small = _blip_record(0xF01E, 0x6E0, _png(4, 4))
    assert extract_officeart_blips(small) == []  # 8px 미만 제외


def test_blips_container_descent_and_record_skip() -> None:
    inner = _wrap_officeart_png_blip(_png())
    container = struct.pack("<HHI", 0x000F, 0xF000, len(inner)) + inner
    assert len(extract_officeart_blips(container)) == 1
    # 비컨테이너 OfficeArt 레코드는 건너뜀
    skip = struct.pack("<HHI", 0x0000, 0xF001, 4) + b"abcd"
    assert len(extract_officeart_blips(skip + inner)) == 1
    # 알 수 없는 레코드에서 중단
    assert extract_officeart_blips(struct.pack("<HHI", 0, 0x1234, 4) + b"abcd") == []


def test_blips_truncated_payload_breaks() -> None:
    png = _png()
    rec = _wrap_officeart_png_blip(png)
    truncated = rec[:-10]  # payload_end > n
    assert extract_officeart_blips(truncated) == []


def test_blips_png_fallback_scan() -> None:
    png = _png(20, 20)
    blob = b"junk-header-bytes!" + png + b"trailing"
    blips = extract_officeart_blips(blob)
    assert len(blips) == 1
    assert blips[0].raw_bytes == png
    # 16px 미만 PNG는 폴백에서 제외
    assert extract_officeart_blips(b"junk" + _png(8, 8)) == []
    # IEND 없으면 중단
    assert extract_officeart_blips(b"junk" + png[:-8]) == []
