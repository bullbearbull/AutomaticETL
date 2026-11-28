"""S7 pattern detection and security level decisions with synthetic data."""

import pytest

from autoetl.contracts import (
    PhysicalType,
    SemanticType,
    SensitiveType,
    SensitivityLevel,
    SensitivityMap,
    TableProfileBundle,
    TypeAssignment,
)
from autoetl.core.multi_context import MultiTableContext, NamedTableInput
from autoetl.core.stages import s00_ingest, s01_sampling, s07_sensitivity
from autoetl.security.levels import (
    LogLevel,
    SecureMode,
    TrustZone,
    allows_metadata,
    allows_raw_value,
    is_loopback_address,
    is_private_address,
    is_registrable_internal,
    log_level_for_value,
    prompt_log_level,
    trust_zone,
)


def _detect(columns: dict[str, list[str]]) -> SensitivityMap:
    table = NamedTableInput(
        name="synthetic",
        headers=tuple(columns),
        rows=tuple(zip(*columns.values(), strict=True)),
    )
    with MultiTableContext(extra_tables=[table], threads=1) as ctx:
        return s07_sensitivity.run(ctx.activate("t0"))


def test_blank_cells_do_not_use_the_pattern_sample_budget() -> None:
    result = _detect(
        {
            "mixed_values": ["\t"] * 500
            + [
                "\tcanary-one@example.invalid ",
                "canary-two@example.invalid",
                "canary-three@example.invalid",
                "no match",
                "another miss",
                "",
                " \t\n\u2003 ",
                "\u0085",
            ],
        }
    )

    assert [assignment.column for assignment in result.assignments] == ["mixed_values"]
    (email,) = result.assignments
    assert email.types == [SensitiveType.EMAIL]
    assert email.level is SensitivityLevel.L2
    assert email.matched_rate == 0.60


def test_patterns_classify_contacts_and_ignore_below_threshold_columns() -> None:
    email = "synthetic-contact@example.invalid"
    result = _detect(
        {
            "local_contact": ["010-0000-0000"] * 5,
            "other_contact": ["+1 202-555-0100"] * 5,
            "web_client": ["192.0.2.26"] * 5,
            "invalid_octets": ["192.0.2.256"] * 5,
            "contact_values": [email, email, email, "no match", "missing"],
            "mixed_values": [email, email, "oak", "elm", "fir"],
        }
    )

    detected = [
        (item.column, item.types, item.level, item.matched_rate) for item in result.assignments
    ]
    assert detected == [
        ("local_contact", [SensitiveType.PHONE_NUMBER], SensitivityLevel.L2, 1.0),
        ("other_contact", [SensitiveType.PHONE_NUMBER], SensitivityLevel.L2, 1.0),
        ("web_client", [SensitiveType.IP_ADDRESS], SensitivityLevel.L1, 1.0),
        ("contact_values", [SensitiveType.EMAIL], SensitivityLevel.L2, 0.60),
    ]
    assert all(item.detector == "pattern" for item in result.assignments)


def _fake_rrns() -> tuple[str, str]:
    # An impossible date guarantees that even the checksum-valid value is synthetic.
    prefix = "000000112345"
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    total = sum(int(digit) * weight for digit, weight in zip(prefix, weights, strict=True))
    check = (11 - total % 11) % 10
    return f"{prefix[:6]}-{prefix[6:]}{check}", f"{prefix}{(check + 1) % 10}"


def _fake_brns() -> tuple[str, str]:
    # Keep the registration office and registration category at all zeroes.
    prefix = "000001237"
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(int(digit) * weight for digit, weight in zip(prefix, weights, strict=True))
    check = -(total + int(prefix[-1]) * 5 // 10) % 10
    formatted = f"{prefix[:3]}-{prefix[3:5]}-{prefix[5:]}"
    return f"{formatted}{check}", f"{formatted}{(check + 1) % 10}"


def test_resident_and_business_patterns_report_checksum_results() -> None:
    valid_rrn, bad_rrn = _fake_rrns()
    valid_brn, bad_brn = _fake_brns()
    masked_rrn = "000000-1******"
    result = _detect(
        {
            "resident_values": [valid_rrn, bad_rrn, masked_rrn, "unknown", "pending"],
            "invalid_resident": [bad_rrn] * 5,
            "masked_resident": [masked_rrn] * 5,
            "foreign_values": ["000000-5000000"] * 5,
            "business_values": [valid_brn, bad_brn, "pending", "", ""],
            "invalid_business": [bad_brn] * 5,
        }
    )

    by_column = {item.column: item for item in result.assignments}
    assert set(by_column) == {
        "resident_values",
        "invalid_resident",
        "masked_resident",
        "foreign_values",
        "business_values",
        "invalid_business",
    }
    for column in ("resident_values", "invalid_resident", "masked_resident"):
        item = by_column[column]
        assert item.types == [SensitiveType.RESIDENT_REGISTRATION_NUMBER]
        assert item.level is SensitivityLevel.L3
    assert by_column["foreign_values"].types == [SensitiveType.FOREIGNER_REGISTRATION_NUMBER]
    assert by_column["foreign_values"].level is SensitivityLevel.L3
    for column in ("business_values", "invalid_business"):
        item = by_column[column]
        assert item.types == [SensitiveType.BUSINESS_REGISTRATION_NUMBER]
        assert item.level is SensitivityLevel.L1
    assert {item.column for item in result.assignments if item.checksum_validated} == {
        "resident_values",
        "business_values",
    }
    assert by_column["resident_values"].matched_rate == 0.60
    assert by_column["business_values"].matched_rate == 0.6667
    assert not result.notes


def test_card_luhn_uses_evidence_and_keeps_raw_samples_out_of_outputs(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dashed = "4111-1111-1111-1111"  # Public payment processor test number.
    compact = dashed.replace("-", "")
    spaced = dashed.replace("-", " ")
    # This variation would pass if doubling 5 skipped the required subtraction of 9.
    bad_check = f"{dashed[:2]}5{dashed[3:-1]}3"
    result = _detect(
        {
            "payment_values": [dashed, bad_check],
            "other_payment_values": [spaced, spaced],
            "card_number_email": [compact, compact],
            "card_no": [bad_check, bad_check],
            "unlabeled_values": [bad_check, bad_check],
        }
    )

    by_column = {item.column: item for item in result.assignments}
    assert set(by_column) == {
        "payment_values",
        "other_payment_values",
        "card_number_email",
        "card_no",
    }
    assert by_column["payment_values"].detector == "pattern+luhn"
    assert by_column["other_payment_values"].detector == "pattern+luhn"
    assert by_column["card_number_email"].types == [SensitiveType.CARD_NUMBER, SensitiveType.EMAIL]
    assert by_column["card_number_email"].detector == "pattern+luhn+name_hint"
    assert by_column["card_no"].types == [SensitiveType.CARD_NUMBER]
    assert by_column["card_no"].detector.startswith("name_hint+pattern")
    assert all(item.level is SensitivityLevel.L3 for item in result.assignments)
    assert {item.column for item in result.assignments if item.checksum_validated} == {
        "payment_values",
        "other_payment_values",
        "card_number_email",
    }
    (failure_note,) = result.notes
    assert all(part in failure_note for part in ("card_no", "Luhn", "2건"))
    captured = capsys.readouterr()
    for output in (result.model_dump_json(), caplog.text, captured.out, captured.err):
        assert all(value not in output for value in (dashed, compact, spaced, bad_check))
    assert "PCI-DSS" in s07_sensitivity.masking_recommendation(SensitiveType.CARD_NUMBER)


def test_name_hints_distinguish_people_addresses_and_accounts() -> None:
    result = _detect(
        {
            "Name": ["synthetic person"],
            "Customer Name": ["synthetic person"],
            "company_customer_name": ["synthetic entity"],
            "birth date": [""],
            "Email Address": [""],
            "home address": [""],
            "ip_address": [""],
            "clientIPv4Address": [""],
            "residentNo2": [""],
            "contact phone": [""],
            "bank account": ["00-00-00"],
            "account_number": ["unspecified"],
        }
    )

    assert [(item.column, item.types, item.matched_rate) for item in result.assignments] == [
        ("Name", [SensitiveType.NAME], 0.80),
        ("Customer Name", [SensitiveType.NAME], 0.80),
        ("birth date", [SensitiveType.DATE_OF_BIRTH], 0.85),
        ("Email Address", [SensitiveType.EMAIL], 0.80),
        ("home address", [SensitiveType.ADDRESS], 0.80),
        ("residentNo2", [SensitiveType.RESIDENT_REGISTRATION_NUMBER], 0.80),
        ("contact phone", [SensitiveType.PHONE_NUMBER], 0.80),
        ("bank account", [SensitiveType.BANK_ACCOUNT], 1.0),
        ("account_number", [SensitiveType.BANK_ACCOUNT], 0.85),
    ]


def test_s7_uses_manifest_positions_and_upstream_semantic_types() -> None:
    type_cases = (
        (
            "code",
            SemanticType.RESIDENT_REGISTRATION_NUMBER,
            SensitiveType.RESIDENT_REGISTRATION_NUMBER,
        ),
        (
            "code#2",
            SemanticType.BUSINESS_REGISTRATION_NUMBER,
            SensitiveType.BUSINESS_REGISTRATION_NUMBER,
        ),
        ("other", SemanticType.PHONE, SensitiveType.PHONE_NUMBER),
        ("lookup", SemanticType.EMAIL, SensitiveType.EMAIL),
        ("place", SemanticType.ADDRESS, SensitiveType.ADDRESS),
    )
    table = NamedTableInput(
        name="synthetic",
        headers=("code", "code", "other", "lookup", "place", "name", "contact values"),
        rows=(
            ("pending", "pending", "pending", "pending", "pending", "1", "canary@example.invalid"),
        ),
    )
    types = [
        TypeAssignment(
            column=column,
            physical_type=PhysicalType.STRING,
            semantic_type=semantic_type,
            detector="synthetic",
            confidence=0.99,
        )
        for column, semantic_type, _ in type_cases
    ]
    types.append(
        TypeAssignment(
            column="name",
            physical_type=PhysicalType.INTEGER,
            semantic_type=SemanticType.FREE_TEXT,
            detector="synthetic",
            confidence=0.99,
        )
    )
    with MultiTableContext(extra_tables=[table], threads=1) as ctx:
        slot = ctx.activate("t0")
        ingest = s00_ingest.run(slot, slot.spec)
        slot.stage_artifacts["s00_ingest"] = ingest
        slot.stage_artifacts["s03_types"] = [*types, {"column": "name"}]
        bundle = TableProfileBundle(
            source=slot.table_info,
            ingest=ingest,
            sampling=s01_sampling.run(slot),
            types=types,
        )
        result = s07_sensitivity.run(slot)
        without_slot = s07_sensitivity.detect_table_sensitivity(bundle)
        assert s07_sensitivity.detect_table_sensitivity(bundle, slot) == result

    assert [(item.column, item.types, item.detector) for item in without_slot.assignments] == [
        (column, [sensitive_type], "semantic_type") for column, _, sensitive_type in type_cases
    ]
    assert result.assignments[:-1] == without_slot.assignments
    assert result.assignments[-1].column == "contact values"
    assert result.assignments[-1].types == [SensitiveType.EMAIL]
    assert result.assignments[-1].detector == "pattern"


@pytest.mark.parametrize(
    ("level", "raw_log_level"),
    [
        (SensitivityLevel.L0, LogLevel.CONFIDENTIAL),
        (SensitivityLevel.L1, LogLevel.CONFIDENTIAL),
        (SensitivityLevel.L2, LogLevel.RESTRICTED),
        (SensitivityLevel.L3, LogLevel.RESTRICTED),
    ],
)
def test_value_log_level_requires_masking_for_sensitive_data(
    level: SensitivityLevel, raw_log_level: LogLevel
) -> None:
    assert log_level_for_value(level, masked=False) is raw_log_level
    assert log_level_for_value(level, masked=True) is LogLevel.CONFIDENTIAL


@pytest.mark.parametrize(
    ("raw_levels", "masked", "secret", "floor", "expected"),
    [
        ((), False, False, None, LogLevel.INTERNAL),
        ((), True, False, None, LogLevel.CONFIDENTIAL),
        ((SensitivityLevel.L0, SensitivityLevel.L1), False, False, None, LogLevel.CONFIDENTIAL),
        ((SensitivityLevel.L0, SensitivityLevel.L2), True, False, None, LogLevel.RESTRICTED),
        ((SensitivityLevel.L3, SensitivityLevel.L1), False, False, None, LogLevel.RESTRICTED),
        ((SensitivityLevel.L0,), True, True, None, LogLevel.RESTRICTED),
        ((), False, True, None, LogLevel.RESTRICTED),
        ((), False, False, LogLevel.CONFIDENTIAL, LogLevel.CONFIDENTIAL),
        ((SensitivityLevel.L0,), False, False, LogLevel.RESTRICTED, LogLevel.RESTRICTED),
        ((SensitivityLevel.L3,), False, False, LogLevel.INTERNAL, LogLevel.RESTRICTED),
    ],
)
def test_prompt_log_level_keeps_the_highest_content_or_request_level(
    raw_levels: tuple[SensitivityLevel, ...],
    masked: bool,
    secret: bool,
    floor: LogLevel | None,
    expected: LogLevel,
) -> None:
    assert (
        prompt_log_level(
            raw_levels, masked_value_present=masked, secret_value_present=secret, at_least=floor
        )
        is expected
    )


@pytest.mark.parametrize(
    ("host", "registered", "zone", "private"),
    [
        (" 127.0.0.2 ", (), TrustZone.LOOPBACK, False),
        ("::1", (), TrustZone.LOOPBACK, False),
        ("::ffff:127.0.0.1", (), TrustZone.LOOPBACK, False),
        ("::ffff:192.168.0.8", ("invalid", "192.168.0.8"), TrustZone.REGISTERED_INTERNAL, True),
        ("fd00::1", ("fd00::1",), TrustZone.REGISTERED_INTERNAL, True),
        ("10.1.1.2", ("10.1.1.3",), TrustZone.EXTERNAL, True),
        ("169.254.0.1", ("169.254.0.1",), TrustZone.EXTERNAL, False),
        ("192.0.2.26", ("192.0.2.26",), TrustZone.EXTERNAL, False),
        ("synthetic.example.invalid", ("synthetic.example.invalid",), TrustZone.EXTERNAL, False),
    ],
)
def test_only_loopback_and_registered_private_addresses_are_trusted(
    host: str,
    registered: tuple[str, ...],
    zone: TrustZone,
    private: bool,
) -> None:
    assert trust_zone(host, registered_internal=registered) is zone
    assert is_loopback_address(host) is (zone is TrustZone.LOOPBACK)
    assert is_private_address(host) is private
    assert is_registrable_internal(host) is private


@pytest.mark.parametrize("mode", list(SecureMode))
def test_transmission_policy_preserves_zone_boundaries(mode: SecureMode) -> None:
    levels = (SensitivityLevel.L0, SensitivityLevel.L1, SensitivityLevel.L2, SensitivityLevel.L3)
    assert [allows_raw_value(TrustZone.LOOPBACK, level, mode) for level in levels] == [True] * 4
    assert [allows_raw_value(TrustZone.REGISTERED_INTERNAL, level, mode) for level in levels] == [
        True,
        True,
        True,
        False,
    ]
    assert [allows_raw_value(TrustZone.EXTERNAL, level, mode) for level in levels] == [
        mode is SecureMode.OFF,
        mode is SecureMode.OFF,
        False,
        False,
    ]
    assert allows_metadata(TrustZone.LOOPBACK, mode)
    assert allows_metadata(TrustZone.REGISTERED_INTERNAL, mode)
    assert allows_metadata(TrustZone.EXTERNAL, mode) is (mode is not SecureMode.STRICT)
