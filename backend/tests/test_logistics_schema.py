"""Legacy adoption, atomic failure and version compatibility on isolated databases."""
import sqlite3
from contextlib import closing

import pytest

from core.logistics import LogisticsService
from core import logistics_schema as schema
from core.logistics_backup import copy_database
from tests.test_logistics_cases import request


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump()), db.execute("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    path = tmp_path / "legacy.sqlite3"
    service = LogisticsService(path)
    opened = request()
    service.process(opened)
    created = service.process(request("确认建单"))["case"]
    with service.connect() as db:
        # Represent a historical unversioned copy, never a live downgrade command.
        db.execute("ALTER TABLE ticket_operations DROP COLUMN created_at")
        db.execute("INSERT INTO ticket_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "original-operation-key", created["case_id"], "demo-user", "logistics-test", "original-hash",
            "{}", "original-confirmation", "demo-2026-11-v1", "succeeded", created["ticket_id"],
            "2026-11-22T10:00:00+00:00"))
        db.execute("DROP TABLE active_cases")
        db.execute("PRAGMA user_version=0")
    return path, created


def test_legacy_upgrade_preserves_business_identity_and_backup_version(legacy, tmp_path):
    path, created = legacy
    with closing(sqlite3.connect(path)) as db:
        rows = {table: db.execute(f'SELECT * FROM "{table}"').fetchall()
                for table in ("cases", "tickets", "logistics_runs", "logistics_steps")}
        operation = db.execute("SELECT * FROM ticket_operations").fetchone()
    service = LogisticsService(path)
    with service.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert rows == {table: db.execute(f'SELECT * FROM "{table}"').fetchall() for table in rows}
        assert db.execute("SELECT * FROM ticket_operations").fetchone() == (*operation, operation[-1])
    assert service.get_active_case("demo-user", "logistics-test")["case_id"] == created["case_id"]
    assert service.get_active_case("other-user", "logistics-test") is None
    assert service.process(request("确认建单"))["case"]["ticket_id"] == created["ticket_id"]
    backup, restored = tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3"
    assert copy_database(path, backup)["schema_version"] == 1
    assert copy_database(backup, restored)["schema_version"] == 1
    assert dump(restored) == dump(path)


def test_repeated_start_does_not_repeat_migration(tmp_path, monkeypatch):
    path = tmp_path / "new.sqlite3"
    LogisticsService(path)
    before = dump(path)
    def forbidden(_db):
        raise AssertionError("A versioned database must not be migrated twice")
    monkeypatch.setattr(schema, "_migrate_0_to_1", forbidden)
    LogisticsService(path)
    assert dump(path) == before


def test_failed_migration_rolls_back_ddl_data_and_version_then_can_retry(legacy):
    path, _ = legacy
    before = dump(path)
    class FailAfterVersionWrite(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if sql == "PRAGMA user_version=1":
                raise RuntimeError("injected failure after DDL, backfill and version write")
            return result
    with closing(sqlite3.connect(path, factory=FailAfterVersionWrite)) as db:
        with pytest.raises(RuntimeError, match="injected failure"):
            schema.ensure_schema(db)
    assert dump(path) == before
    LogisticsService(path)
    assert dump(path)[1] == 1


def test_unknown_higher_version_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "future.sqlite3"
    LogisticsService(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=2")
    before = dump(path)
    with pytest.raises(RuntimeError, match="Unsupported logistics schema version 2"):
        LogisticsService(path)
    assert dump(path) == before
