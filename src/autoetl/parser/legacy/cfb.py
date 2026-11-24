"""순수 Python MS-CFB(OLE2 Compound File Binary) 리더, PropertySet 메타데이터 파서 및 BLIP 추출기.

서드파티 의존성 없이 Python 표준 라이브러리(`struct`, `hashlib`, `pathlib`)만으로
구형 바이너리 문서 포맷(`.hwp`, `.doc`, `.ppt`)의 OLE2 복합 파일(v3 512B / v4 4096B 섹터)을
안전하게 열고 스트림 트리·요약 메타데이터·OfficeArt 내장 이미지를 추출한다.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

from autoetl.parser.adapters import ParserInputError
from autoetl.parser.pptx.ooxml import MAX_MEDIA_BYTES, OLE_MAGIC, probe_image_dimensions

MAX_CFB_FILE_BYTES = 128 * 1024 * 1024  # 128 MiB
MAX_CFB_STREAM_BYTES = 64 * 1024 * 1024  # 64 MiB

_MAXREGSECT = 0xFFFFFFFA
_DIFSECT = 0xFFFFFFFC
_FATSECT = 0xFFFFFFFD
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_NOSTREAM = 0xFFFFFFFF

_OBJ_UNKNOWN = 0
_OBJ_STORAGE = 1
_OBJ_STREAM = 2
_OBJ_ROOT = 5

_CODEPAGE_MAP: dict[int, str] = {
    949: "cp949",
    51949: "euc-kr",
    1200: "utf-16le",
    1201: "utf-16be",
    1252: "cp1252",
    65001: "utf-8",
    932: "cp932",
    936: "gbk",
    950: "big5",
}


@dataclass(frozen=True)
class CfbDirEntry:
    """OLE2 디렉터리 엔트리(128바이트 레코드)."""

    entry_id: int
    name: str
    obj_type: int
    left_sib_id: int
    right_sib_id: int
    child_id: int
    start_sector: int
    stream_size: int


@dataclass(frozen=True)
class OleSummaryInfo:
    """`\\x05SummaryInformation` / `\\x05HwpSummaryInformation` 문서 요약 메타데이터."""

    title: str | None = None
    subject: str | None = None
    author: str | None = None
    creator_app: str | None = None


@dataclass(frozen=True)
class OfficeArtBlip:
    """MS Office 바이너리 스트림(`Pictures`, `Data`)에서 추출한 내장 이미지 BLIP."""

    index: int
    extension: str
    raw_bytes: bytes
    pixel_width: int | None
    pixel_height: int | None


@dataclass
class CfbFile:
    """MS-CFB(OLE2) 복합 파일 읽기 전용 리더."""

    source_path: Path
    _raw: bytes = field(init=False, repr=False)
    _file_size: int = field(init=False)
    _sha256: str = field(init=False)
    _major_version: int = field(init=False)
    _sector_size: int = field(init=False)
    _mini_sector_size: int = field(init=False)
    _mini_stream_cutoff: int = field(init=False)
    _fat: list[int] = field(init=False, repr=False)
    _mini_fat: list[int] = field(init=False, repr=False)
    _mini_stream: bytes = field(init=False, repr=False)
    _streams: dict[str, CfbDirEntry] = field(init=False, default_factory=dict)
    _lower_streams: dict[str, str] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_path.exists():
            raise ParserInputError(f"바이너리 문서 파일이 존재하지 않는다: {self.source_path.name}")
        if not self.source_path.is_file():
            raise ParserInputError(f"입력 경로가 파일이 아니다: {self.source_path.name}")

        self._file_size = self.source_path.stat().st_size
        if self._file_size < 512:
            raise ParserInputError(
                f"유효한 OLE2(MS-CFB) 바이너리 문서가 아니다(크기 부족): {self.source_path.name}"
            )
        if self._file_size > MAX_CFB_FILE_BYTES:
            raise ParserInputError(
                f"바이너리 문서가 최대 허용 크기(128MB)를 초과한다: {self.source_path.name}"
            )

        try:
            self._raw = self.source_path.read_bytes()
        except OSError as exc:
            raise ParserInputError(f"파일을 읽을 수 없다: {self.source_path.name}") from exc

        if not self._raw.startswith(OLE_MAGIC):
            raise ParserInputError(
                f"유효한 OLE2(MS-CFB) 바이너리 문서가 아니다: {self.source_path.name}"
            )

        self._sha256 = hashlib.sha256(self._raw).hexdigest()
        self._parse_header_and_tables()

    def _parse_header_and_tables(self) -> None:
        hdr = self._raw[:512]
        major_ver = struct.unpack_from("<H", hdr, 26)[0]
        byte_order = struct.unpack_from("<H", hdr, 28)[0]
        if byte_order != 0xFFFE:
            raise ParserInputError(
                f"지원하지 않는 OLE2 바이트 순서다(0x{byte_order:04X}): {self.source_path.name}"
            )

        sector_shift = struct.unpack_from("<H", hdr, 30)[0]
        mini_sector_shift = struct.unpack_from("<H", hdr, 32)[0]
        if sector_shift not in (9, 12) or mini_sector_shift < 3 or mini_sector_shift > sector_shift:
            raise ParserInputError(
                f"유효하지 않은 OLE2 섹터 크기 설정이다(shift={sector_shift}): "
                f"{self.source_path.name}"
            )

        self._major_version = major_ver
        self._sector_size = 1 << sector_shift
        self._mini_sector_size = 1 << mini_sector_shift

        num_fat_sectors = struct.unpack_from("<I", hdr, 44)[0]
        first_dir_sector = struct.unpack_from("<I", hdr, 48)[0]
        self._mini_stream_cutoff = struct.unpack_from("<I", hdr, 56)[0] or 4096
        first_mini_fat_sector = struct.unpack_from("<I", hdr, 60)[0]
        num_mini_fat_sectors = struct.unpack_from("<I", hdr, 64)[0]
        first_difat_sector = struct.unpack_from("<I", hdr, 68)[0]
        num_difat_sectors = struct.unpack_from("<I", hdr, 72)[0]

        # 1. DIFAT 수집 (헤더 내 109개 + 확장 DIFAT 섹터 체인)
        difat_entries: list[int] = list(struct.unpack_from("<109I", hdr, 76))
        if first_difat_sector <= _MAXREGSECT and num_difat_sectors > 0:
            cur_difat = first_difat_sector
            visited_difat: set[int] = set()
            entries_per_sec = (self._sector_size // 4) - 1
            for _ in range(min(num_difat_sectors, 4096)):
                if cur_difat > _MAXREGSECT or cur_difat in visited_difat:
                    break
                visited_difat.add(cur_difat)
                sec_bytes = self._read_sector(cur_difat)
                if len(sec_bytes) < self._sector_size:
                    break
                vals = struct.unpack_from(f"<{entries_per_sec + 1}I", sec_bytes, 0)
                difat_entries.extend(vals[:entries_per_sec])
                cur_difat = vals[entries_per_sec]

        fat_sector_ids = [s for s in difat_entries if s <= _MAXREGSECT]
        if num_fat_sectors > 0:
            fat_sector_ids = fat_sector_ids[:num_fat_sectors]

        # 2. FAT 테이블 구성
        self._fat = []
        ints_per_sec = self._sector_size // 4
        for sec_id in fat_sector_ids:
            sec_bytes = self._read_sector(sec_id)
            if len(sec_bytes) < self._sector_size:
                break
            self._fat.extend(struct.unpack_from(f"<{ints_per_sec}I", sec_bytes, 0))

        # 3. 디렉터리 스트림 파싱
        dir_bytes = self._read_fat_chain(first_dir_sector, max_bytes=MAX_CFB_STREAM_BYTES)
        entries: list[CfbDirEntry] = []
        num_entries = len(dir_bytes) // 128
        for idx in range(num_entries):
            off = idx * 128
            raw_name = dir_bytes[off : off + 64]
            name_len = struct.unpack_from("<H", dir_bytes, off + 64)[0]
            obj_type = dir_bytes[off + 66]
            left_sib = struct.unpack_from("<I", dir_bytes, off + 68)[0]
            right_sib = struct.unpack_from("<I", dir_bytes, off + 72)[0]
            child_id = struct.unpack_from("<I", dir_bytes, off + 76)[0]
            start_sec = struct.unpack_from("<I", dir_bytes, off + 116)[0]
            size_raw = struct.unpack_from("<Q", dir_bytes, off + 120)[0]
            stream_size = (size_raw & 0xFFFFFFFF) if self._major_version == 3 else size_raw

            clamped_len = min(max(0, name_len), 64)
            if clamped_len >= 2:
                name = raw_name[: clamped_len - 2].decode("utf-16le", errors="replace")
            else:
                name = ""
            entries.append(
                CfbDirEntry(
                    entry_id=idx,
                    name=name.rstrip("\x00"),
                    obj_type=obj_type,
                    left_sib_id=left_sib,
                    right_sib_id=right_sib,
                    child_id=child_id,
                    start_sector=start_sec,
                    stream_size=stream_size,
                )
            )

        if not entries or entries[0].obj_type != _OBJ_ROOT:
            raise ParserInputError(
                f"유효한 OLE2 루트 디렉터리를 찾을 수 없다: {self.source_path.name}"
            )

        # 4. MiniFAT 및 MiniStream 구성
        self._mini_fat = []
        if first_mini_fat_sector <= _MAXREGSECT and num_mini_fat_sectors > 0:
            mf_bytes = self._read_fat_chain(
                first_mini_fat_sector,
                max_bytes=min(MAX_CFB_STREAM_BYTES, num_mini_fat_sectors * self._sector_size),
            )
            cnt = len(mf_bytes) // 4
            if cnt > 0:
                self._mini_fat = list(struct.unpack_from(f"<{cnt}I", mf_bytes, 0))

        root_entry = entries[0]
        if root_entry.start_sector <= _MAXREGSECT and root_entry.stream_size > 0:
            ms_cap = min(root_entry.stream_size, MAX_CFB_STREAM_BYTES)
            self._mini_stream = self._read_fat_chain(root_entry.start_sector, max_bytes=ms_cap)[
                :ms_cap
            ]
        else:
            self._mini_stream = b""

        # 5. 디렉터리 레드-블랙 트리 순회 -> 계층 경로 맵 구축
        visited_ids: set[int] = set()

        def _walk_tree(node_id: int, prefix: str) -> None:
            if node_id == _NOSTREAM or node_id >= len(entries) or node_id in visited_ids:
                return
            visited_ids.add(node_id)
            ent = entries[node_id]
            _walk_tree(ent.left_sib_id, prefix)
            if ent.obj_type == _OBJ_STREAM and ent.name:
                full_path = f"{prefix}/{ent.name}" if prefix else ent.name
                self._streams[full_path] = ent
                self._lower_streams[full_path.lower()] = full_path
            elif ent.obj_type == _OBJ_STORAGE and ent.name:
                sub_prefix = f"{prefix}/{ent.name}" if prefix else ent.name
                _walk_tree(ent.child_id, sub_prefix)
            _walk_tree(ent.right_sib_id, prefix)

        _walk_tree(root_entry.child_id, "")

    def _read_sector(self, sec_id: int) -> bytes:
        offset = (sec_id + 1) * self._sector_size
        if offset < 0 or offset >= len(self._raw):
            return b""
        return self._raw[offset : offset + self._sector_size]

    def _read_fat_chain(self, start_sector: int, *, max_bytes: int) -> bytes:
        if start_sector > _MAXREGSECT:
            return b""
        chunks: list[bytes] = []
        total = 0
        cur = start_sector
        visited: set[int] = set()
        fat_len = len(self._fat)
        while cur <= _MAXREGSECT and cur not in visited and total < max_bytes:
            visited.add(cur)
            sec = self._read_sector(cur)
            if not sec:
                break
            chunks.append(sec)
            total += len(sec)
            if cur >= fat_len:
                break
            nxt = self._fat[cur]
            if nxt in (_ENDOFCHAIN, _FREESECT, _FATSECT, _DIFSECT):
                break
            cur = nxt
        return b"".join(chunks)[:max_bytes]

    def _read_mini_chain(self, start_sector: int, *, max_bytes: int) -> bytes:
        if start_sector > _MAXREGSECT or not self._mini_stream:
            return b""
        chunks: list[bytes] = []
        total = 0
        cur = start_sector
        visited: set[int] = set()
        mf_len = len(self._mini_fat)
        ms_len = len(self._mini_stream)
        msz = self._mini_sector_size
        while cur <= _MAXREGSECT and cur not in visited and total < max_bytes:
            visited.add(cur)
            off = cur * msz
            if off < 0 or off >= ms_len:
                break
            chunks.append(self._mini_stream[off : off + msz])
            total += msz
            if cur >= mf_len:
                break
            nxt = self._mini_fat[cur]
            if nxt in (_ENDOFCHAIN, _FREESECT, _FATSECT, _DIFSECT):
                break
            cur = nxt
        return b"".join(chunks)[:max_bytes]

    @property
    def file_size(self) -> int:
        return self._file_size

    @property
    def sha256(self) -> str:
        return self._sha256

    def list_streams(self) -> tuple[str, ...]:
        """컨테이너 내부의 모든 스트림 경로 목록을 반환한다."""
        return tuple(sorted(self._streams))

    def resolve_stream_name(self, path: str) -> str | None:
        cleaned = path.strip().lstrip("/").replace("\\", "/")
        if cleaned in self._streams:
            return cleaned
        return self._lower_streams.get(cleaned.lower())

    def has_stream(self, path: str) -> bool:
        return self.resolve_stream_name(path) is not None

    def read_stream(self, path: str, *, max_bytes: int = MAX_CFB_STREAM_BYTES) -> bytes | None:
        """지정된 경로의 스트림 바이트를 읽어 반환한다. 없으면 `None`."""
        actual = self.resolve_stream_name(path)
        if actual is None:
            return None
        ent = self._streams[actual]
        if ent.stream_size == 0 or ent.start_sector > _MAXREGSECT:
            return b""
        limit = min(ent.stream_size, max_bytes)
        if ent.stream_size < self._mini_stream_cutoff and self._mini_stream:
            data = self._read_mini_chain(ent.start_sector, max_bytes=limit)
        else:
            data = self._read_fat_chain(ent.start_sector, max_bytes=limit)
        return data[:limit]

    def close(self) -> None:
        """메모리 참조를 정리한다."""
        self._raw = b""
        self._mini_stream = b""

    def __enter__(self) -> CfbFile:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _decode_ole_prop_string(raw: bytes, encoding: str) -> str:
    trimmed = raw.split(b"\x00", 1)[0]
    if not trimmed:
        return ""
    for enc in (encoding, "utf-8", "cp949", "cp1252"):
        try:
            return trimmed.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return trimmed.decode("utf-8", errors="replace").strip()


def read_ole_summary_info(
    cfb: CfbFile,
    stream_names: tuple[str, ...] = ("\x05SummaryInformation", "\x05HwpSummaryInformation"),
) -> OleSummaryInfo:
    """OLE2 PropertySet 요약 스트림에서 제목·주제·작성자·생성 앱을 읽는다."""
    for sname in stream_names:
        blob = cfb.read_stream(sname, max_bytes=1024 * 1024)
        if not blob or len(blob) < 48:
            continue
        byte_order = struct.unpack_from("<H", blob, 0)[0]
        if byte_order != 0xFFFE:
            continue
        num_sections = struct.unpack_from("<I", blob, 24)[0]
        if num_sections < 1 or len(blob) < 28 + 20:
            continue

        sec_offset = struct.unpack_from("<I", blob, 28 + 16)[0]
        if sec_offset + 8 > len(blob):
            continue
        num_props = struct.unpack_from("<I", blob, sec_offset + 4)[0]
        if num_props == 0 or num_props > 512:
            continue

        prop_offsets: dict[int, int] = {}
        for i in range(num_props):
            entry_off = sec_offset + 8 + i * 8
            if entry_off + 8 > len(blob):
                break
            pid, rel_off = struct.unpack_from("<II", blob, entry_off)
            prop_offsets[pid] = sec_offset + rel_off

        encoding = "cp949"
        if 1 in prop_offsets and prop_offsets[1] + 6 <= len(blob):
            cp_vt = struct.unpack_from("<I", blob, prop_offsets[1])[0]
            if cp_vt in (2, 18):  # VT_I2 / VT_UI2
                cp_val = struct.unpack_from("<H", blob, prop_offsets[1] + 4)[0]
                encoding = _CODEPAGE_MAP.get(cp_val, encoding)

        def _read_str_prop(
            pid: int,
            *,
            _offsets: dict[int, int] = prop_offsets,
            _blob: bytes = blob,
            _enc: str = encoding,
        ) -> str | None:
            poff = _offsets.get(pid)
            if poff is None or poff + 8 > len(_blob):
                return None
            vt, length = struct.unpack_from("<II", _blob, poff)
            if vt == 30 and 0 < length <= 65536 and poff + 8 + length <= len(_blob):
                # VT_LPSTR
                val = _decode_ole_prop_string(_blob[poff + 8 : poff + 8 + length], _enc)
                return val or None
            if vt == 31 and 0 < length <= 32768 and poff + 8 + length * 2 <= len(_blob):
                # VT_LPWSTR
                val = (
                    _blob[poff + 8 : poff + 8 + length * 2]
                    .decode("utf-16le", errors="replace")
                    .split("\x00", 1)[0]
                    .strip()
                )
                return val or None
            return None

        title = _read_str_prop(2)
        subject = _read_str_prop(3)
        author = _read_str_prop(4) or _read_str_prop(8)
        creator = _read_str_prop(18)
        if any((title, subject, author, creator)):
            return OleSummaryInfo(
                title=title,
                subject=subject,
                author=author,
                creator_app=creator,
            )
    return OleSummaryInfo()


def _wrap_dib_as_bmp(dib_bytes: bytes) -> bytes:
    """OfficeArt DIB(BMP 정보 헤더만 포함)에 14바이트 `BM` 파일 헤더를 붙여 유효한 BMP로 만든다."""
    if len(dib_bytes) < 40:
        return dib_bytes
    header_size = struct.unpack_from("<I", dib_bytes, 0)[0]
    bit_count = struct.unpack_from("<H", dib_bytes, 14)[0]
    colors_used = struct.unpack_from("<I", dib_bytes, 32)[0]
    palette_bytes = 0
    if bit_count <= 8:
        palette_bytes = (colors_used if colors_used > 0 else (1 << bit_count)) * 4
    pixel_offset = 14 + header_size + palette_bytes
    file_size = 14 + len(dib_bytes)
    return struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset) + dib_bytes


def extract_officeart_blips(
    blob: bytes,
    *,
    max_images: int = 64,
) -> list[OfficeArtBlip]:
    """MS Office `Pictures` / `Data` 스트림에서 OfficeArt 래스터 BLIP(PNG/JPEG/BMP)을 추출한다."""
    if not blob or len(blob) < 25:
        return []

    blips: list[OfficeArtBlip] = []
    seen_hashes: set[bytes] = set()
    pos = 0
    n = len(blob)

    # 1. OfficeArt 레코드 헤더 순차 스캔 (Pictures 스트림은 연속 BLIP 레코드)
    while pos + 8 <= n and len(blips) < max_images:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", blob, pos)
        rec_inst = (ver_inst >> 4) & 0x0FFF
        payload_start = pos + 8
        payload_end = payload_start + rec_len

        if rec_type in (0xF01D, 0xF01E, 0xF01F, 0xF029) and 17 < rec_len <= MAX_MEDIA_BYTES:
            if payload_end > n:
                break
            # 1-UID(17B) vs 2-UID(33B) BLIP 헤더 건너뛰기
            two_uid = rec_inst in (0x46B, 0x6E1, 0x6E3, 0x7A9)
            hdr_skip = 33 if two_uid else 17
            if payload_start + hdr_skip < payload_end:
                img_bytes = blob[payload_start + hdr_skip : payload_end]
                ext = ".png" if rec_type == 0xF01E else (".bmp" if rec_type == 0xF01F else ".jpg")
                if rec_type == 0xF01F and not img_bytes.startswith(b"BM"):
                    img_bytes = _wrap_dib_as_bmp(img_bytes)
                px_w, px_h = probe_image_dimensions(img_bytes, ext)
                if px_w is not None and px_h is not None and px_w >= 8 and px_h >= 8:
                    digest = hashlib.sha256(img_bytes).digest()
                    if digest not in seen_hashes:
                        seen_hashes.add(digest)
                        blips.append(
                            OfficeArtBlip(
                                index=len(blips) + 1,
                                extension=ext,
                                raw_bytes=img_bytes,
                                pixel_width=px_w,
                                pixel_height=px_h,
                            )
                        )
            pos = payload_end
            continue

        # 컨테이너 레코드(ver == 0xF)는 내부로 진입, 그 외 정상 레코드는 건너뜀
        if (ver_inst & 0x0F) == 0x0F and rec_type >= 0xF000 and payload_end <= n:
            pos = payload_start
        elif 0xF000 <= rec_type <= 0xFFFF and payload_end <= n:
            pos = payload_end
        else:
            break

    if blips:
        return blips

    # 2. 비정형 오프셋(`Data` 스트림 등)에서 PNG 시그니처 스캔 폴백
    scan = 0
    while scan < n and len(blips) < max_images:
        idx = blob.find(b"\x89PNG\r\n\x1a\n", scan)
        if idx < 0:
            break
        iend = blob.find(b"IEND", idx + 8)
        if iend < 0 or iend + 8 > n:
            break
        end_pos = iend + 8
        if end_pos - idx <= MAX_MEDIA_BYTES:
            img_bytes = blob[idx:end_pos]
            px_w, px_h = probe_image_dimensions(img_bytes, ".png")
            if px_w is not None and px_h is not None and px_w >= 16 and px_h >= 16:
                digest = hashlib.sha256(img_bytes).digest()
                if digest not in seen_hashes:
                    seen_hashes.add(digest)
                    blips.append(
                        OfficeArtBlip(
                            index=len(blips) + 1,
                            extension=".png",
                            raw_bytes=img_bytes,
                            pixel_width=px_w,
                            pixel_height=px_h,
                        )
                    )
        scan = end_pos

    return blips


__all__ = [
    "MAX_CFB_FILE_BYTES",
    "MAX_CFB_STREAM_BYTES",
    "CfbDirEntry",
    "CfbFile",
    "OfficeArtBlip",
    "OleSummaryInfo",
    "extract_officeart_blips",
    "read_ole_summary_info",
]
