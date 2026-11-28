"""SQLite 적재 배치의 롤백 검증과 트랜잭션 경계 테스트."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from autoetl.core.db_loader import inspect_db_catalog, load_tables, rollback_batch
from autoetl.core.errors import InputError


def _load_update_and_insert(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "items.csv"
    source.write_text("item_id,label\nT01,before\nT02,keep\n", encoding="utf-8")
    database = tmp_path / "warehouse.sqlite"
    load_tables([source], db_path=database)

    source.write_text("item_id,label\nT01,after\nT03,new\n", encoding="utf-8")
    _, loaded = load_tables([source], db_path=database)
    assert loaded.status == "succeeded"
    return database, loaded.batch_id


def test_rollback_preserves_unmanaged_table(tmp_path: Path) -> None:
    database = tmp_path / "unmanaged.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE items (item_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO items VALUES ('T01')")

    with pytest.raises(InputError, match="데이터 관리 테이블"):
        rollback_batch(database, "synthetic-batch")

    catalog = inspect_db_catalog(database)
    assert catalog["management_tables"] == []
    assert catalog["batches"] == []
    assert catalog["business_tables"] == [
        {"table": "items", "row_count": 1, "columns": ["item_id"]}
    ]


def test_rollback_rejects_unknown_and_replayed_batches(tmp_path: Path) -> None:
    source = tmp_path / "items.csv"
    source.write_text("item_id,label\nT01,before\nT02,keep\n", encoding="utf-8")
    database = tmp_path / "warehouse.sqlite"
    _, loaded = load_tables([source], db_path=database)

    with pytest.raises(InputError, match="존재하지 않는 batch_id"):
        rollback_batch(database, "synthetic-missing-batch")
    assert inspect_db_catalog(database)["batches"][0]["status"] == "succeeded"

    result = rollback_batch(database, loaded.batch_id)
    assert result["status"] == "rolled_back"
    assert result["total_deleted"] == 2
    with pytest.raises(InputError, match="이미 롤백된 배치"):
        rollback_batch(database, loaded.batch_id)
    assert inspect_db_catalog(database)["batches"][0]["status"] == "rolled_back"


def test_rollback_requires_batch_history_on_the_target_table(tmp_path: Path) -> None:
    source = tmp_path / "items.csv"
    source.write_text("item_id,label\nT01,keep\n", encoding="utf-8")
    database = tmp_path / "warehouse.sqlite"
    _, loaded = load_tables([source], db_path=database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE untracked_items AS SELECT item_id, label FROM items")
        connection.execute("DROP TABLE items")
        connection.execute("ALTER TABLE untracked_items RENAME TO items")

    with pytest.raises(InputError, match="이력 컬럼"):
        rollback_batch(database, loaded.batch_id)

    catalog = inspect_db_catalog(database)
    assert catalog["business_tables"][0]["row_count"] == 1
    assert catalog["batches"][0]["status"] == "succeeded"


def test_rollback_restores_deleted_inserts_if_an_update_is_ignored(tmp_path: Path) -> None:
    database, batch_id = _load_update_and_insert(tmp_path)

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER ignore_restore BEFORE UPDATE ON items "
            "WHEN NEW.label = 'before' BEGIN SELECT RAISE(IGNORE); END"
        )

    with pytest.raises(InputError, match="기존 행 복원 대상"):
        rollback_batch(database, batch_id)

    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT item_id, label FROM items ORDER BY item_id").fetchall()
    assert rows == [("T01", "after"), ("T02", "keep"), ("T03", "new")]
    assert inspect_db_catalog(database)["batches"][0]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("external_edit", "error_text"),
    [
        ("DELETE FROM items WHERE item_id = 'T01'", "갱신 대상 행이 이미 삭제"),
        (
            "UPDATE items SET _etl_batch_id = 'synthetic-other-batch' WHERE item_id = 'T01'",
            "갱신 대상 행이 후속 배치",
        ),
    ],
    ids=["deleted", "reassigned"],
)
def test_rollback_refuses_to_restore_rows_changed_outside_the_batch(
    tmp_path: Path, external_edit: str, error_text: str
) -> None:
    database, batch_id = _load_update_and_insert(tmp_path)

    query = "SELECT item_id, label, _etl_batch_id FROM items ORDER BY item_id"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(external_edit)
        observed_rows = connection.execute(query).fetchall()

    with pytest.raises(InputError, match=error_text):
        rollback_batch(database, batch_id)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(query).fetchall() == observed_rows
    assert inspect_db_catalog(database)["batches"][0]["status"] == "succeeded"


def test_catalog_preserves_dataset_kind_from_a_different_version(tmp_path: Path) -> None:
    source = tmp_path / "items.csv"
    source.write_text("item_id,label\nT01,keep\n", encoding="utf-8")
    database = tmp_path / "warehouse.sqlite"
    _, loaded = load_tables([source], db_path=database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE _etl_datasets SET dataset_kind = ? WHERE batch_id = ?",
            ("synthetic_legacy_kind", loaded.batch_id),
        )

    dataset = inspect_db_catalog(database)["datasets"][0]
    assert dataset["dataset_kind"] == "synthetic_legacy_kind"
    assert dataset["dataset_kind_label"] == "synthetic_legacy_kind"
    assert dataset["rows_loaded"] == 1
