"""표 칸 값이 스프레드시트에서 수식으로 실행되지 않게 막는 규칙 테스트(CSV 파일·화면용 CSV 글)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from autoetl.parser import BBox, ExtractedTable, ParseStrategy
from autoetl.parser.exporters import export_table_csv, spreadsheet_safe_cell
from autoetl.ui.server import _table_to_csv_text

_FIXTURE = Path(__file__).parent / "fixtures" / "spreadsheet_formula_cells.json"
_CASES: list[tuple[str, str]] = [
    (case["input"], case["expected"])
    for case in json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]
]


def _sample_table(headers: list[str], rows: list[list[str]]) -> ExtractedTable:
    return ExtractedTable(
        table_id="sample",
        page=1,
        bbox=BBox(x0=0, top=0, x1=1, bottom=1, norm_x0=0, norm_top=0, norm_x1=1, norm_bottom=1),
        strategy=ParseStrategy.RULED_TABLE,
        confidence=1.0,
        row_count=len(rows) + 1,
        col_count=len(headers),
        headers=headers,
        rows=rows,
    )


@pytest.mark.parametrize(("value", "expected"), _CASES)
def test_spreadsheet_safe_cell_follows_shared_cases(value: str, expected: str) -> None:
    assert spreadsheet_safe_cell(value) == expected


def test_export_table_csv_guards_formula_cells(tmp_path: Path) -> None:
    """헤더와 본문 칸 모두 규칙을 거치고, 숫자와 보통 글은 그대로 남는다."""
    table = _sample_table(
        ["=구분", "금액"],
        [["=1+1", "-1,234"], ["@SUM(A1:A2)", "+5%"], ["총무팀", "-"]],
    )
    csv_path = tmp_path / "table_sample.csv"

    export_table_csv(table, csv_path)

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        assert list(csv.reader(fh)) == [
            ["'=구분", "금액"],
            ["'=1+1", "-1,234"],
            ["'@SUM(A1:A2)", "+5%"],
            ["총무팀", "-"],
        ]


def test_table_to_csv_text_guards_formula_cells() -> None:
    """화면 복사용 CSV 글(`/api/parse`의 표 `csv`)도 같은 규칙을 쓴다."""
    text = _table_to_csv_text(["=구분", "금액"], [["-2+3", "-3.5"], ["\t=1+1", "1+1"]])

    assert list(csv.reader(io.StringIO(text, newline=""))) == [
        ["'=구분", "금액"],
        ["'-2+3", "-3.5"],
        ["'\t=1+1", "1+1"],
    ]
    assert _table_to_csv_text([], [["=1+1"]]) == "'=1+1\r\n"
