"""순수 Python 이미지 인코딩 및 PDF 내장 이미지 추출 유틸리티.

Pillow 같은 추가 런타임 의존성 없이도 PNG 인코딩(`zlib` + `struct`)과
JPEG/JP2/FlateDecode 원시 이미지 저장을 수행한다.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EncodedImage:
    """저장 준비가 끝난 이미지 바이트와 메타 정보."""

    data: bytes
    extension: str  # ".png", ".jpg", ".jp2"
    width: int | None = None
    height: int | None = None


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def encode_png(
    raw_pixels: bytes,
    width: int,
    height: int,
    *,
    channels: int = 3,
    bit_depth: int = 8,
) -> bytes:
    """원시 픽셀 바이트(Gray=1, RGB=3, RGBA=4)를 표준 PNG 바이트로 인코딩한다."""
    if width <= 0 or height <= 0:
        raise ValueError("이미지 너비와 높이는 양수여야 한다.")
    if bit_depth != 8:
        raise ValueError("현재 순수 PNG 인코더는 8비트 채널만 지원한다.")
    color_type_map = {1: 0, 3: 2, 4: 6}
    if channels not in color_type_map:
        raise ValueError(f"지원하지 않는 채널 수: {channels}")

    stride = width * channels
    expected = stride * height
    if len(raw_pixels) < expected:
        raise ValueError("픽셀 버퍼 크기가 너비x높이x채널보다 작다.")

    scanlines = bytearray()
    for y in range(height):
        start = y * stride
        scanlines.append(0)  # filter type 0 (None)
        scanlines.extend(raw_pixels[start : start + stride])

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type_map[channels], 0, 0, 0)
    compressed = zlib.compress(bytes(scanlines), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def bgra_to_rgba(bgra: bytes) -> bytes:
    """BGRA 바이트 배열을 RGBA로 바꾼다(pypdfium2 비트맵 변환용)."""
    out = bytearray(len(bgra))
    out[0::4] = bgra[2::4]
    out[1::4] = bgra[1::4]
    out[2::4] = bgra[0::4]
    out[3::4] = bgra[3::4]
    return bytes(out)


def cmyk_to_rgb(cmyk: bytes) -> bytes:
    """8비트 CMYK 원시 픽셀을 RGB로 변환한다."""
    pixel_count = len(cmyk) // 4
    out = bytearray(pixel_count * 3)
    for i in range(pixel_count):
        c = cmyk[i * 4]
        m = cmyk[i * 4 + 1]
        y = cmyk[i * 4 + 2]
        k = cmyk[i * 4 + 3]
        inv_k = 255 - k
        out[i * 3] = ((255 - c) * inv_k) // 255
        out[i * 3 + 1] = ((255 - m) * inv_k) // 255
        out[i * 3 + 2] = ((255 - y) * inv_k) // 255
    return bytes(out)


def decode_pdf_image_stream(
    raw_data: bytes,
    *,
    filters: list[str],
    color_space: str | None,
    width: int,
    height: int,
    bits_per_component: int = 8,
    decoded_data: bytes | None = None,
) -> EncodedImage | None:
    """PDF Image XObject 스트림에서 저장 가능한 이미지(JPG/JP2/PNG)를 추출한다.

    지원하지 않는 복합 필터나 1비트/인덱스 팔레트 등은 None을 반환해 상위에서
    페이지 렌더러 폴백을 쓰도록 넘긴다.
    """
    if width <= 0 or height <= 0:
        return None

    last_filter = filters[-1] if filters else ""
    if last_filter in ("/DCTDecode", "/DCT"):
        return EncodedImage(data=raw_data, extension=".jpg", width=width, height=height)
    if last_filter == "/JPXDecode":
        return EncodedImage(data=raw_data, extension=".jp2", width=width, height=height)

    if bits_per_component != 8:
        return None

    pixels = decoded_data if decoded_data is not None else raw_data
    cs = (color_space or "").upper()
    if "RGB" in cs and len(pixels) >= width * height * 3:
        png = encode_png(pixels, width, height, channels=3)
        return EncodedImage(data=png, extension=".png", width=width, height=height)
    if "GRAY" in cs and len(pixels) >= width * height:
        png = encode_png(pixels, width, height, channels=1)
        return EncodedImage(data=png, extension=".png", width=width, height=height)
    if "CMYK" in cs and len(pixels) >= width * height * 4:
        rgb = cmyk_to_rgb(pixels[: width * height * 4])
        png = encode_png(rgb, width, height, channels=3)
        return EncodedImage(data=png, extension=".png", width=width, height=height)

    # ColorSpace 이름이 복합 배열(ICCBased 등)일 때 픽셀 길이로 채널 추정
    if len(pixels) == width * height * 3:
        png = encode_png(pixels, width, height, channels=3)
        return EncodedImage(data=png, extension=".png", width=width, height=height)
    if len(pixels) == width * height:
        png = encode_png(pixels, width, height, channels=1)
        return EncodedImage(data=png, extension=".png", width=width, height=height)
    return None


def save_encoded_image(image: EncodedImage, target_dir: Path, stem: str) -> Path:
    """이미지를 지정 디렉터리에 UTF-8 경로로 안전하게 저장한다."""
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in stem)
    out_path = target_dir / f"{safe_stem}{image.extension}"
    out_path.write_bytes(image.data)
    return out_path


@dataclass(frozen=True)
class DecodedImage:
    """메모리에 디코딩된 8비트 래스터 이미지(Gray=1, GA=2, RGB=3, RGBA=4)."""

    width: int
    height: int
    channels: int
    pixels: bytes


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_scanlines(
    raw: bytes,
    *,
    width: int,
    height: int,
    row_bytes: int,
    bpp: int,
) -> bytes | None:
    expected = (row_bytes + 1) * height
    if len(raw) < expected:
        return None
    out = bytearray(row_bytes * height)
    prev_row = bytearray(row_bytes)
    pos = 0
    for y in range(height):
        flt = raw[pos]
        pos += 1
        src = raw[pos : pos + row_bytes]
        pos += row_bytes
        cur = bytearray(row_bytes)
        if flt == 0:
            cur[:] = src
        elif flt == 1:
            for i in range(row_bytes):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (src[i] + left) & 0xFF
        elif flt == 2:
            for i in range(row_bytes):
                cur[i] = (src[i] + prev_row[i]) & 0xFF
        elif flt == 3:
            for i in range(row_bytes):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (src[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif flt == 4:
            for i in range(row_bytes):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                cur[i] = (src[i] + _paeth_predictor(a, b, c)) & 0xFF
        else:
            return None
        row_start = y * row_bytes
        out[row_start : row_start + row_bytes] = cur
        prev_row = cur
    return bytes(out)


def decode_png(data: bytes) -> DecodedImage | None:
    """표준 비인터레이스 PNG 바이트를 순수 Python으로 디코딩한다.

    지원: 8비트 Gray(0)·RGB(2)·Palette(3)·Gray+Alpha(4)·RGBA(6) 및 1비트 Gray(0).
    지원하지 않는 인터레이스/16비트 PNG는 None을 반환한다.
    """
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    pos = 8
    width = 0
    height = 0
    bit_depth = 0
    color_type = -1
    interlace = 0
    palette: bytes | None = None
    idat_parts: list[bytes] = []

    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk_start = pos + 8
        chunk_end = chunk_start + length
        if chunk_end + 4 > len(data):
            return None
        payload = data[chunk_start:chunk_end]
        pos = chunk_end + 4

        if ctype == b"IHDR" and len(payload) == 13:
            width, height, bit_depth, color_type, _comp, _flt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif ctype == b"PLTE":
            palette = payload
        elif ctype == b"IDAT":
            idat_parts.append(payload)
        elif ctype == b"IEND":
            break

    if width <= 0 or height <= 0 or interlace != 0 or not idat_parts:
        return None
    if color_type not in (0, 2, 3, 4, 6):
        return None

    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error:
        return None

    if bit_depth == 8:
        ch_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
        ch = ch_map[color_type]
        row_bytes = width * ch
        unfiltered = _unfilter_scanlines(
            raw, width=width, height=height, row_bytes=row_bytes, bpp=ch
        )
        if unfiltered is None:
            return None
        if color_type == 3:
            if palette is None or len(palette) < 3:
                return None
            max_idx = (len(palette) // 3) - 1
            rgb = bytearray(width * height * 3)
            for i, idx in enumerate(unfiltered):
                p_off = min(idx, max_idx) * 3
                rgb[i * 3] = palette[p_off]
                rgb[i * 3 + 1] = palette[p_off + 1]
                rgb[i * 3 + 2] = palette[p_off + 2]
            return DecodedImage(width=width, height=height, channels=3, pixels=bytes(rgb))
        return DecodedImage(width=width, height=height, channels=ch, pixels=unfiltered)

    if bit_depth == 1 and color_type == 0:
        row_bytes = (width + 7) // 8
        unfiltered = _unfilter_scanlines(
            raw, width=width, height=height, row_bytes=row_bytes, bpp=1
        )
        if unfiltered is None:
            return None
        gray = bytearray(width * height)
        for y in range(height):
            r_off = y * row_bytes
            out_off = y * width
            for x in range(width):
                byte = unfiltered[r_off + (x >> 3)]
                bit = (byte >> (7 - (x & 7))) & 1
                gray[out_off + x] = 255 if bit else 0
        return DecodedImage(width=width, height=height, channels=1, pixels=bytes(gray))

    return None


def to_grayscale(image: DecodedImage) -> bytes:
    """디코딩된 이미지를 8비트 1채널 그레이스케일 바이트로 변환한다."""
    if image.channels == 1:
        return image.pixels
    n_px = image.width * image.height
    px = image.pixels
    out = bytearray(n_px)
    if image.channels == 2:
        out[:] = px[0::2]
        return bytes(out)
    step = image.channels  # 3 or 4
    for i in range(n_px):
        off = i * step
        out[i] = (px[off] * 77 + px[off + 1] * 150 + px[off + 2] * 29) >> 8
    return bytes(out)


def crop_decoded_image(
    image: DecodedImage,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> EncodedImage | None:
    """디코딩된 이미지에서 픽셀 사각형 `[x0, y0, x1, y1)`을 잘라 PNG로 인코딩한다."""
    cx0 = max(0, min(x0, image.width - 1))
    cy0 = max(0, min(y0, image.height - 1))
    cx1 = max(cx0 + 1, min(x1, image.width))
    cy1 = max(cy0 + 1, min(y1, image.height))
    cw = cx1 - cx0
    ch = cy1 - cy0
    if cw <= 0 or ch <= 0:
        return None

    channels = image.channels
    if channels == 2:
        # PNG 인코더는 1/3/4채널을 지원하므로 Gray+Alpha는 1채널 그레이로 정규화한다.
        cropped = bytearray(cw * ch)
        for row_idx, y in enumerate(range(cy0, cy1)):
            src_row = y * image.width * 2
            dst_row = row_idx * cw
            for x in range(cw):
                cropped[dst_row + x] = image.pixels[src_row + (cx0 + x) * 2]
        png = encode_png(bytes(cropped), cw, ch, channels=1)
        return EncodedImage(data=png, extension=".png", width=cw, height=ch)

    src_stride = image.width * channels
    dst_stride = cw * channels
    cropped = bytearray(dst_stride * ch)
    for row_idx, y in enumerate(range(cy0, cy1)):
        s_start = y * src_stride + cx0 * channels
        d_start = row_idx * dst_stride
        cropped[d_start : d_start + dst_stride] = image.pixels[s_start : s_start + dst_stride]
    png = encode_png(bytes(cropped), cw, ch, channels=channels)
    return EncodedImage(data=png, extension=".png", width=cw, height=ch)


def crop_png_region(
    png_bytes: bytes,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> EncodedImage | None:
    """PNG 바이트에서 지정 픽셀 영역 `[x0, y0, x1, y1)`을 크롭해 새 PNG로 반환한다."""
    decoded = decode_png(png_bytes)
    if decoded is None:
        return None
    return crop_decoded_image(decoded, x0, y0, x1, y1)
