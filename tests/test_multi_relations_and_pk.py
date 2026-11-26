"""Milestone E3: 다중 테이블 내부 관계·테이블 간 FK(문자열 슬롯 FK 포함)·최적 PK 선정 테스트."""

from __future__ import annotations

import pytest

from autoetl.contracts import ColumnRoleCategory, RelationshipCardinality
from autoetl.core.multi_context import MultiTableContext, NamedTableInput, profile_all_tables
from autoetl.core.pk_selector import singularize
from autoetl.core.relations_multi import analyze_multi_table_relations


def _build_ecommerce_tables() -> list[NamedTableInput]:
    """연관 6개 테이블(branches, customers, products, orders, order_items, audit_logs) 픽스처."""
    branches = NamedTableInput(
        name="branches",
        headers=("branch_code", "branch_name", "city"),
        rows=(
            ("KR", "서울본점", "서울"),
            ("US", "뉴욕지사", "뉴욕"),
            ("JP", "도쿄지사", "도쿄"),
        ),
    )
    customers = NamedTableInput(
        name="customers",
        headers=("customer_id", "customer_name", "grade", "city", "district"),
        rows=(
            ("101", "김민수", "VIP", "서울", "강남구"),
            ("102", "이서연", "골드", "서울", "서초구"),
            ("103", "박지훈", "일반", "부산", "해운대구"),
            ("104", "최유진", "골드", "서울", "강남구"),
        ),
    )
    products = NamedTableInput(
        name="products",
        headers=("product_id", "product_name", "category"),
        rows=(
            ("501", "노트북 프로", "전자"),
            ("502", "기계식 키보드", "전자"),
            ("503", "스탠딩 데스크", "가구"),
        ),
    )
    # orders: customer_id → customers.customer_id,
    #         order_code("ORD-2026-KR-0001")의 가변 슬롯 → branches.branch_code (숨은 FK!)
    #         qty(1~3)는 우연 정수 충돌 오탐이 나지 않아야 함
    orders = NamedTableInput(
        name="orders",
        headers=("order_id", "order_code", "customer_id", "order_date"),
        rows=(
            ("1001", "ORD-2025-KR-0001", "101", "2025-11-01"),
            ("1002", "ORD-2025-US-0002", "102", "2025-11-02"),
            ("1003", "ORD-2026-JP-0003", "103", "2026-01-10"),
            ("1004", "ORD-2026-KR-0004", "101", "2026-02-15"),
            ("1005", "ORD-2026-US-0005", "104", "2026-03-01"),
        ),
    )
    # order_items: 복합 PK (order_id, line_no), FK → orders & products,
    # 파생 total_amount = unit_price * qty
    order_items = NamedTableInput(
        name="order_items",
        headers=("order_id", "line_no", "product_id", "unit_price", "qty", "total_amount"),
        rows=(
            ("1001", "1", "501", "1500000", "1", "1500000"),
            ("1001", "2", "502", "120000", "2", "240000"),
            ("1002", "1", "503", "450000", "1", "450000"),
            ("1003", "1", "501", "1500000", "2", "3000000"),
            ("1004", "1", "502", "120000", "3", "360000"),
            ("1005", "1", "503", "450000", "2", "900000"),
        ),
    )
    # audit_logs: 중복 행 존재 → 자연 PK 없음 → 서로게이트 키 권고
    audit_logs = NamedTableInput(
        name="audit_logs",
        headers=("event_type", "actor"),
        rows=(
            ("LOGIN", "kim"),
            ("LOGIN", "kim"),
            ("EXPORT", "lee"),
        ),
    )
    return [branches, customers, products, orders, order_items, audit_logs]


def test_multi_table_fk_slot_fk_derivations_and_pk_selection() -> None:
    """연관 6개 테이블에서 직접 FK·문자열 슬롯 FK·복합 PK·서로게이트 PK·파생 칼럼을 검증한다."""
    tables = _build_ecommerce_tables()
    with MultiTableContext(extra_tables=tables) as mctx:
        bundles = profile_all_tables(mctx)
        analysis = analyze_multi_table_relations(mctx, bundles)

    # 1. 테이블별 최적 PK 선정 검증
    assert analysis.primary_keys["branches"].columns == ["branch_code"]
    assert analysis.primary_keys["customers"].columns == ["customer_id"]
    assert analysis.primary_keys["products"].columns == ["product_id"]
    assert analysis.primary_keys["orders"].columns == ["order_id"]

    # order_items는 단일 유일 컬럼이 없어 복합 PK (order_id, line_no) 선정
    oi_pk = analysis.primary_keys["order_items"]
    assert oi_pk.is_composite is True
    assert set(oi_pk.columns) == {"order_id", "line_no"}

    # audit_logs는 중복 행으로 후보 키 부재 → 서로게이트 키 권고
    log_pk = analysis.primary_keys["audit_logs"]
    assert log_pk.recommend_surrogate is True
    assert log_pk.surrogate_column_name == "audit_log_id"

    # 2. 다중 테이블 간 FK 탐지 검증
    fk_set = {
        (
            fk.child_table,
            tuple(fk.child_columns),
            fk.child_slot_name,
            fk.parent_table,
            tuple(fk.parent_columns),
        )
        for fk in analysis.foreign_keys
    }
    assert ("orders", ("customer_id",), None, "customers", ("customer_id",)) in fk_set
    assert ("order_items", ("order_id",), None, "orders", ("order_id",)) in fk_set
    assert ("order_items", ("product_id",), None, "products", ("product_id",)) in fk_set

    # 3. 문자열 분해 슬롯에서 숨은 FK 발견 검증: orders.order_code[slot] → branches.branch_code
    slot_fks = [
        fk
        for fk in analysis.foreign_keys
        if fk.child_table == "orders"
        and fk.child_columns == ["order_code"]
        and fk.parent_table == "branches"
        and fk.parent_columns == ["branch_code"]
    ]
    assert len(slot_fks) == 1
    assert slot_fks[0].child_slot_name is not None
    assert slot_fks[0].child_extract_sql is not None
    assert slot_fks[0].inclusion_rate == 1.0
    assert slot_fks[0].cardinality is RelationshipCardinality.MANY_TO_ONE

    # 4. 오탐 차단: order_items.qty 또는 line_no가 아무 관련 없는 부모 PK를 참조하지 않아야 함
    assert not any(
        fk.child_table == "order_items" and fk.child_columns == ["qty"]
        for fk in analysis.foreign_keys
    )

    # 5. 내부 파생 칼럼(total_amount = unit_price * qty) 및 계층(city -> district) 탐지 검증
    oi_derivs = analysis.table_relations["order_items"].derivations
    assert any(
        d.target == "total_amount" and set(d.sources) == {"unit_price", "qty"} for d in oi_derivs
    )

    cust_hiers = analysis.table_relations["customers"].hierarchies
    assert any(h.path == ["city", "district"] for h in cust_hiers)

    # 6. 타 테이블 참조 횟수가 PK 추천 점수에 반영됨
    assert analysis.primary_keys["customers"].referenced_by_count >= 1
    assert analysis.primary_keys["orders"].referenced_by_count >= 1
    assert analysis.primary_keys["branches"].referenced_by_count >= 1


def test_one_to_one_cycle_deduplication_and_orphan_rows() -> None:
    """1:1 대칭 FK의 사이클 제거와 95% 이상 포함 시 고아 행 카운트를 검증한다."""
    employees = NamedTableInput(
        name="employees",
        headers=("emp_id", "emp_name"),
        rows=tuple((str(i), f"직원{i}") for i in range(1, 21)),
    )
    employee_profiles = NamedTableInput(
        name="employee_profiles",
        headers=("emp_id", "bio"),
        rows=tuple((str(i), f"소개문구 {i}") for i in range(1, 21)),
    )
    # 20건 중 19건 일치(95.0%), 1건 고아 행(emp_id=999)
    employee_logs = NamedTableInput(
        name="employee_logs",
        headers=("log_id", "emp_id", "action"),
        rows=tuple(
            (str(i), str(i if i <= 19 else 999), "LOGIN" if i % 2 else "LOGOUT")
            for i in range(1, 21)
        ),
    )

    with MultiTableContext(extra_tables=[employees, employee_profiles, employee_logs]) as mctx:
        bundles = profile_all_tables(mctx)
        analysis = analyze_multi_table_relations(mctx, bundles)

    # employees ↔ employee_profiles 1:1 관계는 단방향 1건으로만 정리되어야 함
    one_to_one_between = [
        fk
        for fk in analysis.foreign_keys
        if {fk.child_table, fk.parent_table} == {"employees", "employee_profiles"}
    ]
    assert len(one_to_one_between) == 1
    assert one_to_one_between[0].cardinality is RelationshipCardinality.ONE_TO_ONE

    # employee_logs → employees (또는 employee_profiles) FK에서 orphan_rows == 1
    log_fks = [fk for fk in analysis.foreign_keys if fk.child_table == "employee_logs"]
    assert len(log_fks) == 1
    assert log_fks[0].orphan_rows == 1
    assert abs(log_fks[0].inclusion_rate - 0.95) < 1e-6


def test_foreign_keys_on_different_columns_survive_opposite_directions() -> None:
    departments = NamedTableInput(
        name="departments",
        headers=("dept_id", "manager_id"),
        rows=(("DEP101", "EMP001"), ("DEP102", "EMP003"), ("DEP103", "EMP005")),
    )
    employees = NamedTableInput(
        name="employees",
        headers=("emp_id", "dept_id"),
        rows=tuple((f"EMP{i:03d}", f"DEP{101 + (i - 1) // 2}") for i in range(1, 7)),
    )

    with MultiTableContext(extra_tables=[departments, employees]) as mctx:
        analysis = analyze_multi_table_relations(mctx, profile_all_tables(mctx))

    fks = {
        (
            fk.child_table,
            tuple(fk.child_columns),
            fk.parent_table,
            tuple(fk.parent_columns),
        ): fk.cardinality
        for fk in analysis.foreign_keys
    }
    assert fks[("employees", ("dept_id",), "departments", ("dept_id",))] is (
        RelationshipCardinality.MANY_TO_ONE
    )
    assert fks[("departments", ("manager_id",), "employees", ("emp_id",))] is (
        RelationshipCardinality.ONE_TO_ONE
    )


@pytest.mark.parametrize(
    ("child_column", "parent_name", "parent_column", "expected_orphans"),
    [
        ("region_code", "product_codes", "product_code", []),
        ("store_no", "item_no", "item_no", []),
        ("code_id", "codes", "id", [1]),
        ("code_code_id", "codes", "id", []),
        ("num_code_id", "code_nums", "id", []),
        ("code_num_id", "code_nums", "id", [1]),
        ("branch_id", "branches", "id", [1]),
        ("address_id", "addresses", "id", [1]),
        ("status_id", "statuses", "id", [1]),
        ("alias_id", "aliases", "id", [1]),
        ("quiz_id", "quizzes", "id", [1]),
    ],
)
def test_partial_fk_requires_domain_or_explicit_table_match(
    child_column: str, parent_name: str, parent_column: str, expected_orphans: list[int]
) -> None:
    parent = NamedTableInput(
        name=parent_name,
        headers=(parent_column,),
        rows=tuple((str(i),) for i in range(100, 120)),
    )
    child = NamedTableInput(
        name="locations",
        headers=(child_column,),
        rows=tuple((str(i),) for i in [*range(100, 119), 999]),
    )

    with MultiTableContext(extra_tables=[parent, child]) as mctx:
        analysis = analyze_multi_table_relations(mctx, profile_all_tables(mctx))

    assert [
        fk.orphan_rows
        for fk in analysis.foreign_keys
        if fk.child_table == "locations" and fk.parent_table == parent_name
    ] == expected_orphans


def test_composite_key_dependency_uses_equivalence_class_representatives() -> None:
    table = NamedTableInput(
        name="states",
        headers=("m", "r", "a", "x"),
        rows=(
            ("red", "10", "north", "open"),
            ("blue", "20", "north", "closed"),
            ("red", "10", "east", "closed"),
            ("blue", "20", "east", "open"),
            ("red", "10", "west", "open"),
            ("blue", "20", "west", "open"),
        ),
    )

    with MultiTableContext(extra_tables=[table]) as mctx:
        analysis = analyze_multi_table_relations(mctx, profile_all_tables(mctx))

    relations = analysis.table_relations["states"]
    assert {tuple(key.columns) for key in relations.candidate_keys} == {("a", "m"), ("a", "r")}
    assert any(fd.lhs == ["a", "r"] and fd.rhs == "x" for fd in relations.functional_dependencies)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("addresses", "address_id"),
        ("boxes", "box_id"),
        ("branches", "branch_id"),
        ("movies", "movie_id"),
        ("countries", "country_id"),
        ("release_movies", "release_movie_id"),
        ("statuses", "status_id"),
        ("order_statuses", "order_status_id"),
        ("order_status", "order_status_id"),
        ("aliases", "alias_id"),
        ("email_aliases", "email_alias_id"),
        ("email_alias", "email_alias_id"),
        ("result_caches", "result_cache_id"),
        ("loan_tranches", "loan_tranche_id"),
        ("cases", "case_id"),
        ("headaches", "headache_id"),
        ("backaches", "backache_id"),
        ("avalanches", "avalanche_id"),
        ("quizzes", "quiz_id"),
        ("user_quizzes", "user_quiz_id"),
        ("beaches", "beach_id"),
        ("coaches", "coach_id"),
        ("buzzes", "buzz_id"),
    ],
)
def test_surrogate_key_name_uses_singular_table_name(name: str, expected: str) -> None:
    table = NamedTableInput(name=name, headers=("color",), rows=(("red",), ("red",)))
    with MultiTableContext(extra_tables=[table]) as mctx:
        analysis = analyze_multi_table_relations(mctx, profile_all_tables(mctx))

    recommendation = analysis.primary_keys[name]
    assert recommendation.recommend_surrogate
    assert recommendation.surrogate_column_name == expected


def test_singular_table_name_breaks_tie_between_natural_keys() -> None:
    table = NamedTableInput(
        name="addresses",
        headers=("legacy_id", "address_id"),
        rows=tuple((str(1000 + i), str(2000 + i)) for i in range(1, 8)),
    )
    with MultiTableContext(extra_tables=[table]) as mctx:
        analysis = analyze_multi_table_relations(mctx, profile_all_tables(mctx))

    assert analysis.primary_keys["addresses"].columns == ["address_id"]


def test_camelcase_associative_composite_pk_selection() -> None:
    """camelCase 연관 테이블 복합 PK(orderID, productID)와 다수 반복 문자열 FK를 검증한다."""
    customers = NamedTableInput(
        name="customers",
        headers=("customerID", "companyName"),
        rows=tuple((f"CUS{i:02d}", f"Company {i}") for i in range(1, 26)),
    )
    orders = NamedTableInput(
        name="orders",
        headers=("orderID", "customerID"),
        rows=tuple((str(10248 + i), f"CUS{(i % 25) + 1:02d}") for i in range(60)),
    )
    products = NamedTableInput(
        name="products",
        headers=("productID", "productName"),
        rows=tuple((str(i), f"Product {i}") for i in range(1, 6)),
    )
    order_details = NamedTableInput(
        name="order_details",
        headers=("orderID", "productID", "unitPrice", "quantity", "discount"),
        rows=(
            ("10248", "1", "14.0", "12", "0"),
            ("10248", "2", "9.8", "10", "0"),
            ("10249", "2", "18.6", "9", "0"),
            ("10249", "3", "42.4", "40", "0"),
            ("10250", "1", "14.0", "10", "0.15"),
            ("10250", "4", "16.8", "35", "0.15"),
            ("10251", "3", "42.4", "6", "0.05"),
            ("10251", "5", "21.0", "15", "0.05"),
            ("10252", "4", "16.8", "20", "0"),
            ("10253", "5", "21.0", "25", "0"),
        ),
    )

    with MultiTableContext(extra_tables=[customers, orders, products, order_details]) as mctx:
        bundles = profile_all_tables(mctx)
        analysis = analyze_multi_table_relations(mctx, bundles)

    od_pk = analysis.primary_keys["order_details"]
    assert od_pk.recommend_surrogate is False
    assert od_pk.columns == ["orderID", "productID"]
    assert od_pk.is_composite is True
    assert analysis.primary_keys["orders"].columns == ["orderID"]
    assert analysis.primary_keys["products"].columns == ["productID"]
    assert any(
        fk.child_table == "orders"
        and fk.child_columns == ["customerID"]
        and fk.parent_table == "customers"
        and fk.parent_columns == ["customerID"]
        and fk.inclusion_rate == 1.0
        for fk in analysis.foreign_keys
    )


def test_year_prefixed_sequential_ids_across_unrelated_tables_do_not_form_fk() -> None:
    orders = NamedTableInput(
        name="orders",
        headers=("order_id", "amount"),
        rows=tuple((f"2026{i:06d}", str(i * 100)) for i in range(1, 20)),
    )
    invoices = NamedTableInput(
        name="invoices",
        headers=("invoice_id", "tax"),
        rows=tuple((f"2026{i:06d}", str(i * 10)) for i in range(1, 20)),
    )

    with MultiTableContext(extra_tables=[orders, invoices]) as mctx:
        bundles = profile_all_tables(mctx)
        analysis = analyze_multi_table_relations(mctx, bundles)

    for bundle in bundles:
        pk_cls = bundle.classifications[0]
        assert pk_cls.category is ColumnRoleCategory.STRUCTURED_STRING
        assert pk_cls.string_pattern is not None
        assert pk_cls.string_pattern.template == "{year}{seq}"
    assert analysis.foreign_keys == []


@pytest.mark.parametrize(
    ("raw_name", "expected_singular"),
    [
        ("branches", "branch"),
        ("addresses", "address"),
        ("statuses", "status"),
        ("orderStatuses", "order_status"),
        ("emailAliases", "email_alias"),
        ("quizzes", "quiz"),
        ("userQuizzes", "user_quiz"),
        ("headaches", "headache"),
        ("beaches", "beach"),
    ],
)
def test_singularize_handles_irregular_suffix_and_camelcase_forms(
    raw_name: str, expected_singular: str
) -> None:
    assert singularize(raw_name) == expected_singular
