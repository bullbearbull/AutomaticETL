"""추출한 표를 엑셀 파일(`tables.xlsx`)로 내보내는 경로 테스트.

수식처럼 보이는 칸이 `'` 없이 글자 칸으로 남는지, 숫자 기준이 CSV와 같은지,
번들·CLI·폴더 탐색이 함께 맞물리는지를 본다.
"""

from __future__ import annotations

import base64
import decimal
import io
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from autoetl.cli import main
from autoetl.core.multi_context import discover_table_sources
from autoetl.parser import (
    BBox,
    ExtractedTable,
    ParseStrategy,
    discover_batch_files,
    parse_and_export,
    parse_document,
)
from autoetl.parser.exporters import (
    TABLES_XLSX_NAME,
    export_parse_bundle,
    export_tables_xlsx,
    is_bundle_tables_workbook,
)
from autoetl.parser.ui import create_ui_server
from autoetl.parser.xlsx_writer import (
    XLSX_MAX_CELL_CHARS,
    XlsxLimitError,
    XlsxNumber,
    XlsxSheet,
    XlsxText,
    write_xlsx_workbook,
)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_FIXTURE = Path(__file__).parent / "fixtures" / "spreadsheet_formula_cells.json"
_CASES: list[tuple[str, str]] = [
    (case["input"], case["expected"])
    for case in json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
]
_BUILTIN_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
}


def _sample_table(
    headers: list[str], rows: list[list[str]], *, table_id: str = "p001_t01"
) -> ExtractedTable:
    return ExtractedTable(
        table_id=table_id,
        page=1,
        bbox=BBox(x0=0, top=0, x1=1, bottom=1, norm_x0=0, norm_top=0, norm_x1=1, norm_bottom=1),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=1.0,
        row_count=len(rows) + 1,
        col_count=len(headers),
        headers=headers,
        rows=rows,
    )


class _Cell:
    def __init__(self, kind: str, value: str, num_fmt: str, quote_prefix: bool, wrap: bool) -> None:
        self.kind = kind  # "text" 또는 "number"
        self.value = value
        self.num_fmt = num_fmt
        self.quote_prefix = quote_prefix
        self.wrap = wrap


def _read_workbook(path: Path) -> dict[str, dict[str, _Cell]]:
    """테스트용 최소 리더: 시트 이름 → 칸 좌표 → 칸. 수식 칸이 하나라도 있으면 실패한다."""
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        parts = {name: zf.read(name) for name in zf.namelist()}
    # 엑셀은 글 속의 `_xHHHH_`를 그 번호의 글자 하나로 읽는다.
    strings = [
        re.sub(
            r"_x([0-9A-Fa-f]{4})_",
            lambda m: chr(int(m.group(1), 16)),
            "".join(t.text or "" for t in si.iterfind("m:t", _NS)),
        )
        for si in ET.fromstring(parts["xl/sharedStrings.xml"]).iterfind("m:si", _NS)
    ]
    styles_root = ET.fromstring(parts["xl/styles.xml"])
    formats = dict(_BUILTIN_FORMATS)
    formats[49] = "@"
    for fmt in styles_root.iterfind("m:numFmts/m:numFmt", _NS):
        formats[int(fmt.attrib["numFmtId"])] = fmt.attrib["formatCode"]
    xfs = list(styles_root.iterfind("m:cellXfs/m:xf", _NS))

    workbook = ET.fromstring(parts["xl/workbook.xml"])
    out: dict[str, dict[str, _Cell]] = {}
    for number, sheet in enumerate(workbook.iterfind("m:sheets/m:sheet", _NS), start=1):
        root = ET.fromstring(parts[f"xl/worksheets/sheet{number}.xml"])
        assert root.find(".//m:f", _NS) is None
        cells: dict[str, _Cell] = {}
        for c in root.iterfind("m:sheetData/m:row/m:c", _NS):
            xf = xfs[int(c.attrib.get("s", "0"))]
            raw = c.findtext("m:v", default="", namespaces=_NS)
            is_text = c.attrib.get("t") == "s"
            cells[c.attrib["r"]] = _Cell(
                kind="text" if is_text else "number",
                value=strings[int(raw)] if is_text else raw,
                num_fmt=formats[int(xf.attrib["numFmtId"])],
                quote_prefix=xf.attrib.get("quotePrefix") == "1",
                wrap=xf.find("m:alignment[@wrapText='1']", _NS) is not None,
            )
        out[sheet.attrib["name"]] = cells
    return out


@pytest.mark.parametrize(("value", "csv_value"), _CASES)
def test_xlsx_cells_follow_shared_formula_cases(tmp_path: Path, value: str, csv_value: str) -> None:
    """공용 예시마다: CSV에서 `'`가 붙는 값은 `'` 없는 글자 칸, 숫자로 두는 값은 숫자 칸."""
    out = tmp_path / TABLES_XLSX_NAME
    export_tables_xlsx([_sample_table(["값"], [[value]])], out)
    cell = _read_workbook(out)["p001_t01"].get("A2")

    if value == "":
        assert cell is None
        return
    assert cell is not None
    guarded_in_csv = csv_value != value
    if guarded_in_csv:
        assert (cell.kind, cell.value) == ("text", value)
        assert cell.quote_prefix
        assert cell.num_fmt == "@"
    elif value[0] in "+-" and value != "-":
        # 부호로 시작하는데 CSV가 그대로 두는 값은 숫자다.
        assert cell.kind == "number"
    assert not (cell.kind == "text" and cell.value.startswith("'") and not value.startswith("'"))


def test_xlsx_numbers_keep_value_and_display_format(tmp_path: Path) -> None:
    out = tmp_path / TABLES_XLSX_NAME
    values = ["-1,234", "1,234.50", "5%", "+12.5%", "-3.5", "-.5", "2024", "0", "+0", "0.10"]
    export_tables_xlsx([_sample_table(["값"], [[v] for v in values])], out)
    cells = _read_workbook(out)["p001_t01"]

    got = [
        (cells[f"A{i}"].kind, cells[f"A{i}"].value, cells[f"A{i}"].num_fmt) for i in range(2, 12)
    ]
    assert got == [
        ("number", "-1234", "#,##0"),
        ("number", "1234.5", "#,##0.00"),
        ("number", "0.05", "0%"),
        ("number", "0.125", "0.0%"),
        ("number", "-3.5", "0.0"),
        ("number", "-0.5", "0.0"),
        ("number", "2024", "0"),
        ("number", "0", "0"),
        ("number", "0", "0"),
        ("number", "0.1", "0.00"),
    ]


def test_xlsx_keeps_number_like_text_that_would_lose_digits(tmp_path: Path) -> None:
    """앞자리 0이나 15자리 넘는 값은 숫자로 바꾸면 달라지므로 글자 칸으로 둔다."""
    out = tmp_path / TABLES_XLSX_NAME
    values = ["00123", "01012345678", "1234567890123456", "123456789012345", "0.5", "1-2"]
    values.append("2024-01-05")
    export_tables_xlsx([_sample_table(["값"], [[v] for v in values])], out)
    cells = _read_workbook(out)["p001_t01"]

    got = [(cells[f"A{i}"].kind, cells[f"A{i}"].value, cells[f"A{i}"].num_fmt) for i in range(2, 9)]
    assert got == [
        ("text", "00123", "@"),
        ("text", "01012345678", "@"),
        ("text", "1234567890123456", "@"),
        ("number", "123456789012345", "0"),
        ("number", "0.5", "0.0"),
        ("text", "1-2", "General"),
        ("text", "2024-01-05", "General"),
    ]


def test_xlsx_numbers_stay_inside_excel_format_and_value_limits(tmp_path: Path) -> None:
    """소수 자리가 30개를 넘으면 글자로 두고, 부르는 쪽 Decimal 정밀도에 값이 흔들리지 않는다."""
    out = tmp_path / TABLES_XLSX_NAME
    tiny_ok = "0." + "0" * 29 + "1"
    tiny_too_long = "0." + "0" * 30 + "1"
    underflow = "0." + "0" * 323 + "1"
    values = [tiny_ok, tiny_too_long, underflow, "1234567", "123456789012345", "12.5%"]

    with decimal.localcontext() as ctx:
        ctx.prec = 6
        export_tables_xlsx([_sample_table(["값"], [[v] for v in values])], out)
    cells = _read_workbook(out)["p001_t01"]

    got = [(cells[f"A{i}"].kind, cells[f"A{i}"].value) for i in range(2, 8)]
    assert got == [
        ("number", tiny_ok),
        ("text", tiny_too_long),
        ("text", underflow),
        ("number", "1234567"),
        ("number", "123456789012345"),
        ("number", "0.125"),
    ]
    assert cells["A2"].num_fmt == "0." + "0" * 30


def test_xlsx_headers_are_always_text(tmp_path: Path) -> None:
    out = tmp_path / TABLES_XLSX_NAME
    export_tables_xlsx([_sample_table(["=구분", "2024", "금액"], [["총무팀", "1", "-"]])], out)
    cells = _read_workbook(out)["p001_t01"]

    assert (cells["A1"].kind, cells["A1"].value, cells["A1"].num_fmt) == ("text", "=구분", "@")
    assert (cells["B1"].kind, cells["B1"].value) == ("text", "2024")
    assert (cells["C1"].kind, cells["C1"].value, cells["C1"].num_fmt) == ("text", "금액", "General")
    assert (cells["B2"].kind, cells["B2"].value) == ("number", "1")
    assert (cells["C2"].kind, cells["C2"].value, cells["C2"].quote_prefix) == ("text", "-", False)


def test_xlsx_one_sheet_per_table_with_safe_unique_names(tmp_path: Path) -> None:
    out = tmp_path / TABLES_XLSX_NAME
    long_id = "p001_" + "t" * 40
    tables = [
        _sample_table(["a"], [["1"]], table_id="p001_t01"),
        _sample_table(["b"], [["2"]], table_id="P001_T01"),
        _sample_table(["c"], [["3"]], table_id="a/b:c"),
        _sample_table(["d"], [["4"]], table_id=long_id),
    ]
    skipped = export_tables_xlsx(tables, out)

    assert skipped == ()
    book = _read_workbook(out)
    assert list(book) == ["p001_t01", "P001_T01_2", "a_b_c", long_id[:31]]
    assert [sheet["A1"].value for sheet in book.values()] == ["a", "b", "c", "d"]


def test_xlsx_text_survives_xml_special_and_control_characters(tmp_path: Path) -> None:
    """`<`, `&`, 줄바꿈, 탭, CR, 제어 문자, `_x000D_` 꼴 글이 엑셀 표기대로 담긴다."""
    out = tmp_path / "book.xlsx"
    texts = ["a<b>&\"c'", "첫 줄\n둘째 줄", "\t=1+1", "\r=1+1", "벨\x07", "_x000D_ 그대로"]
    texts.append(" 앞뒤 공백 ")
    write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText(t)] for t in texts])])

    with zipfile.ZipFile(out) as zf:
        raw = zf.read("xl/sharedStrings.xml").decode("utf-8")
    assert '<t xml:space="preserve">a&lt;b&gt;&amp;"c\'</t>' in raw
    assert "_x000D_=1+1" in raw
    assert "벨_x0007_" in raw
    assert "_x005F_x000D_ 그대로" in raw
    cells = _read_workbook(out)["s"]
    assert cells["A2"].value == "첫 줄\n둘째 줄"
    assert cells["A2"].wrap
    assert not cells["A1"].wrap
    assert cells["A3"].value == "\t=1+1"
    assert cells["A4"].value == "\r=1+1"
    assert cells["A5"].value == "벨\x07"
    assert cells["A6"].value == "_x000D_ 그대로"
    assert cells["A7"].value == " 앞뒤 공백 "


def test_xlsx_long_forced_text_skips_text_format(tmp_path: Path) -> None:
    """255자를 넘는 글자 칸은 `####`로 보이지 않게 글자 서식 없이 quotePrefix만 쓴다."""
    out = tmp_path / "book.xlsx"
    long_text = "- " + "가" * 300
    write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText(long_text, force_text=True)]])])
    cell = _read_workbook(out)["s"]["A1"]

    assert (cell.value, cell.num_fmt, cell.quote_prefix) == (long_text, "General", True)


def test_xlsx_writer_is_deterministic_and_rejects_oversized_cells(tmp_path: Path) -> None:
    sheet = XlsxSheet("s", [[XlsxText("가"), XlsxNumber("1.5", "0.0")], [None, XlsxText("")]])
    write_xlsx_workbook(tmp_path / "a.xlsx", [sheet])
    write_xlsx_workbook(tmp_path / "b.xlsx", [sheet])
    assert (tmp_path / "a.xlsx").read_bytes() == (tmp_path / "b.xlsx").read_bytes()
    assert list(_read_workbook(tmp_path / "a.xlsx")["s"]) == ["A1", "B1"]

    too_long = XlsxSheet("s", [[XlsxText("가" * (XLSX_MAX_CELL_CHARS + 1))]])
    with pytest.raises(XlsxLimitError):
        write_xlsx_workbook(tmp_path / "c.xlsx", [too_long])
    assert not (tmp_path / "c.xlsx").exists()
    with pytest.raises(ValueError, match="시트"):
        write_xlsx_workbook(tmp_path / "d.xlsx", [])


def test_xlsx_writer_keeps_the_previous_file_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """쓰다가 실패하면 이전 파일이 그대로 남고, 쓰다 만 파일이나 임시 파일이 남지 않는다."""
    out = tmp_path / "book.xlsx"
    write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText("이전")]])])
    before = out.read_bytes()

    def _fail(self: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr("autoetl.parser.xlsx_writer._SharedStrings.to_xml", _fail)
    with pytest.raises(OSError, match="disk full"):
        write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText("새 값")]])])

    assert out.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["book.xlsx"]


def test_xlsx_writer_leaves_an_unrelated_tmp_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """출력 파일 옆에 사용자가 둔 `<이름>.tmp` 파일은 성공해도 실패해도 그대로 남는다."""
    out = tmp_path / "book.xlsx"
    bystander = tmp_path / "book.xlsx.tmp"
    bystander.write_bytes(b"not ours")

    write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText("값")]])])
    assert bystander.read_bytes() == b"not ours"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["book.xlsx", "book.xlsx.tmp"]

    def _fail(self: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr("autoetl.parser.xlsx_writer._SharedStrings.to_xml", _fail)
    with pytest.raises(OSError, match="disk full"):
        write_xlsx_workbook(out, [XlsxSheet("s", [[XlsxText("새 값")]])])
    assert bystander.read_bytes() == b"not ours"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["book.xlsx", "book.xlsx.tmp"]


def test_export_tables_xlsx_replaces_oversized_table_with_notice(tmp_path: Path) -> None:
    out = tmp_path / TABLES_XLSX_NAME
    tables = [
        _sample_table(["a"], [["=1+1"]], table_id="p001_t01"),
        _sample_table(["b"], [["가" * (XLSX_MAX_CELL_CHARS + 1)]], table_id="p002_t01"),
    ]
    skipped = export_tables_xlsx(tables, out)

    assert skipped == ("p002_t01",)
    book = _read_workbook(out)
    assert book["p001_t01"]["A2"].value == "=1+1"
    assert list(book["p002_t01"]) == ["A1"]
    assert "tables/table_p002_t01.csv" in book["p002_t01"]["A1"].value


def _write_formula_docx(path: Path) -> Path:
    """수식처럼 보이는 칸과 숫자 칸이 든 표 하나짜리 DOCX."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def cell(text: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

    rows = [["구분", "값"], ["=1+1", "-1,234"], ["@SUM(A1:A2)", "5%"], ["- 항목", "00123"]]
    body = "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{ns}"><w:body><w:tbl>{body}</w:tbl></w:body></w:document>',
        )
    return path


def test_bundle_and_cli_write_tables_xlsx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`parse_and_export`(화면 ZIP과 같은 경로)와 CLI가 CSV 옆에 `tables.xlsx`를 함께 만든다."""
    docx = _write_formula_docx(tmp_path / "in" / "표.docx")
    out_dir = tmp_path / "out"

    doc, manifest = parse_and_export(docx, out_dir)

    assert len(doc.tables) == 1
    assert manifest.tables_xlsx == out_dir / TABLES_XLSX_NAME
    assert manifest.xlsx_skipped_table_ids == ()
    assert len(manifest.table_csv_paths) == 1
    # CSV는 그대로 '를 붙인다.
    assert "'=1+1" in manifest.table_csv_paths[0].read_text(encoding="utf-8-sig")
    table = doc.tables[0]
    cells = _read_workbook(manifest.tables_xlsx)[table.table_id]
    flat = {(c.kind, c.value) for c in cells.values()}
    assert {
        ("text", "=1+1"),
        ("text", "@SUM(A1:A2)"),
        ("text", "- 항목"),
        ("text", "00123"),
    } <= flat
    assert {("number", "-1234"), ("number", "0.05")} <= flat
    assert all(not c.value.startswith("'") for c in cells.values())

    cli_out = tmp_path / "cli_out"
    assert main(["parse", str(docx), "--out", str(cli_out)]) == 0
    printed = capsys.readouterr().out
    assert "추출 표 CSV/JSON: 1개" in printed
    assert f"추출 표 엑셀 파일: {cli_out / TABLES_XLSX_NAME}" in printed
    assert (cli_out / TABLES_XLSX_NAME).read_bytes() == manifest.tables_xlsx.read_bytes()


def test_ui_zip_download_holds_tables_xlsx(tmp_path: Path) -> None:
    """화면의 `/api/parse` → ZIP 내려받기에도 CSV와 함께 `tables.xlsx`가 들어 있다."""
    docx = _write_formula_docx(tmp_path / "표.docx")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps(
        {
            "filename": "table.docx",
            "content_base64": base64.b64encode(docx.read_bytes()).decode("ascii"),
        }
    ).encode("utf-8")

    with create_ui_server(host="127.0.0.1", port=0) as srv:
        req = urllib.request.Request(
            f"{srv.url}/api/parse",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
        with opener.open(f"{srv.url}/api/download/{parsed['bundle_id']}") as resp:
            zip_bytes = resp.read()

    table_id = parsed["tables"][0]["table_id"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        assert {TABLES_XLSX_NAME, f"tables/table_{table_id}.csv"} <= names
        (tmp_path / TABLES_XLSX_NAME).write_bytes(zf.read(TABLES_XLSX_NAME))
    cells = _read_workbook(tmp_path / TABLES_XLSX_NAME)[table_id]
    assert (cells["A2"].kind, cells["A2"].value) == ("text", "=1+1")
    assert (cells["B2"].kind, cells["B2"].value) == ("number", "-1234")


def test_bundle_without_tables_writes_no_workbook(tmp_path: Path) -> None:
    path = tmp_path / "글만.docx"
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>본문</w:t></w:r></w:p>'
            "</w:body></w:document>",
        )

    # 같은 출력 폴더에 이전 문서의 표 파일이 남아 있던 경우.
    stale = tmp_path / "out" / TABLES_XLSX_NAME
    stale.parent.mkdir()
    stale.write_bytes(b"old")

    _doc, manifest = parse_and_export(path, tmp_path / "out")

    assert manifest.tables_xlsx is None
    assert not stale.exists()


def test_export_never_overwrites_or_removes_the_source_workbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """입력이 출력 폴더의 `tables.xlsx` 자신이면 그 파일을 그대로 두고 엑셀 내보내기만 건너뛴다."""
    work = tmp_path / "work"
    source = work / TABLES_XLSX_NAME
    # 다시 내보낸 결과와 구별되도록 시트 이름과 칸 형식을 다르게 쓴다.
    rows = [[XlsxText("구분"), XlsxText("금액")], [XlsxText("총무팀"), XlsxText("1,234")]]
    write_xlsx_workbook(source, [XlsxSheet("원본", rows)])
    original = source.read_bytes()

    doc, manifest = parse_and_export(source, work)

    assert len(doc.tables) == 1
    assert manifest.tables_xlsx is None
    assert manifest.xlsx_blocked_by_source
    assert len(manifest.table_csv_paths) == 1
    assert source.read_bytes() == original

    assert main(["parse", str(source), "--out", str(work)]) == 0
    printed = capsys.readouterr().out
    assert "추출 표 엑셀 파일: 만들지 않음" in printed
    assert source.read_bytes() == original

    # 표가 없는 문서를 내보낼 때도 원본 자리의 파일은 지우지 않는다.
    empty_doc = doc.model_copy(update={"tables": []})
    blocked = export_parse_bundle(empty_doc, work, source_file=source)
    assert blocked.xlsx_blocked_by_source
    assert source.read_bytes() == original
    # 원본이 다른 파일이면 보통대로 쓴다.
    other = export_parse_bundle(doc, tmp_path / "elsewhere", source_file=source)
    assert other.tables_xlsx == tmp_path / "elsewhere" / TABLES_XLSX_NAME
    assert not other.xlsx_blocked_by_source


def test_exported_workbook_reads_back_with_the_xlsx_parser(tmp_path: Path) -> None:
    """내보낸 파일을 이 저장소의 XLSX 파서가 다시 읽을 수 있다(형식이 맞는지 확인)."""
    out = tmp_path / "book.xlsx"
    export_tables_xlsx(
        [_sample_table(["구분", "금액"], [["=1+1", "-1,234"], ["총무팀", "5%"]])], out
    )

    doc = parse_document(out)

    assert len(doc.tables) == 1
    assert doc.tables[0].headers == ["구분", "금액"]
    assert doc.tables[0].rows == [["=1+1", "-1,234"], ["총무팀", "5%"]]


def test_folder_scans_skip_the_bundle_workbook(tmp_path: Path) -> None:
    """폴더를 훑는 parse·analyze 입력 탐색이 번들 속 `tables.xlsx`를 다시 읽지 않는다."""
    in_dir = tmp_path / "docs"
    docx = _write_formula_docx(in_dir / "표.docx")
    bundle = in_dir / "out"
    parse_and_export(docx, bundle)
    assert is_bundle_tables_workbook(bundle / TABLES_XLSX_NAME)

    # 번들이 아닌 곳의 같은 이름 파일은 보통 입력이다.
    loose = in_dir / "mine" / TABLES_XLSX_NAME
    loose.parent.mkdir()
    loose.write_bytes((bundle / TABLES_XLSX_NAME).read_bytes())
    assert not is_bundle_tables_workbook(loose)

    batch_found = [p.relative_to(in_dir).as_posix() for p in discover_batch_files(in_dir)]
    assert batch_found == ["mine/tables.xlsx", "표.docx"]

    table_found = [p.relative_to(in_dir).as_posix() for p in discover_table_sources(in_dir)]
    assert table_found == ["mine/tables.xlsx", "out/tables/table_p001_t01.csv"]
    # 파일을 직접 가리키면 그대로 받는다.
    assert discover_table_sources(bundle / TABLES_XLSX_NAME) == [bundle / TABLES_XLSX_NAME]
