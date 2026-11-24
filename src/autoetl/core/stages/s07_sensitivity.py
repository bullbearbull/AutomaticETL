"""S7 민감도 분류 — 결정론적 개인정보(PII)·민감 컬럼 탐지(SensitivityMap).

원본 셀 값은 절대 로그·예외·노트·계약 출력에 포함하지 않으며, 컬럼명 신호,
S3 의미 타입, 정규식/체크섬(주민등록번호·사업자등록번호·Luhn 카드번호) 기반의
비율·등급·마스킹 권고만 산출한다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from autoetl.contracts import (
    IngestManifest,
    PhysicalType,
    SemanticType,
    SensitiveType,
    SensitivityAssignment,
    SensitivityLevel,
    SensitivityMap,
    TableProfileBundle,
    TypeAssignment,
)
from autoetl.core.context import RAW_TABLE, AnalysisContext, quote_identifier

SAMPLE_LIMIT = 500
VALUE_MATCH_THRESHOLD = 0.60

_RRN_RE = re.compile(r"^\d{6}-?[1-4]\d{6}$")
_RRN_MASKED_RE = re.compile(r"^\d{6}-?[1-4]\*{6}$")
_FRN_RE = re.compile(r"^\d{6}-?[5-8]\d{6}$")
_BRN_RE = re.compile(r"^\d{3}-\d{2}-\d{5}$")
_CARD_SHAPE_RE = re.compile(r"^(?:\d{4}[- ]?){3}\d{1,7}$")
_PHONE_KR_RE = re.compile(r"^(?:01[016789]|02|0[3-6][1-5]|070)[- ]?\d{3,4}[- ]?\d{4}$")
_PHONE_INTL_RE = re.compile(r"^(?:\+\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]\d{3,4}[-.\s]\d{4}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$")
_BANK_ACCT_VALUE_RE = re.compile(r"^\d{2,4}-\d{2,6}-\d{2,7}(?:-\d{1,3})?$")

_ENTITY_NAME_EXCLUSIONS = (
    "branch",
    "product",
    "company",
    "store",
    "dept",
    "department",
    "category",
    "item",
    "bank",
    "org",
    "merchant",
    "vendor",
    "지점",
    "상품",
    "회사",
    "부서",
    "법인",
    "기관",
    "가맹점",
)

_PERSON_NAME_HINTS = (
    "성명",
    "고객명",
    "회원명",
    "사용자명",
    "직원명",
    "사원명",
    "대표자명",
    "예금주",
    "수취인",
    "이름",
    "full_name",
    "person_name",
    "customer_name",
    "user_name",
    "member_name",
    "employee_name",
    "first_name",
    "last_name",
    "contact_name",
)

_DOB_HINTS = (
    "생년월일",
    "출생일",
    "생일",
    "birth_date",
    "birthdate",
    "date_of_birth",
    "birth_dt",
    "dob",
)

_PHONE_HINTS = (
    "전화",
    "연락처",
    "휴대폰",
    "휴대전화",
    "핸드폰",
    "phone",
    "mobile",
    "tel_no",
    "telephone",
    "cell_phone",
)

_EMAIL_HINTS = ("이메일", "메일주소", "email", "e_mail")

_ADDRESS_HINTS = (
    "상세주소",
    "도로명주소",
    "지번주소",
    "거주지",
    "주소",
    "home_address",
    "street_address",
    "address",
    "addr",
)

_BANK_ACCOUNT_HINTS = (
    "계좌번호",
    "출금계좌",
    "입금계좌",
    "계좌",
    "bank_account",
    "account_no",
    "account_number",
    "acct_no",
    "iban",
)

_RRN_HINTS = ("주민등록번호", "주민번호", "rrn", "ssn", "resident_no")
_CARD_HINTS = ("카드번호", "card_no", "card_number", "credit_card")

_LEVEL_BY_TYPE: dict[SensitiveType, SensitivityLevel] = {
    SensitiveType.RESIDENT_REGISTRATION_NUMBER: SensitivityLevel.L3,
    SensitiveType.FOREIGNER_REGISTRATION_NUMBER: SensitivityLevel.L3,
    SensitiveType.CARD_NUMBER: SensitivityLevel.L3,
    SensitiveType.BANK_ACCOUNT: SensitivityLevel.L2,
    SensitiveType.PHONE_NUMBER: SensitivityLevel.L2,
    SensitiveType.EMAIL: SensitivityLevel.L2,
    SensitiveType.NAME: SensitivityLevel.L2,
    SensitiveType.DATE_OF_BIRTH: SensitivityLevel.L2,
    SensitiveType.ADDRESS: SensitivityLevel.L2,
    SensitiveType.BUSINESS_REGISTRATION_NUMBER: SensitivityLevel.L1,
    SensitiveType.IP_ADDRESS: SensitivityLevel.L1,
}

_LEVEL_RANK = {
    SensitivityLevel.L0: 0,
    SensitivityLevel.L1: 1,
    SensitivityLevel.L2: 2,
    SensitivityLevel.L3: 3,
}

_MASKING_POLICY: dict[SensitiveType, str] = {
    SensitiveType.RESIDENT_REGISTRATION_NUMBER: (
        "법정 고유식별정보(L3): 저장 암호화 필수, 조회 시 뒤 7자리 마스킹 및 별도 테이블 격리"
    ),
    SensitiveType.FOREIGNER_REGISTRATION_NUMBER: (
        "법정 고유식별정보(L3): 저장 암호화 필수, 조회 시 뒤 7자리 마스킹 및 접근 통제"
    ),
    SensitiveType.CARD_NUMBER: (
        "결제 민감정보(L3): 토큰화 또는 암호화 저장, 중간 8자리 마스킹(PCI-DSS)"
    ),
    SensitiveType.BANK_ACCOUNT: (
        "금융 식별정보(L2): 저장 암호화 및 중간 자릿수 마스킹(앞/뒤 일부만 노출)"
    ),
    SensitiveType.PHONE_NUMBER: "연락처(L2): 가운데 4자리 마스킹(예: 010-****-5678)",
    SensitiveType.EMAIL: "이메일(L2): 로컬부 일부 마스킹(예: ab***@domain) 또는 해시화",
    SensitiveType.NAME: "성명(L2): 가운데 글자 마스킹(예: 홍*동) 및 PII 권한 분리",
    SensitiveType.DATE_OF_BIRTH: "생년월일(L2): 분석 용도 시 출생연도·연령대 일반화 권고",
    SensitiveType.ADDRESS: "주소(L2): 상세주소 분리 및 분석 용도 시 시/군/구 단위 일반화",
    SensitiveType.BUSINESS_REGISTRATION_NUMBER: (
        "사업자 식별정보(L1): 외부 공유 시 마스킹 여부 정책 확인"
    ),
    SensitiveType.IP_ADDRESS: "접속 IP(L1): 마지막 옥텟 마스킹 또는 익명화 해시 적용",
}


def masking_recommendation(stype: SensitiveType) -> str:
    """민감 유형별 권장 마스킹·격리 정책 안내 문자열을 반환한다."""
    return _MASKING_POLICY.get(stype, "접근 통제 및 마스킹 정책 검토")


def _valid_rrn_checksum(raw: str) -> bool:
    digits = [int(ch) for ch in raw if ch.isdigit()]
    if len(digits) != 13:
        return False
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    total = sum(d * w for d, w in zip(digits[:12], weights, strict=True))
    check = (11 - (total % 11)) % 10
    return check == digits[12]


def _valid_brn_checksum(raw: str) -> bool:
    digits = [int(ch) for ch in raw if ch.isdigit()]
    if len(digits) != 10:
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(d * w for d, w in zip(digits[:9], weights, strict=True))
    total += (digits[8] * 5) // 10
    check = (10 - (total % 10)) % 10
    return check == digits[9]


def _valid_luhn(raw: str) -> bool:
    digits = [int(ch) for ch in raw if ch.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for idx, d in enumerate(digits):
        val = d
        if idx % 2 == parity:
            val *= 2
            if val > 9:
                val -= 9
        total += val
    return total % 10 == 0


def _contains_hint(col_name: str, hints: tuple[str, ...]) -> bool:
    low = col_name.strip().lower()
    compact = low.replace(" ", "_")
    return any(h in low or h in compact for h in hints)


def _is_person_name_column(col_name: str) -> bool:
    low = col_name.strip().lower()
    if any(ex in low for ex in _ENTITY_NAME_EXCLUSIONS):
        return False
    if low in {"name", "성명", "이름"}:
        return True
    return _contains_hint(low, _PERSON_NAME_HINTS)


def _fetch_column_sample(ctx: AnalysisContext, column_id: str) -> list[str]:
    qcol = quote_identifier(column_id)
    rows = ctx.connection.execute(
        f"SELECT {qcol} FROM {RAW_TABLE} "
        f"WHERE {qcol} IS NOT NULL AND trim(CAST({qcol} AS VARCHAR)) <> '' "
        f"LIMIT {SAMPLE_LIMIT}"
    ).fetchall()
    return [str(r[0]).strip() for r in rows if r[0] is not None]


def _classify_column(
    col_name: str,
    t_assign: TypeAssignment | None,
    samples: Sequence[str],
) -> tuple[SensitivityAssignment | None, list[str]]:
    """단일 컬럼의 민감 유형·등급·체크섬 결과를 판정한다(원본 값 비포함)."""
    detected: list[SensitiveType] = []
    detectors: list[str] = []
    notes: list[str] = []
    checksum_ok = False
    best_rate = 0.0
    best_conf = 0.0

    sem = t_assign.semantic_type if t_assign is not None else None
    phys = t_assign.physical_type if t_assign is not None else None
    n = len(samples)

    def _add(
        stype: SensitiveType,
        detector: str,
        conf: float,
        rate: float,
        *,
        validated: bool = False,
    ) -> None:
        nonlocal checksum_ok, best_rate, best_conf
        if stype not in detected:
            detected.append(stype)
        if detector not in detectors:
            detectors.append(detector)
        checksum_ok = checksum_ok or validated
        best_rate = max(best_rate, rate)
        best_conf = max(best_conf, conf)

    # 1. 값 패턴 + 체크섬 검증
    if n > 0:
        rrn_hits = [s for s in samples if _RRN_RE.fullmatch(s) or _RRN_MASKED_RE.fullmatch(s)]
        if len(rrn_hits) / n >= VALUE_MATCH_THRESHOLD:
            unmasked = [s for s in rrn_hits if "*" not in s]
            valid_chk = bool(unmasked) and (
                sum(1 for s in unmasked if _valid_rrn_checksum(s)) / len(unmasked) >= 0.5
            )
            _add(
                SensitiveType.RESIDENT_REGISTRATION_NUMBER,
                "pattern+checksum" if valid_chk else "pattern",
                0.98 if valid_chk else 0.90,
                len(rrn_hits) / n,
                validated=valid_chk,
            )

        frn_hits = sum(1 for s in samples if _FRN_RE.fullmatch(s))
        if frn_hits / n >= VALUE_MATCH_THRESHOLD:
            _add(
                SensitiveType.FOREIGNER_REGISTRATION_NUMBER,
                "pattern",
                0.92,
                frn_hits / n,
            )

        brn_hits = [s for s in samples if _BRN_RE.fullmatch(s)]
        if len(brn_hits) / n >= VALUE_MATCH_THRESHOLD:
            valid_brn = sum(1 for s in brn_hits if _valid_brn_checksum(s)) / len(brn_hits) >= 0.5
            _add(
                SensitiveType.BUSINESS_REGISTRATION_NUMBER,
                "pattern+checksum" if valid_brn else "pattern",
                0.95 if valid_brn else 0.85,
                len(brn_hits) / n,
                validated=valid_brn,
            )

        card_hits = [
            s
            for s in samples
            if _CARD_SHAPE_RE.fullmatch(s)
            and ("-" in s or " " in s or _contains_hint(col_name, _CARD_HINTS))
        ]
        if len(card_hits) / n >= VALUE_MATCH_THRESHOLD:
            luhn_valid = sum(1 for s in card_hits if _valid_luhn(s))
            if luhn_valid / len(card_hits) >= 0.5:
                _add(
                    SensitiveType.CARD_NUMBER,
                    "pattern+luhn",
                    0.97,
                    len(card_hits) / n,
                    validated=True,
                )
            elif _contains_hint(col_name, _CARD_HINTS):
                failed = len(card_hits) - luhn_valid
                notes.append(f"`{col_name}`: 카드번호 형식이나 Luhn 검증 실패 {failed}건 포함")
                _add(SensitiveType.CARD_NUMBER, "name_hint+pattern", 0.80, len(card_hits) / n)

        phone_hits = sum(
            1 for s in samples if _PHONE_KR_RE.fullmatch(s) or _PHONE_INTL_RE.fullmatch(s)
        )
        if phone_hits / n >= VALUE_MATCH_THRESHOLD:
            _add(SensitiveType.PHONE_NUMBER, "pattern", 0.95, phone_hits / n)

        email_hits = sum(1 for s in samples if _EMAIL_RE.fullmatch(s))
        if email_hits / n >= VALUE_MATCH_THRESHOLD:
            _add(SensitiveType.EMAIL, "pattern", 0.97, email_hits / n)

        ip_hits = sum(1 for s in samples if _IPV4_RE.fullmatch(s))
        if ip_hits / n >= VALUE_MATCH_THRESHOLD:
            _add(SensitiveType.IP_ADDRESS, "pattern", 0.92, ip_hits / n)

        if _contains_hint(col_name, _BANK_ACCOUNT_HINTS):
            acct_hits = sum(1 for s in samples if _BANK_ACCT_VALUE_RE.fullmatch(s))
            rate = max(
                acct_hits / n, 0.85 if _contains_hint(col_name, _BANK_ACCOUNT_HINTS) else 0.0
            )
            _add(SensitiveType.BANK_ACCOUNT, "name_hint+pattern", 0.90, min(1.0, rate))

    # 2. S3 의미 타입 연동
    if sem is SemanticType.RESIDENT_REGISTRATION_NUMBER:
        _add(
            SensitiveType.RESIDENT_REGISTRATION_NUMBER, "semantic_type", 0.96, max(best_rate, 0.95)
        )
    elif sem is SemanticType.BUSINESS_REGISTRATION_NUMBER:
        _add(
            SensitiveType.BUSINESS_REGISTRATION_NUMBER, "semantic_type", 0.92, max(best_rate, 0.95)
        )
    elif sem is SemanticType.PHONE:
        _add(SensitiveType.PHONE_NUMBER, "semantic_type", 0.95, max(best_rate, 0.95))
    elif sem is SemanticType.EMAIL:
        _add(SensitiveType.EMAIL, "semantic_type", 0.96, max(best_rate, 0.95))
    elif sem is SemanticType.ADDRESS:
        _add(SensitiveType.ADDRESS, "semantic_type", 0.88, max(best_rate, 0.85))

    # 3. 결정론적 컬럼명 힌트 (패턴만으로 잡기 어려운 성명·생년월일·주소·계좌 등)
    if _contains_hint(col_name, _RRN_HINTS):
        _add(SensitiveType.RESIDENT_REGISTRATION_NUMBER, "name_hint", 0.92, max(best_rate, 0.80))
    if _contains_hint(col_name, _CARD_HINTS):
        _add(SensitiveType.CARD_NUMBER, "name_hint", 0.88, max(best_rate, 0.80))
    if _contains_hint(col_name, _BANK_ACCOUNT_HINTS):
        _add(SensitiveType.BANK_ACCOUNT, "name_hint", 0.86, max(best_rate, 0.80))
    if _contains_hint(col_name, _PHONE_HINTS):
        _add(SensitiveType.PHONE_NUMBER, "name_hint", 0.88, max(best_rate, 0.80))
    if _contains_hint(col_name, _EMAIL_HINTS):
        _add(SensitiveType.EMAIL, "name_hint", 0.88, max(best_rate, 0.80))
    if _contains_hint(col_name, _DOB_HINTS):
        _add(SensitiveType.DATE_OF_BIRTH, "name_hint", 0.90, max(best_rate, 0.85))
    if _contains_hint(col_name, _ADDRESS_HINTS):
        _add(SensitiveType.ADDRESS, "name_hint", 0.85, max(best_rate, 0.80))
    if _is_person_name_column(col_name) and phys in {None, PhysicalType.STRING}:
        _add(SensitiveType.NAME, "name_hint", 0.86, max(best_rate, 0.80))

    if not detected:
        return None, notes

    top_level = max(
        (_LEVEL_BY_TYPE.get(st, SensitivityLevel.L1) for st in detected),
        key=lambda lv: _LEVEL_RANK[lv],
    )
    return (
        SensitivityAssignment(
            column=col_name,
            types=detected,
            level=top_level,
            detector="+".join(detectors),
            confidence=round(min(1.0, best_conf), 3),
            checksum_validated=checksum_ok,
            matched_rate=round(min(1.0, best_rate), 4),
        ),
        notes,
    )


def detect_table_sensitivity(
    bundle: TableProfileBundle,
    slot: AnalysisContext | None = None,
) -> SensitivityMap:
    """단일 테이블 번들(+선택 DuckDB 슬롯)에서 PII·민감 컬럼을 결정론적으로 탐지한다."""
    type_by_col = {t.column: t for t in bundle.types}
    col_id_by_name = (
        {c.original_name: c.column_id for c in slot.columns} if slot is not None else {}
    )
    assignments: list[SensitivityAssignment] = []
    notes: list[str] = []

    for col in bundle.ingest.columns:
        samples: list[str] = []
        if slot is not None:
            col_id = col_id_by_name.get(col.name)
            if col_id is not None:
                samples = _fetch_column_sample(slot, col_id)
        assignment, col_notes = _classify_column(col.name, type_by_col.get(col.name), samples)
        notes.extend(col_notes)
        if assignment is not None:
            assignments.append(assignment)

    return SensitivityMap(assignments=assignments, notes=notes)


def run(ctx: AnalysisContext) -> SensitivityMap:
    """단일 테이블 파이프라인 S7 단계 실행."""
    ingest_obj = ctx.stage_artifacts.get("s00_ingest")
    types_obj = ctx.stage_artifacts.get("s03_types")
    type_by_col: dict[str, TypeAssignment] = {}
    if isinstance(types_obj, list):
        for item in types_obj:
            if isinstance(item, TypeAssignment):
                type_by_col[item.column] = item

    if isinstance(ingest_obj, IngestManifest):
        col_names = [c.name for c in ingest_obj.columns]
    else:
        col_names = [c.original_name for c in ctx.columns]
    col_id_by_name = {c.original_name: c.column_id for c in ctx.columns}

    assignments: list[SensitivityAssignment] = []
    notes: list[str] = []
    for col_name in col_names:
        col_id = col_id_by_name.get(col_name)
        samples = _fetch_column_sample(ctx, col_id) if col_id is not None else []
        assignment, col_notes = _classify_column(col_name, type_by_col.get(col_name), samples)
        notes.extend(col_notes)
        if assignment is not None:
            assignments.append(assignment)

    return SensitivityMap(assignments=assignments, notes=notes)


__all__ = [
    "detect_table_sensitivity",
    "masking_recommendation",
    "run",
]
