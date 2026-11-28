"""image_io 순수 Python 이미지 유틸리티의 오류 경로·빈 입력·경계 값 단위 테스트."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from autoetl.parser.image_io import (
    DecodedImage,
    EncodedImage,
    bgra_to_rgba,
    cmyk_to_rgb,
    crop_decoded_image,
    crop_png_region,
    decode_pdf_image_stream,
    decode_png,
    encode_png,
    save_encoded_image,
    to_grayscale,
)


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(payload, zlib.crc32(ctype)) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def _ihdr(width: int, height: int, depth: int, color_type: int, interlace: int = 0) -> bytes:
    return struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, interlace)


def _custom_png(
    ihdr: bytes,
    idat_parts: list[bytes],
    *,
    plte: bytes | None = None,
    trailing: bytes = b"",
) -> bytes:
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
    if plte is not None:
        out += _chunk(b"PLTE", plte)
    for part in idat_parts:
        out += _chunk(b"IDAT", part)
    out += _chunk(b"IEND", b"")
    return out + trailing


# ---------------------------------------------------------------------------
# encode_png 입력 검증
# ---------------------------------------------------------------------------


def test_encode_png_rejects_invalid_dimensions_depth_channels_and_short_buffer() -> None:
    row = bytes([10, 20, 30] * 2)
    with pytest.raises(ValueError, match="양수"):
        encode_png(row, 0, 1)
    with pytest.raises(ValueError, match="양수"):
        encode_png(row, 2, -1)
    with pytest.raises(ValueError, match="8비트"):
        encode_png(row, 2, 1, bit_depth=16)
    with pytest.raises(ValueError, match="채널"):
        encode_png(row, 2, 1, channels=2)
    with pytest.raises(ValueError, match="픽셀 버퍼"):
        encode_png(b"\x00" * 5, 2, 1, channels=3)


def test_encode_png_accepts_extra_buffer_bytes_and_gray_rgba_roundtrip() -> None:
    # 필요한 크기보다 긴 버퍼는 앞부분만 사용한다.
    gray = bytes([0, 64, 128, 255])
    png = encode_png(gray + b"\xff\xff", 2, 2, channels=1)
    dec = decode_png(png)
    assert dec is not None
    assert (dec.width, dec.height, dec.channels) == (2, 2, 1)
    assert dec.pixels == gray

    rgba = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    png4 = encode_png(rgba, 2, 1, channels=4)
    dec4 = decode_png(png4)
    assert dec4 is not None
    assert dec4.channels == 4
    assert dec4.pixels == rgba


# ---------------------------------------------------------------------------
# 채널 변환
# ---------------------------------------------------------------------------


def test_bgra_to_rgba_swaps_channels_and_handles_empty() -> None:
    assert bgra_to_rgba(b"") == b""
    assert bgra_to_rgba(bytes([1, 2, 3, 4, 5, 6, 7, 8])) == bytes([3, 2, 1, 4, 7, 6, 5, 8])


def test_cmyk_to_rgb_known_values_and_partial_pixel() -> None:
    assert cmyk_to_rgb(b"") == b""
    # K=0이면 단순 반전, K=255이면 검정, 완전 CMY면 검정.
    src = bytes(
        [
            0,
            0,
            0,
            0,  # white
            255,
            255,
            255,
            255,  # black via K
            0,
            0,
            0,
            255,  # black via K only
            51,
            102,
            153,
            25,  # mixed
        ]
    )
    out = cmyk_to_rgb(src + b"\x01\x02")  # 4바이트 미만 잔여는 무시
    assert out[:3] == bytes([255, 255, 255])
    assert out[3:6] == bytes([0, 0, 0])
    assert out[6:9] == bytes([0, 0, 0])
    inv_k = 230
    assert out[9:12] == bytes([(204 * inv_k) // 255, (153 * inv_k) // 255, (102 * inv_k) // 255])
    assert len(out) == 12


# ---------------------------------------------------------------------------
# decode_pdf_image_stream 분기
# ---------------------------------------------------------------------------


def test_decode_pdf_image_stream_rejects_nonpositive_dimensions() -> None:
    assert decode_pdf_image_stream(b"", filters=[], color_space=None, width=0, height=2) is None
    assert decode_pdf_image_stream(b"", filters=[], color_space=None, width=2, height=-1) is None


def test_decode_pdf_image_stream_passthrough_jpeg_jp2_and_filter_order() -> None:
    raw = b"\xff\xd8\xff\xe0fake-jpeg"
    jpg = decode_pdf_image_stream(
        raw, filters=["/FlateDecode", "/DCTDecode"], color_space=None, width=4, height=4
    )
    assert jpg is not None
    assert (jpg.extension, jpg.data) == (".jpg", raw)
    assert (jpg.width, jpg.height) == (4, 4)

    alias = decode_pdf_image_stream(raw, filters=["/DCT"], color_space=None, width=4, height=4)
    assert alias is not None and alias.extension == ".jpg"

    jp2 = decode_pdf_image_stream(
        b"jp2-bytes", filters=["/JPXDecode"], color_space=None, width=8, height=2
    )
    assert jp2 is not None and jp2.extension == ".jp2"

    # DCT/JPX는 bits_per_component 검사보다 먼저 적용된다.
    low_bpc = decode_pdf_image_stream(
        raw, filters=["/DCTDecode"], color_space=None, width=4, height=4, bits_per_component=1
    )
    assert low_bpc is not None and low_bpc.extension == ".jpg"


def test_decode_pdf_image_stream_rejects_non_8bit_raw() -> None:
    assert (
        decode_pdf_image_stream(
            b"\x00" * 12,
            filters=[],
            color_space="/DeviceRGB",
            width=2,
            height=2,
            bits_per_component=4,
        )
        is None
    )


def test_decode_pdf_image_stream_rgb_gray_cmyk_and_decoded_override() -> None:
    rgb = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
    enc = decode_pdf_image_stream(
        b"compressed-placeholder",
        filters=["/FlateDecode"],
        color_space="/DeviceRGB",
        width=2,
        height=2,
        decoded_data=rgb,
    )
    assert enc is not None and enc.extension == ".png"
    dec = decode_png(enc.data)
    assert dec is not None and dec.pixels == rgb

    gray = bytes([0, 85, 170, 255])
    enc_g = decode_pdf_image_stream(gray, filters=[], color_space="/DeviceGray", width=2, height=2)
    assert enc_g is not None
    dec_g = decode_png(enc_g.data)
    assert dec_g is not None and dec_g.channels == 1 and dec_g.pixels == gray

    cmyk = bytes([0, 0, 0, 0, 51, 102, 153, 25])
    enc_c = decode_pdf_image_stream(cmyk, filters=[], color_space="/DeviceCMYK", width=2, height=1)
    assert enc_c is not None
    dec_c = decode_png(enc_c.data)
    assert dec_c is not None and dec_c.channels == 3
    assert dec_c.pixels[:3] == bytes([255, 255, 255])
    inv_k = 230
    assert dec_c.pixels[3:6] == bytes(
        [(204 * inv_k) // 255, (153 * inv_k) // 255, (102 * inv_k) // 255]
    )


def test_decode_pdf_image_stream_channel_guess_by_length_and_none_fallback() -> None:
    rgb = bytes(range(12))
    # 복합 ICCBased 배열 이름은 픽셀 길이로 채널을 추정한다.
    guess3 = decode_pdf_image_stream(
        rgb, filters=[], color_space="[/ICCBased 5 0 R]", width=2, height=2
    )
    assert guess3 is not None and guess3.extension == ".png"
    dec3 = decode_png(guess3.data)
    assert dec3 is not None and dec3.channels == 3

    guess1 = decode_pdf_image_stream(
        bytes([1, 2, 3, 4]), filters=[], color_space=None, width=2, height=2
    )
    assert guess1 is not None
    dec1 = decode_png(guess1.data)
    assert dec1 is not None and dec1.channels == 1

    # 이름이 RGB여도 길이가 부족하면 인코딩하지 않는다.
    assert (
        decode_pdf_image_stream(
            b"\x00" * 11, filters=[], color_space="/DeviceRGB", width=2, height=2
        )
        is None
    )
    assert (
        decode_pdf_image_stream(b"\x00" * 5, filters=[], color_space="/DeviceN", width=2, height=2)
        is None
    )


# ---------------------------------------------------------------------------
# save_encoded_image 경로 정규화
# ---------------------------------------------------------------------------


def test_save_encoded_image_sanitizes_stem_and_creates_parents(tmp_path: Path) -> None:
    img = EncodedImage(data=b"png-bytes", extension=".png", width=1, height=1)
    target = tmp_path / "nested" / "crops"
    out = save_encoded_image(img, target, "re port/1:a")
    assert out == target / "re_port_1_a.png"
    assert out.read_bytes() == b"png-bytes"
    # 이미 존재하는 디렉터리에도 안전하게 덮어쓴다.
    out2 = save_encoded_image(EncodedImage(data=b"v2", extension=".jpg"), target, "re_port_1_a")
    assert out2.read_bytes() == b"v2"


# ---------------------------------------------------------------------------
# decode_png 거부 경로·팔레트·1비트 그레이
# ---------------------------------------------------------------------------


def test_decode_png_rejects_short_bad_magic_and_truncated_chunk() -> None:
    assert decode_png(b"") is None
    assert decode_png(b"\x89PNG\r\n") is None
    assert decode_png(b"\x00NG\r\n\x1a\n" + b"\x00" * 40) is None
    # 청크 길이가 실제 데이터보다 길면 거부한다.
    truncated = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 100) + b"IHDR" + b"\x00" * 30
    assert decode_png(truncated) is None


def test_decode_png_rejects_invalid_header_fields() -> None:
    ok_idat = zlib.compress(b"\x00" + b"\x00" * 3)
    assert decode_png(_custom_png(_ihdr(0, 1, 8, 2), [ok_idat])) is None
    assert decode_png(_custom_png(_ihdr(1, 1, 8, 2, interlace=1), [ok_idat])) is None
    assert decode_png(_custom_png(_ihdr(1, 1, 8, 2), [])) is None
    assert decode_png(_custom_png(_ihdr(1, 1, 8, 1), [ok_idat])) is None
    assert decode_png(_custom_png(_ihdr(1, 1, 16, 0), [ok_idat])) is None


def test_decode_png_rejects_corrupt_or_short_scanline_data() -> None:
    assert decode_png(_custom_png(_ihdr(1, 1, 8, 2), [b"not-zlib-data"])) is None
    # 스캔라인 바이트가 부족하면 None.
    short = zlib.compress(b"\x00\x01")
    assert decode_png(_custom_png(_ihdr(2, 2, 8, 2), [short])) is None
    # 알 수 없는 필터 타입(9)은 None.
    bad_filter = zlib.compress(b"\x09" + b"\x00" * 3)
    assert decode_png(_custom_png(_ihdr(1, 1, 8, 2), [bad_filter])) is None


def test_decode_png_palette_expansion_clamps_and_missing_palette() -> None:
    indices = bytes([0, 1, 2, 255])
    raw = zlib.compress(b"\x00" + indices)
    plte = bytes([10, 20, 30, 40, 50, 60])
    dec = decode_png(_custom_png(_ihdr(4, 1, 8, 3), [raw], plte=plte))
    assert dec is not None
    assert (dec.width, dec.height, dec.channels) == (4, 1, 3)
    # 팔레트 밖 인덱스는 마지막 항목으로 클램프된다.
    assert dec.pixels == bytes([10, 20, 30, 40, 50, 60, 40, 50, 60, 40, 50, 60])

    assert decode_png(_custom_png(_ihdr(4, 1, 8, 3), [raw])) is None
    assert decode_png(_custom_png(_ihdr(4, 1, 8, 3), [raw], plte=b"\x01\x02")) is None


def test_decode_png_one_bit_gray_expands_msb_first() -> None:
    # width=10 → 행당 2바이트. MSB부터 비트 전개.
    scanlines = bytes([0, 0b10101010, 0b11000000, 0, 0, 0])
    raw = zlib.compress(scanlines)
    dec = decode_png(_custom_png(_ihdr(10, 2, 1, 0), [raw]))
    assert dec is not None
    assert (dec.width, dec.height, dec.channels) == (10, 2, 1)
    assert dec.pixels == bytes([255, 0, 255, 0, 255, 0, 255, 0, 255, 255] + [0] * 10)

    short = zlib.compress(b"\x00\x00")
    assert decode_png(_custom_png(_ihdr(10, 2, 1, 0), [short])) is None


def test_decode_png_joins_multiple_idat_and_ignores_trailing_bytes() -> None:
    rgb = bytes([9, 8, 7, 6, 5, 4])
    compressed = zlib.compress(b"\x00" + rgb)
    mid = len(compressed) // 2
    png = _custom_png(
        _ihdr(2, 1, 8, 2), [compressed[:mid], compressed[mid:]], trailing=b"trailing-junk"
    )
    dec = decode_png(png)
    assert dec is not None and dec.pixels == rgb


# ---------------------------------------------------------------------------
# to_grayscale / crop
# ---------------------------------------------------------------------------


def test_to_grayscale_channel_paths() -> None:
    gray = DecodedImage(width=2, height=1, channels=1, pixels=b"\x01\x02")
    assert to_grayscale(gray) == b"\x01\x02"

    ga = DecodedImage(width=2, height=1, channels=2, pixels=bytes([10, 255, 20, 128]))
    assert to_grayscale(ga) == bytes([10, 20])

    rgb = DecodedImage(
        width=3, height=1, channels=3, pixels=bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
    )
    assert to_grayscale(rgb) == bytes([76, 149, 28])

    rgba = DecodedImage(width=1, height=1, channels=4, pixels=bytes([10, 20, 30, 255]))
    assert to_grayscale(rgba) == bytes([(10 * 77 + 20 * 150 + 30 * 29) >> 8])


def test_crop_decoded_image_gray_alpha_normalizes_to_gray_png() -> None:
    # 4x3 Gray+Alpha에서 (1,0)-(3,2) 크롭 → 2x2 그레이 PNG.
    pixels = bytearray()
    for y in range(3):
        for x in range(4):
            pixels.extend([y * 40 + x * 10, 255])
    img = DecodedImage(width=4, height=3, channels=2, pixels=bytes(pixels))
    cropped = crop_decoded_image(img, 1, 0, 3, 2)
    assert cropped is not None
    assert (cropped.width, cropped.height, cropped.extension) == (2, 2, ".png")
    dec = decode_png(cropped.data)
    assert dec is not None and dec.channels == 1
    assert dec.pixels == bytes([10, 20, 50, 60])


def test_crop_decoded_image_clamps_out_of_range_and_inverted_rects() -> None:
    rgb = bytes([1, 2, 3] * 12)
    img = DecodedImage(width=4, height=3, channels=3, pixels=rgb)

    full = crop_decoded_image(img, -5, -5, 100, 100)
    assert full is not None and (full.width, full.height) == (4, 3)
    dec_full = decode_png(full.data)
    assert dec_full is not None and dec_full.pixels == rgb

    # 역전된 사각형은 최소 1x1로 보정된다.
    tiny = crop_decoded_image(img, 3, 2, 1, 1)
    assert tiny is not None and (tiny.width, tiny.height) == (1, 1)
    dec_tiny = decode_png(tiny.data)
    assert dec_tiny is not None
    assert dec_tiny.pixels == rgb[(2 * 4 + 3) * 3 : (2 * 4 + 3) * 3 + 3]


def test_crop_png_region_returns_none_for_undecodable_png() -> None:
    assert crop_png_region(b"not-a-png", 0, 0, 2, 2) is None
