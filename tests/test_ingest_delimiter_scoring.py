"""Regression tests for delimiter scoring after record sampling falls back."""

from pathlib import Path

import pytest

from autoetl.core.errors import InputError
from autoetl.core.stages.s00_ingest import probe_source


@pytest.mark.parametrize("line_width", [3900, 4], ids=["fallback", "logical-records"])
def test_semicolon_sample_with_commas_in_multiline_field(tmp_path: Path, line_width: int) -> None:
    source = tmp_path / "report.csv"
    line = ("x" * line_width) + ", east, west"
    cell = "\n".join(line for _ in range(18))
    note = "b" * (line_width * 18)
    source.write_text(f'Report Title\ncode;note\n"{cell}";{note}\n', encoding="utf-8")

    spec = probe_source(source)

    assert spec.delimiter == ";"
    assert spec.row_count == 2


def test_comma_sample_with_oversized_multiline_field_reports_input_error(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    line = "x" * 70_000
    source.write_text(f'code,note\n001,"{line}\n{line}"\n', encoding="utf-8")

    with pytest.raises(InputError, match="입력 CSV를 끝까지 읽지 못했다"):
        probe_source(source)
