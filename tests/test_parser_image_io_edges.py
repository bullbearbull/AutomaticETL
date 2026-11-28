"""순수 Python 이미지 인코딩·디코딩(image_io)의 오류 경로·빈 입력·경계 값 테스트."""

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


def _custom_png(
    width: int,
    height: int,
    *,
    bit_depth: int = 8,
    color_type: int = 2,
    interlace: int = 0,
    scanlines: bytes = b"",
    palette: bytes | None = None,
    idat_override: bytes | None = None,
) -> bytes:
    """임의 IHDR 필드와 스캔라인으로 작은 PNG 바이트를 조립한다."""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
    if palette is not None:
        out += _chunk(b"PLTE", palette)
    idat = idat_override if idat_override is not None else zlib.compress(scanlines)
    out += _chunk(b"IDAT", idat)
    out += _chunk(b"IEND", b"")
    return out


# ---------------------------------------------------------------------------
# encode_png: 잘못된 입력 거부와 채널별 왕복
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), [(0, 4), (4, 0), (-1, 4), (4, -2)])
def test_encode_png_rejects_non_positive_dimensions(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="양수"):
        encode_png(b"\x00" * 64, width, height)


def test_encode_png_rejects_unsupported_bit_depth_and_channels() -> None:
    with pytest.raises(ValueError, match="8비트"):
        encode_png(b"\x00" * 12, 2, 2, bit_depth=16)
    with pytest.raises(ValueError, match="채널"):
        encode_png(b"\x00" * 8, 2, 2, channels=2)


def test_encode_png_rejects_short_pixel_buffer() -> None:
    with pytest.raises(ValueError, match="픽셀 버퍼"):
        encode_png(b"\x00" * 11, 2, 2, channels=3)


def test_encode_png_gray_and_rgba_roundtrip_and_extra_bytes_ignored() -> None:
    gray = bytes(range(6))
    png_gray = encode_png(gray, 3, 2, channels=1)
    dec_gray = decode_png(png_gray)
    assert dec_gray is not None
    assert (dec_gray.width, dec_gray.height, dec_gray.channels) == (3, 2, 1)
    assert dec_gray.pixels == gray

    rgba = bytes([10, 20, 30, 40, 50, 60, 70, 80])
    # 필요한 8바이트보다 긴 버퍼는 앞부분만 사용해야 한다.
    png_rgba = encode_png(rgba + b"\xff\xff", 2, 1, channels=4)
    dec_rgba = decode_png(png_rgba)
    assert dec_rgba is not None
    assert dec_rgba.channels == 4
    assert dec_rgba.pixels == rgba


# ---------------------------------------------------------------------------
# BGRA/CMYK 변환
# ---------------------------------------------------------------------------


def test_bgra_to_rgba_swaps_channels_and_handles_empty() -> None:
    bgra = bytes([1, 2, 3, 4, 11, 12, 13, 14])
    assert bgra_to_rgba(bgra) == bytes([3, 2, 1, 4, 13, 12, 11, 14])
    assert bgra_to_rgba(b"") == b""


def test_cmyk_to_rgb_known_colors_and_partial_trailing_bytes() -> None:
    # 흰색(K=0), 검정(K=255), 순수 시안(C=255)
    cmyk = bytes([0, 0, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0])
    assert cmyk_to_rgb(cmyk) == bytes([255, 255, 255, 0, 0, 0, 0, 255, 255])
    # (10,20,30,40): ((255-c)*(255-40))//255 = 206, 198, 189
    assert cmyk_to_rgb(bytes([10, 20, 30, 40])) == bytes([206, 198, 189])
    # 4바이트 미만 꼬리 바이트는 무시한다.
    assert cmyk_to_rgb(bytes([0, 0, 0, 0, 99])) == bytes([255, 255, 255])
    assert cmyk_to_rgb(b"") == b""


# ---------------------------------------------------------------------------
# decode_pdf_image_stream: 필터·색공간·크기 경계
# ---------------------------------------------------------------------------


def test_decode_pdf_image_stream_rejects_non_positive_size() -> None:
    assert (
        decode_pdf_image_stream(b"x", filters=[], color_space="/DeviceRGB", width=0, height=4)
        is None
    )
    assert (
        decode_pdf_image_stream(b"x", filters=[], color_space="/DeviceRGB", width=4, height=-1)
        is None
    )


def test_decode_pdf_image_stream_dct_and_jpx_passthrough() -> None:
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    # 마지막 필터가 DCT이면 앞선 FlateDecode와 무관하게 원본 바이트를 그대로 저장한다.
    jpg = decode_pdf_image_stream(
        jpeg, filters=["/FlateDecode", "/DCTDecode"], color_space=None, width=8, height=8
    )
    assert jpg is not None
    assert (jpg.extension, jpg.data) == (".jpg", jpeg)
    assert (jpg.width, jpg.height) == (8, 8)

    dct_alias = decode_pdf_image_stream(jpeg, filters=["/DCT"], color_space=None, width=8, height=8)
    assert dct_alias is not None and dct_alias.extension == ".jpg"

    jp2 = decode_pdf_image_stream(
        b"fake-jp2", filters=["/JPXDecode"], color_space=None, width=4, height=4
    )
    assert jp2 is not None
    assert (jp2.extension, jp2.data) == (".jp2", b"fake-jp2")


def test_decode_pdf_image_stream_rejects_non_8bit_flate() -> None:
    assert (
        decode_pdf_image_stream(
            b"\x00" * 12,
            filters=["/FlateDecode"],
            color_space="/DeviceRGB",
            width=2,
            height=2,
            bits_per_component=1,
        )
        is None
    )


def test_decode_pdf_image_stream_rgb_gray_cmyk_and_decoded_data() -> None:
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 12, 34, 56])
    # raw_data는 압축 스트림, decoded_data가 실제 픽셀인 경우 후자를 사용해야 한다.
    img = decode_pdf_image_stream(
        zlib.compress(rgb),
        filters=["/FlateDecode"],
        color_space="/DeviceRGB",
        width=2,
        height=2,
        decoded_data=rgb,
    )
    assert img is not None and img.extension == ".png"
    dec = decode_png(img.data)
    assert dec is not None and dec.pixels == rgb

    gray = bytes([0, 64, 128, 255])
    img_gray = decode_pdf_image_stream(
        gray, filters=[], color_space="/DeviceGray", width=2, height=2
    )
    assert img_gray is not None
    dec_gray = decode_png(img_gray.data)
    assert dec_gray is not None
    assert dec_gray.channels == 1 and dec_gray.pixels == gray

    cmyk = bytes([0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 255, 10, 20, 30, 40])
    img_cmyk = decode_pdf_image_stream(
        cmyk, filters=["/FlateDecode"], color_space="/DeviceCMYK", width=2, height=2
    )
    assert img_cmyk is not None
    dec_cmyk = decode_png(img_cmyk.data)
    assert dec_cmyk is not None
    assert dec_cmyk.channels == 3
    assert dec_cmyk.pixels == bytes([255, 255, 255, 0, 255, 255, 0, 0, 0, 206, 198, 189])


def test_decode_pdf_image_stream_channel_guess_from_pixel_length() -> None:
    rgb = bytes([9, 8, 7] * 4)
    # ColorSpace가 복합 배열(ICCBased 등)이어도 길이가 정확히 3*w*h면 RGB로 추정한다.
    img3 = decode_pdf_image_stream(
        rgb, filters=["/FlateDecode"], color_space="[/ICCBased 12 0 R]", width=2, height=2
    )
    assert img3 is not None
    dec3 = decode_png(img3.data)
    assert dec3 is not None and dec3.channels == 3 and dec3.pixels == rgb

    gray = bytes([1, 2, 3, 4])
    img1 = decode_pdf_image_stream(
        gray, filters=["/FlateDecode"], color_space=None, width=2, height=2
    )
    assert img1 is not None
    dec1 = decode_png(img1.data)
    assert dec1 is not None and dec1.channels == 1 and dec1.pixels == gray

    # RGB 이름이지만 버퍼가 w*h뿐이면 그레이 추정 경로를 탄다.
    img_short = decode_pdf_image_stream(
        gray, filters=[], color_space="/DeviceRGB", width=2, height=2
    )
    assert img_short is not None
    dec_short = decode_png(img_short.data)
    assert dec_short is not None and dec_short.channels == 1

    # 어떤 길이 조건에도 맞지 않으면 None(페이지 렌더러 폴백).
    assert (
        decode_pdf_image_stream(b"\x00" * 5, filters=[], color_space="/Indexed", width=2, height=2)
        is None
    )


# ---------------------------------------------------------------------------
# save_encoded_image: 경로 안전화와 디렉터리 생성
# ---------------------------------------------------------------------------


def test_save_encoded_image_sanitizes_stem_and_creates_parents(tmp_path: Path) -> None:
    img = EncodedImage(data=b"fake-png", extension=".png", width=1, height=1)
    target = tmp_path / "nested" / "crops"
    out = save_encoded_image(img, target, "a/b\\c:d*e?")
    assert out == target / "a_b_c_d_e_.png"
    assert out.read_bytes() == b"fake-png"

    # 한글처럼 isalnum인 문자는 그대로 유지한다.
    out_ko = save_encoded_image(img, target, "보고서_1-차")
    assert out_ko.name == "보고서_1-차.png"


# ---------------------------------------------------------------------------
# decode_png: 손상·미지원 입력 거부
# ---------------------------------------------------------------------------


def test_decode_png_rejects_empty_short_and_bad_magic() -> None:
    assert decode_png(b"") is None
    assert decode_png(b"\x89PNG\r\n\x1a\n") is None
    assert decode_png(b"NOT-A-PNG" + b"\x00" * 40) is None


def test_decode_png_rejects_truncated_chunk() -> None:
    png = _custom_png(2, 2, scanlines=b"\x00" + b"\x00" * 6)
    # IDAT 청크 중간에서 잘라낸다.
    assert decode_png(png[: len(png) - 20]) is None


def test_decode_png_rejects_interlaced_unknown_color_and_missing_idat() -> None:
    interlaced = _custom_png(2, 2, interlace=1, scanlines=b"\x00" * 14)
    assert decode_png(interlaced) is None

    bad_color = _custom_png(2, 2, color_type=5, scanlines=b"\x00" * 14)
    assert decode_png(bad_color) is None

    no_idat = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + _chunk(b"IEND", b"")
    )
    assert decode_png(no_idat) is None


def test_decode_png_rejects_corrupt_zlib_and_bad_filter_and_short_scanlines() -> None:
    corrupt = _custom_png(2, 2, idat_override=b"not-zlib-data")
    assert decode_png(corrupt) is None

    # 필터 바이트 7은 정의되지 않은 필터다(길이는 충족해야 필터 분기에 도달).
    bad_filter = _custom_png(2, 2, scanlines=b"\x07" + b"\x00" * 13)
    assert decode_png(bad_filter) is None

    # 높이 2를 선언했지만 스캔라인이 1줄뿐이면 None.
    short = _custom_png(2, 2, scanlines=b"\x00" + b"\x00" * 6)
    assert decode_png(short) is None


def test_decode_png_rejects_16bit_and_1bit_non_gray_and_zero_size() -> None:
    assert decode_png(_custom_png(2, 2, bit_depth=16, scanlines=b"\x00" * 26)) is None
    assert decode_png(_custom_png(2, 2, bit_depth=1, color_type=2, scanlines=b"\x00" * 4)) is None
    assert decode_png(_custom_png(0, 2, scanlines=b"")) is None
    # IHDR 페이로드가 13바이트가 아니면 파싱되지 않아 width=0으로 거부된다.
    bad_ihdr = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", b"\x00" * 12)
        + _chunk(b"IDAT", zlib.compress(b"\x00"))
        + _chunk(b"IEND", b"")
    )
    assert decode_png(bad_ihdr) is None


# ---------------------------------------------------------------------------
# decode_png: 팔레트·1비트 그레이·다중 IDAT·Gray+Alpha
# ---------------------------------------------------------------------------


def test_decode_png_palette_lookup_and_index_clamping() -> None:
    palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])  # R, G, B 3색
    # 인덱스 3은 팔레트 최대 인덱스 2(B)로 클램프되어야 한다.
    scanlines = b"\x00" + bytes([0, 1, 2, 3])
    png = _custom_png(4, 1, color_type=3, scanlines=scanlines, palette=palette)
    dec = decode_png(png)
    assert dec is not None
    assert (dec.width, dec.height, dec.channels) == (4, 1, 3)
    assert dec.pixels == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 255])


def test_decode_png_palette_missing_or_too_short_returns_none() -> None:
    scanlines = b"\x00" + bytes([0, 1])
    no_plte = _custom_png(2, 1, color_type=3, scanlines=scanlines)
    assert decode_png(no_plte) is None
    short_plte = _custom_png(2, 1, color_type=3, scanlines=scanlines, palette=b"\x01\x02")
    assert decode_png(short_plte) is None


def test_decode_png_1bit_gray_expands_msb_first() -> None:
    # 너비 10 → 행당 2바이트. 0b10101010 0b11000000 → 255,0,...,255,255
    row0 = bytes([0b10101010, 0b11000000])
    row1 = bytes([0b00000000, 0b00000000])
    scanlines = b"\x00" + row0 + b"\x00" + row1
    png = _custom_png(10, 2, bit_depth=1, color_type=0, scanlines=scanlines)
    dec = decode_png(png)
    assert dec is not None
    assert (dec.width, dec.height, dec.channels) == (10, 2, 1)
    assert dec.pixels[:10] == bytes([255, 0, 255, 0, 255, 0, 255, 0, 255, 255])
    assert dec.pixels[10:] == bytes(10)


def test_decode_png_1bit_gray_short_scanlines_returns_none() -> None:
    png = _custom_png(10, 2, bit_depth=1, color_type=0, scanlines=b"\x00\x00")
    assert decode_png(png) is None


def test_decode_png_joins_multiple_idat_chunks() -> None:
    rgb = bytes([7, 8, 9] * 4)
    scanlines = b"".join(b"\x00" + rgb[y * 6 : (y + 1) * 6] for y in range(2))
    compressed = zlib.compress(scanlines)
    mid = len(compressed) // 2
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", compressed[:mid])
        + _chunk(b"IDAT", compressed[mid:])
        + _chunk(b"IEND", b"")
    )
    dec = decode_png(png)
    assert dec is not None and dec.pixels == rgb


def test_decode_png_gray_alpha_and_to_grayscale_paths() -> None:
    # Gray+Alpha(color_type 4): 그레이 [10, 200], 알파 [255, 0]
    scanlines = b"\x00" + bytes([10, 255, 200, 0])
    png = _custom_png(2, 1, color_type=4, scanlines=scanlines)
    dec = decode_png(png)
    assert dec is not None and dec.channels == 2
    assert to_grayscale(dec) == bytes([10, 200])

    # 1채널은 그대로 반환한다.
    gray_img = DecodedImage(width=2, height=1, channels=1, pixels=b"\x01\x02")
    assert to_grayscale(gray_img) == b"\x01\x02"

    # RGB 가중치: (r*77 + g*150 + b*29) >> 8
    rgb_img = DecodedImage(
        width=4,
        height=1,
        channels=3,
        pixels=bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]),
    )
    assert to_grayscale(rgb_img) == bytes([76, 149, 28, 255])

    # RGBA는 알파를 무시하고 동일 가중치를 적용한다.
    rgba_img = DecodedImage(width=1, height=1, channels=4, pixels=bytes([255, 0, 0, 128]))
    assert to_grayscale(rgba_img) == bytes([76])


# ---------------------------------------------------------------------------
# crop_decoded_image / crop_png_region: 좌표 클램프 경계
# ---------------------------------------------------------------------------


def _rgb_4x4() -> DecodedImage:
    px = bytearray(4 * 4 * 3)
    for y in range(4):
        for x in range(4):
            off = (y * 4 + x) * 3
            px[off] = x * 10
            px[off + 1] = y * 10
            px[off + 2] = 100
    return DecodedImage(width=4, height=4, channels=3, pixels=bytes(px))


def test_crop_decoded_image_clamps_negative_and_oversized_coords() -> None:
    img = _rgb_4x4()
    left_top = crop_decoded_image(img, -5, -7, 2, 2)
    assert left_top is not None
    assert (left_top.width, left_top.height) == (2, 2)
    dec = decode_png(left_top.data)
    assert dec is not None
    assert dec.pixels[:3] == bytes([0, 0, 100])

    right_bottom = crop_decoded_image(img, 3, 3, 99, 99)
    assert right_bottom is not None
    assert (right_bottom.width, right_bottom.height) == (1, 1)
    dec_rb = decode_png(right_bottom.data)
    assert dec_rb is not None
    assert dec_rb.pixels == bytes([30, 30, 100])


def test_crop_decoded_image_fully_outside_still_yields_one_pixel() -> None:
    img = _rgb_4x4()
    # 완전히 밖이어도 마지막 행/열 1픽셀로 클램프된다(빈 크롭 대신).
    out = crop_decoded_image(img, 10, 10, 20, 20)
    assert out is not None
    assert (out.width, out.height) == (1, 1)
    dec = decode_png(out.data)
    assert dec is not None and dec.pixels == bytes([30, 30, 100])

    # x1==x0이어도 최소 1픽셀 폭을 보장한다.
    degenerate = crop_decoded_image(img, 2, 2, 2, 2)
    assert degenerate is not None
    assert (degenerate.width, degenerate.height) == (1, 1)


def test_crop_decoded_image_gray_alpha_normalizes_to_gray_png() -> None:
    ga = DecodedImage(
        width=3,
        height=2,
        channels=2,
        pixels=bytes([10, 255, 20, 255, 30, 255, 40, 0, 50, 0, 60, 0]),
    )
    out = crop_decoded_image(ga, 1, 0, 3, 2)
    assert out is not None
    assert (out.width, out.height) == (2, 2)
    dec = decode_png(out.data)
    assert dec is not None
    assert dec.channels == 1
    assert dec.pixels == bytes([20, 30, 50, 60])


def test_crop_png_region_returns_none_for_invalid_png() -> None:
    assert crop_png_region(b"not-a-png", 0, 0, 2, 2) is None
