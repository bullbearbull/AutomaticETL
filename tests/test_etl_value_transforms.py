"""Observable value transforms and privacy guarantees for real ETL loads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from autoetl.contracts import (
    ColumnMapping,
    ColumnOverride,
    MaskingPolicy,
    NullHandling,
    PhysicalType,
    SchemaOverrideSpec,
    SensitiveType,
    TargetColumn,
)
from autoetl.core.db_loader import (
    _parse_temporal_date,
    _parse_temporal_time,
    _parse_temporal_timestamp,
    _transform_cell,
    inspect_db_catalog,
    load_tables,
    mask_value,
)
from autoetl.core.multi_context import NamedTableInput


@pytest.mark.parametrize(
    ("sensitive_type", "raw", "expected"),
    [
        (SensitiveType.PHONE_NUMBER, " +00.000.0000 ", "+00.***.0000"),
        (SensitiveType.PHONE_NUMBER, "+1 (202) 555-0142", "****-0142"),
        (SensitiveType.PHONE_NUMBER, "000", "0*0"),
        (SensitiveType.EMAIL, "ab@example.test", "a***@example.test"),
        (SensitiveType.EMAIL, "alpha@example.test", "al***@example.test"),
        (SensitiveType.RESIDENT_REGISTRATION_NUMBER, "000000-1000000", "000000-1******"),
        (SensitiveType.FOREIGNER_REGISTRATION_NUMBER, "0000005000000", "000000-5******"),
        (SensitiveType.RESIDENT_REGISTRATION_NUMBER, "000000/0000000", "000000/*******"),
        (SensitiveType.CARD_NUMBER, "4111 1111 1111 1111", "4111-****-****-1111"),
        (SensitiveType.BANK_ACCOUNT, "000-0000-0000", "000-***-***000"),
        (SensitiveType.DATE_OF_BIRTH, "1996년 2월 29일", "1996-**-**"),
        (SensitiveType.DATE_OF_BIRTH, "year unavailable", "****-**-**"),
        (SensitiveType.NAME, "가", "*"),
        (SensitiveType.NAME, "가나", "가*"),
        (SensitiveType.NAME, "가나다라", "가**라"),
        (SensitiveType.ADDRESS, "Test District Imaginary-Road 000", "Test District ***"),
        (SensitiveType.ADDRESS, "TestBlock", "Test***"),
        (SensitiveType.IP_ADDRESS, "203.0.113.7", "203.0.113.***"),
        (SensitiveType.BUSINESS_REGISTRATION_NUMBER, "000-00-00000", "000-**-***00"),
        (None, " ", ""),
        (None, "ab", "**"),
        (None, "abcd", "a**d"),
        (None, "abcde", "ab*de"),
        (None, "fixture-token-7", "fi******-7"),
    ],
)
def test_mask_value_redacts_each_sensitive_format(
    sensitive_type: SensitiveType | None, raw: str, expected: str
) -> None:
    assert mask_value(raw, sensitive_type, MaskingPolicy.MASK) == expected


def test_none_preserves_raw_value_and_hash_uses_all_utf8_bytes() -> None:
    raw = " 가상@example.test "
    assert mask_value(raw, SensitiveType.EMAIL, MaskingPolicy.NONE) == raw
    assert (
        mask_value(raw, SensitiveType.EMAIL, MaskingPolicy.HASH, hash_key=bytes(32))
        == "h_a64de444fda5d4c6e16de755"
    )
    assert (
        mask_value(raw, None, MaskingPolicy.HASH, hash_key=bytes(32))
        == "h_a64de444fda5d4c6e16de755"
    )
    assert (
        mask_value(raw.strip(), SensitiveType.EMAIL, MaskingPolicy.HASH, hash_key=bytes(32))
        == "h_c88c6457d9b4992bd673bfc1"
    )


def test_load_stores_typed_dates_times_and_numbers_and_quarantines_invalid_date(
    tmp_path: Path,
) -> None:
    source = NamedTableInput(
        name="measurements",
        headers=(
            "sample_id",
            "event_date",
            "local_date",
            "logged_at",
            "clock_time",
            "is_active",
            "amount",
            "reading",
        ),
        rows=(
            (
                "101",
                "2024-02-29",
                "2024년 2월 29일",
                "2024-02-29T04:03:02.125",
                "4:03",
                " TRUE ",
                "₩1,234.50",
                "1e2",
            ),
            (
                "102",
                "2024/03/01",
                "2024년3월1일",
                "2024-03-01 00:00:00",
                "23:59:59",
                "false",
                "-1,000원",
                "-2.5e1",
            ),
            (
                "103",
                "2024.03.02",
                "2024년 3월 2일",
                "2024-03-02T14:00:00",
                "00:00",
                "True",
                "$0.25",
                "0e0",
            ),
            (
                "104",
                "20240303",
                "2024년 3월 3일",
                "2024-03-03T20:15:00",
                "17:05:04",
                "False",
                "0.00",
                "8e0",
            ),
            (
                "105",
                "45292",
                "2024년 3월 4일",
                "2024-03-04T20:15:00",
                "12:30",
                "True",
                "25.00",
                "9e0",
            ),
            (
                "106",
                "2024-02-29",
                "2024년 2월 30일",
                "2024-03-05T20:15:00",
                "07:30",
                "False",
                "12.34",
                "2e0",
            ),
        ),
    )
    db_path = tmp_path / "measurements.duckdb"

    _, result = load_tables(extra_tables=[source], db_path=db_path, engine="duckdb")

    assert (result.total_rows_read, result.total_rows_loaded, result.total_rows_quarantined) == (
        6,
        5,
        1,
    )
    with duckdb.connect(str(db_path), read_only=True) as db:
        rows = db.execute(
            "SELECT sample_id, event_date, local_date, logged_at, clock_time, is_active, "
            "amount, reading FROM measurements ORDER BY sample_id"
        ).fetchall()
        (error,) = db.execute(
            "SELECT source_row_number, error_category, error_column FROM _etl_quarantine_rows"
        ).fetchall()

    assert rows == [
        (
            101,
            date(2024, 2, 29),
            date(2024, 2, 29),
            datetime(2024, 2, 29, 4, 3, 2, 125000),
            time(4, 3),
            True,
            Decimal("1234.50"),
            100.0,
        ),
        (
            102,
            date(2024, 3, 1),
            date(2024, 3, 1),
            datetime(2024, 3, 1),
            time(23, 59, 59),
            False,
            Decimal("-1000"),
            -25.0,
        ),
        (
            103,
            date(2024, 3, 2),
            date(2024, 3, 2),
            datetime(2024, 3, 2, 14),
            time(0),
            True,
            Decimal("0.25"),
            0.0,
        ),
        (
            104,
            date(2024, 3, 3),
            date(2024, 3, 3),
            datetime(2024, 3, 3, 20, 15),
            time(17, 5, 4),
            False,
            Decimal("0"),
            8.0,
        ),
        (
            105,
            date(2024, 1, 1),
            date(2024, 3, 4),
            datetime(2024, 3, 4, 20, 15),
            time(12, 30),
            True,
            Decimal("25"),
            9.0,
        ),
    ]
    assert [type(value) for value in rows[0]] == [
        int,
        date,
        date,
        datetime,
        time,
        bool,
        Decimal,
        float,
    ]
    assert error == (6, "cast_error", "local_date")


def test_load_accepts_a_single_digit_hour_with_fractional_seconds(tmp_path: Path) -> None:
    # Leave sufficient matching TIME values for inference at the normal threshold.
    regular_rows = ((str(index), "04:03:00") for index in range(1, 60))
    source = NamedTableInput(
        name="clock_samples",
        headers=("sample_id", "clock_time"),
        rows=(*regular_rows, ("60", "4:03:02.125")),
    )
    db_path = tmp_path / "clock.sqlite"

    _, result = load_tables(extra_tables=[source], db_path=db_path)

    assert result.total_rows_quarantined == 0
    assert result.total_rows_loaded == 60
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute(
            "SELECT clock_time FROM clock_samples WHERE sample_id = 60"
        ).fetchone() == ("04:03:02.125000",)


def test_quarantine_masks_detected_ip_without_an_explicit_policy(tmp_path: Path) -> None:
    source = NamedTableInput(
        name="samples",
        headers=("record_id", "sample_code", "ip_address"),
        rows=(
            ("101", "A1", "192.0.2.7"),
            ("102", "B2", "192.0.2.8"),
            ("103", "A1", "192.0.2.9"),
        ),
    )
    overrides = SchemaOverrideSpec(
        columns=[ColumnOverride(table="samples", source_column="sample_code", unique=True)]
    )
    db_path = tmp_path / "samples.sqlite"

    _, result = load_tables(extra_tables=[source], db_path=db_path, overrides=overrides)

    assert (result.total_rows_loaded, result.total_rows_quarantined) == (2, 1)
    catalog = inspect_db_catalog(db_path)
    ip_column = next(col for col in catalog["columns"] if col["source_column"] == "ip_address")
    assert (ip_column["pii_level"], ip_column["masking_policy"]) == ("L1", "none")
    (rejected,) = catalog["quarantine"]
    assert (rejected["source_row_number"], rejected["error_category"]) == (3, "unique_violation")
    assert json.loads(rejected["raw_row_json"]) == {
        "record_id": "103",
        "sample_code": "A1",
        "ip_address": "192.0.2.***",
    }


def test_auto_mask_and_explicit_policies_protect_loaded_and_quarantined_values(
    tmp_path: Path,
) -> None:
    source = NamedTableInput(
        name="contacts",
        headers=("record_id", "sample_code", "email", "phone_number", "test_note"),
        rows=(
            ("101", "A1", "alpha@example.test", "202-555-0140", "N/A"),
            ("102", "B2", "beta@example.test", "202-555-0141", "fixture-token-2"),
            ("103", "A1", "gamma@example.test", "202-555-0142", "fixture-token-7"),
        ),
    )
    overrides = SchemaOverrideSpec(
        columns=[
            ColumnOverride(table="contacts", source_column="sample_code", unique=True),
            ColumnOverride(table="contacts", source_column="email", masking=MaskingPolicy.HASH),
            ColumnOverride(table="contacts", source_column="test_note", masking=MaskingPolicy.MASK),
        ]
    )
    db_path = tmp_path / "contacts.sqlite"

    _, result = load_tables(
        extra_tables=[source], db_path=db_path, overrides=overrides, auto_mask_pii=True
    )

    assert (result.total_rows_loaded, result.total_rows_quarantined) == (2, 1)
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute(
            "SELECT email, phone_number, test_note FROM contacts ORDER BY record_id"
        ).fetchall() == [
            ("h_d8477df6aed7f868f6d2185b", "202-***-0140", None),
            ("h_c7d701d71ea4c58cdcd72b9a", "202-***-0141", "fi******-2"),
        ]
        ((payload,),) = db.execute("SELECT raw_row_json FROM _etl_quarantine_rows").fetchall()
    assert json.loads(payload) == {
        "record_id": "103",
        "sample_code": "A1",
        "email": "h_7130d11c7492ff84f831a907",
        "phone_number": "202-***-0142",
        "test_note": "fi******-7",
    }


@pytest.mark.parametrize(
    ("raw", "korean", "expected"),
    [
        (" 2024년 2월 29일 ", True, date(2024, 2, 29)),
        ("2024년 2월 30일", True, None),
        ("2024/02/29", True, date(2024, 2, 29)),
        ("20000", False, date(1954, 10, 3)),
        ("60000", False, date(2064, 4, 8)),
        ("19999", False, None),
        ("60001", False, None),
        ("45292", True, None),
        ("2024-02-30", False, None),
    ],
)
def test_date_parser_limits_serials_and_checks_calendar_dates(
    raw: str, korean: bool, expected: date | None
) -> None:
    assert _parse_temporal_date(raw, korean=korean) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-02-29T04:05:06Z", datetime(2024, 2, 29, 4, 5, 6, tzinfo=timezone.utc)),
        (
            "2024-02-29T04:05:06+09:00",
            datetime(2024, 2, 29, 4, 5, 6, tzinfo=timezone(timedelta(hours=9))),
        ),
        ("2024/02/29 4:05:06", datetime(2024, 2, 29, 4, 5, 6)),
        ("2024.02.29 04:05:06", datetime(2024, 2, 29, 4, 5, 6)),
        ("2024-02-30T04:05:06", None),
    ],
)
def test_timestamp_parser_preserves_local_time_and_offset(
    raw: str, expected: datetime | None
) -> None:
    actual = _parse_temporal_timestamp(raw)
    assert actual == expected
    if expected is not None:
        assert actual is not None and actual.isoformat() == expected.isoformat()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" 4:05:06 ", time(4, 5, 6)), ("23:59:59.999999", time(23, 59, 59, 999999)), ("24:00", None)],
)
def test_time_parser_rejects_hour_24_without_truncating_microseconds(
    raw: str, expected: time | None
) -> None:
    assert _parse_temporal_time(raw) == expected


@pytest.mark.parametrize(
    ("raw", "physical_type", "type_cast", "transform", "detail"),
    [
        ("12원34", PhysicalType.DECIMAL, "decimal(6, 2)", "strip_currency_and_comma", "형식"),
        ("₩3.50", PhysicalType.INTEGER, "int", "strip_currency_and_comma", "소수 존재"),
        ("invalid", PhysicalType.BOOLEAN, "boolean", "normalize_boolean", "불리언"),
        ("2024-02-30", PhysicalType.DATE, "date", "parse_iso_temporal", "날짜"),
        ("24:00", PhysicalType.TIME, "time", "parse_iso_temporal", "시간"),
        (
            "2024-02-30T14:00:00",
            PhysicalType.TIMESTAMP,
            "timestamp",
            "parse_iso_temporal",
            "타임스탬프",
        ),
        ("3.50", PhysicalType.INTEGER, "int", "identity", "정수"),
        ("none", PhysicalType.FLOAT, "double precision", "identity", "실수"),
        ("1e309", PhysicalType.FLOAT, "double precision", "identity", "유한하지 않은"),
        ("abcd", PhysicalType.STRING, "varchar(3)", "identity", "최대 길이"),
    ],
)
def test_cell_cast_rejects_malformed_or_out_of_range_values(
    raw: str, physical_type: PhysicalType, type_cast: str, transform: str, detail: str
) -> None:
    # The public loader infers types, so force a typed column to reach these cast errors.
    column = TargetColumn(
        name="value", source_columns=["value"], physical_type=physical_type, nullable=True
    )
    mapping = ColumnMapping(
        source_column="value",
        target_table="samples",
        target_column="value",
        type_cast=type_cast,
        transform=transform,
        null_handling=NullHandling.NULL,
    )

    ok, value, error = _transform_cell(
        raw,
        col=column,
        mapping=mapping,
        slot_rx=None,
        mask_policy=MaskingPolicy.NONE,
        sensitive_type=None,
        engine="sqlite",
    )

    assert (ok, value) == (False, None)
    assert detail in error
