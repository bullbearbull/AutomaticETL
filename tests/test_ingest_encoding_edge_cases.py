"""Generated-file regressions for S0 inference and raw ingestion."""

import codecs
import tempfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from autoetl.contracts import IngestFormat
from autoetl.core.context import AnalysisContext
from autoetl.core.errors import InputError
from autoetl.core.multi_context import MultiTableContext, profile_all_tables
from autoetl.core.stages import s00_ingest
from autoetl.core.stages.s00_ingest import probe_source


def test_delimiter_ignores_commas_inside_multiline_quoted_field(tmp_path: Path) -> None:
    source = tmp_path / "inventory.csv"
    description = "north, east\nnear, far\nabove, below\ninside, outside"
    source.write_text(
        f'code;description;qty\n001;"{description}";2\n', encoding="utf-8", newline=""
    )

    spec = probe_source(source)
    assert spec.delimiter == ";"
    assert spec.header == ["code", "description", "qty"]
    assert spec.row_count == 1

    with AnalysisContext(source, spec=spec, threads=1) as context:
        assert context.connection.execute("SELECT * FROM raw").fetchall() == [
            ("001", description, "2")
        ]


def test_delimiter_ignores_blank_lines_before_pipe_header(tmp_path: Path) -> None:
    source = tmp_path / "inventory.csv"
    source.write_text("\n\n\n\ncode|label|qty\n001|Dune|2\n\n", encoding="utf-8")

    spec = probe_source(source)
    assert spec.delimiter == "|"
    assert spec.skip_lines == 5
    assert spec.row_count == 1

    with AnalysisContext(source, spec=spec, threads=1) as context:
        assert context.connection.execute("SELECT * FROM raw").fetchall() == [("001", "Dune", "2")]


def test_delimiter_survives_wrong_candidate_exceeding_the_field_limit(tmp_path: Path) -> None:
    source = tmp_path / "notes.csv"
    first = ("a" * 35_000) + "\n" + ("a" * 35_000)
    second = "b" * 70_000
    source.write_text(f'first;second\n"{first}";{second}\n', encoding="utf-8", newline="")

    spec = probe_source(source)
    assert spec.delimiter == ";"
    assert spec.header == ["first", "second"]
    assert spec.row_count == 1


def test_delimiter_ignores_record_cut_at_the_sample_boundary(tmp_path: Path) -> None:
    source = tmp_path / "notes.csv"
    note = "\n".join(f"line {index}, east, west" for index in range(30))
    record = f'001|"{note}"|2|open\n'
    source.write_text("code|note|qty|state\n" + (record * 3), encoding="utf-8", newline="")

    spec = probe_source(source)
    assert spec.delimiter == "|"
    assert spec.header == ["code", "note", "qty", "state"]
    assert spec.row_count == 3


def test_delimiter_keeps_complete_last_record_when_sample_is_full(tmp_path: Path) -> None:
    source = tmp_path / "notes.csv"
    source.write_text("a;b;c;d,x,y\n1,2,3\n" + ("\n" * 48), encoding="utf-8", newline="")

    spec = probe_source(source)
    assert spec.delimiter == ","
    assert spec.header == ["a;b;c;d", "x", "y"]
    assert spec.row_count == 1


def test_probe_rejects_oversized_cell_that_another_delimiter_would_split(
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.csv"
    label = "fake-private-cell"
    cell = label + (";x" * 65_536)
    source.write_text(f"code,label\n001,{cell}\n", encoding="utf-8", newline="")

    with pytest.raises(InputError, match="입력 CSV를 끝까지 읽지 못했다") as error:
        probe_source(source)

    assert label not in str(error.value)


def test_tab_delimiter_preserves_quoted_tab_and_other_delimiters(tmp_path: Path) -> None:
    source = tmp_path / "inventory.tsv"
    label = "north\teast; west, south"
    source.write_text(f'code\tlabel\n001\t"{label}"\n', encoding="utf-8")

    with AnalysisContext(source, threads=1) as context:
        assert context.spec.delimiter == "\t"
        assert context.connection.execute("SELECT * FROM raw").fetchall() == [("001", label)]


@pytest.mark.parametrize(
    ("byte_encoding", "bom", "detected_encoding"),
    [
        pytest.param("utf-8", codecs.BOM_UTF8, "utf-8", id="utf8-bom"),
        pytest.param("utf-16-le", codecs.BOM_UTF16_LE, "utf-16", id="utf16-le"),
        pytest.param("utf-16-be", codecs.BOM_UTF16_BE, "utf-16", id="utf16-be"),
    ],
)
def test_bom_source_preserves_headers_and_raw_values(
    tmp_path: Path, byte_encoding: str, bom: bytes, detected_encoding: str
) -> None:
    source = tmp_path / "inventory.csv"
    records = '품목;label;qty\r\n001;"Étoile\r\n북쪽";02\r\n002;Δείγμα;03\r\n'
    encoded = bom + records.encode(byte_encoding)
    source.write_bytes(encoded)

    spec = probe_source(source)
    with AnalysisContext(source, spec=spec, threads=1) as context:
        assert spec.encoding == detected_encoding
        assert spec.converted is (detected_encoding == "utf-16")
        assert spec.size_bytes == len(encoded)
        assert spec.delimiter == ";"
        assert spec.header_row_count == 1
        assert spec.header == ["품목", "label", "qty"]
        assert spec.warnings == []
        assert context.connection.execute("SELECT * FROM raw ORDER BY c0").fetchall() == [
            ("001", "Étoile\r\n북쪽", "02"),
            ("002", "Δείγμα", "03"),
        ]
        if spec.converted:
            assert spec.load_path != source
            assert spec.load_path.read_bytes() == records.encode("utf-8")
        else:
            assert spec.load_path == source

    if spec.converted:
        assert not spec.load_path.exists()
    assert source.read_bytes() == encoded


def test_windows_1252_source_preserves_western_characters(tmp_path: Path) -> None:
    source = tmp_path / "catalog.csv"
    label = "“Café” — Crème brûlée costs €4.50; Œuvre sample uses ƒ(x)."
    source.write_bytes(f'code,label\r\n001,"{label}"\r\n'.encode("cp1252"))

    with AnalysisContext(source, threads=1) as context:
        assert context.spec.encoding == "cp1252"
        assert context.spec.converted
        assert any("1바이트 코드페이지" in warning for warning in context.spec.warnings)
        assert context.connection.execute("SELECT * FROM raw").fetchall() == [("001", label)]


def test_cp949_with_late_non_ascii_preserves_its_value(tmp_path: Path) -> None:
    source = tmp_path / "inventory.csv"
    ascii_rows = "".join(f"{index:03d},{'A' * 1024}\n" for index in range(1, 101))
    source.write_bytes(f"code,label\n{ascii_rows}101,한글 표본\n".encode("cp949"))

    with AnalysisContext(source, threads=1) as context:
        assert context.spec.encoding == "cp949"
        assert context.spec.converted
        assert context.spec.warnings == []
        assert context.row_count == 101
        assert context.connection.execute("SELECT c1 FROM raw WHERE c0 = '101'").fetchone() == (
            "한글 표본",
        )


def test_multirow_header_uses_last_row_and_physical_skip(tmp_path: Path) -> None:
    source = tmp_path / "inventory.csv"
    source.write_text(
        '\n"Inventory\nfields";Metrics;Labels\n SKU ; Quantity ;\n'
        "001;12;Boreal\n002;13;Dune\n003;14;Larch\n004;15;Lime\n005;16;Nori\n",
        encoding="utf-8",
    )

    spec = probe_source(source)
    assert spec.delimiter == ";"
    assert spec.header_row_count == 2
    assert spec.skip_lines == 4
    assert spec.header == ["SKU", "Quantity", "col_3"]
    assert spec.row_count == 5
    assert any("다중 행 헤더" in item for item in spec.applied_normalizations)

    with AnalysisContext(source, spec=spec, threads=1) as context:
        assert context.connection.execute("SELECT * FROM raw ORDER BY c0 LIMIT 1").fetchone() == (
            "001",
            "12",
            "Boreal",
        )


def test_headerless_data_retains_first_row_with_shorter_serial(tmp_path: Path) -> None:
    source = tmp_path / "readings.csv"
    source.write_text(
        "1,2026-11-17,5.25\n10,2026-11-18,5.50\n11,2026-11-19,5.75\n"
        "12,2026-11-20,5.85\n13,2026-11-21,5.95\n14,2026-11-22,6.25\n",
        encoding="utf-8",
    )

    spec = probe_source(source)
    assert spec.header_row_count == 0
    assert spec.skip_lines == 0
    assert spec.header == ["col_1", "col_2", "col_3"]
    assert spec.row_count == 6
    assert any("헤더 행이 없다고" in warning for warning in spec.warnings)

    with AnalysisContext(source, spec=spec, threads=1) as context:
        assert context.connection.execute("SELECT * FROM raw WHERE c0 = '1'").fetchone() == (
            "1",
            "2026-11-17",
            "5.25",
        )


def test_year_headers_remain_headers_when_data_have_more_digits(tmp_path: Path) -> None:
    source = tmp_path / "counts.csv"
    source.write_text(
        "2020,2021,zone\n100000,100001,A\n100002,100003,B\n100004,100005,C\n"
        "100006,100007,D\n100008,100009,E\n100010,100011,F\n",
        encoding="utf-8",
    )

    spec = probe_source(source)
    assert spec.header_row_count == 1
    assert spec.header == ["2020", "2021", "zone"]
    assert spec.row_count == 6
    assert any("자릿수가 데이터 자릿수 범위 밖" in warning for warning in spec.warnings)


def test_missing_markers_do_not_promote_first_data_row_to_header(tmp_path: Path) -> None:
    source = tmp_path / "readings.csv"
    source.write_text(
        "qty,day,label\nNA,?,edge-only-cell\n10,2026-11-18,Dune\n11,2026-11-19,Larch\n"
        "12,2026-11-20,Lime\n13,2026-11-21,Nori\n14,2026-11-22,Fir\n",
        encoding="utf-8",
    )

    with AnalysisContext(source, threads=1) as context:
        assert context.spec.header_row_count == 1
        assert context.spec.header == ["qty", "day", "label"]
        assert context.row_count == 6
        assert context.connection.execute(
            "SELECT * FROM raw WHERE c2 = 'edge-only-cell'"
        ).fetchone() == ("NA", "?", "edge-only-cell")


def test_manifest_keeps_blank_and_duplicate_headers_addressable(tmp_path: Path) -> None:
    source = tmp_path / "inventory.csv"
    source.write_text(" item , ,item,item#2\n001,Dune,03,Larch\n", encoding="utf-8")

    with AnalysisContext(source, threads=1) as context:
        manifest = s00_ingest.run(context, context.spec)

    assert s00_ingest.source_names(manifest) == ["item", "col_2", "item#2", "item#2#2"]
    assert [column.index for column in manifest.columns] == [0, 1, 2, 3]
    assert manifest.files[0].path == "inventory.csv"
    assert manifest.files[0].format is IngestFormat.CSV
    assert manifest.files[0].encoding == "utf-8"
    assert manifest.header_row_count == 1
    assert manifest.row_count == 1


@pytest.mark.parametrize("contents", ["", "\r\n\r\n"], ids=["empty-file", "blank-lines"])
def test_probe_rejects_csv_without_records(tmp_path: Path, contents: str) -> None:
    source = tmp_path / "empty.csv"
    source.write_bytes(contents.encode("utf-8"))

    with pytest.raises(InputError, match="빈 CSV"):
        probe_source(source)


@pytest.mark.parametrize("preceding_rows", [0, 52], ids=["delimiter-sample", "full-scan"])
def test_probe_omits_oversized_cell_from_error_and_cleans_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preceding_rows: int
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    source = tmp_path / "oversized.csv"
    label = "fake-private-cell"
    text = "code,label\n" + ("002,small\n" * preceding_rows) + f"001,{label}"
    source.write_bytes((text + ("x" * 131_072) + "\n").encode("utf-16"))

    with pytest.raises(InputError, match="입력 CSV를 끝까지 읽지 못했다") as error:
        probe_source(source)

    assert label not in str(error.value)
    assert not list(tmp_path.glob("*.autoetl-utf8.csv"))
    if preceding_rows:
        assert "마지막으로 읽은 행 번호 53" in str(error.value)


def test_xlsx_ingestion_reports_sheet_and_preserves_raw_text(tmp_path: Path) -> None:
    source = tmp_path / "inventory.xlsx"
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    spreadsheet = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    rows = (("code", "label"), ("001", "Café"), ("002", "북쪽"))
    sheet_rows = []
    for index, (code, label) in enumerate(rows, start=1):
        sheet_rows.append(
            f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{code}</t></is></c>'
            f'<c r="B{index}" t="inlineStr"><is><t>{label}</t></is></c></row>'
        )
    with ZipFile(source, "w") as book:
        book.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{spreadsheet}.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            f'ContentType="{spreadsheet}.worksheet+xml"/></Types>',
        )
        book.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{package}"><Relationship Id="rId1" '
            f'Type="{office}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        book.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{office}"><sheets>'
            '<sheet name="Inventory" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{package}"><Relationship Id="rId1" '
            f'Type="{office}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        book.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>',
        )

    with MultiTableContext(source, threads=1) as context:
        (bundle,) = profile_all_tables(context)
        (file,) = bundle.ingest.files
        assert file.path == "inventory.xlsx"
        assert file.format is IngestFormat.XLSX
        assert file.encoding == "utf-8"
        assert file.sheet == "Inventory"
        assert bundle.ingest.header_row_count == 1
        assert s00_ingest.source_names(bundle.ingest) == ["code", "label"]
        assert bundle.ingest.row_count == 2
        assert context.connection is not None
        assert context.connection.execute("SELECT * FROM raw ORDER BY c0").fetchall() == [
            ("001", "Café"),
            ("002", "북쪽"),
        ]
