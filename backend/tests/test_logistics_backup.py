"""Backup/restore business invariants, not just a file-copy success check."""
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest

from core.logistics import LogisticsService
from core.logistics_backup import TABLES, copy_database
from tests.test_logistics_cases import request
from tests.test_logistics_http_operation import start_server, remote_count


def table_digests(path):
    with closing(sqlite3.connect(path)) as db:
        return {name: hashlib.sha256(json.dumps(sorted(db.execute(f'SELECT * FROM "{name}"').fetchall()),
                            ensure_ascii=True).encode()).hexdigest() for name in TABLES}


def test_wal_backup_and_restored_completed_case_keep_one_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    source = tmp_path / "source.sqlite3"
    service = LogisticsService(source)
    with closing(sqlite3.connect(source)) as anchor:
        anchor.execute("PRAGMA journal_mode=WAL")
        anchor.execute("PRAGMA wal_autocheckpoint=0")
        anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        opened = request()
        service.process(opened)
        created = service.process(request("确认建单", state=opened.conversation_state))
        # Counterexample: copying only the main file loses committed WAL state.
        control = tmp_path / "main-file-only.sqlite3"
        shutil.copyfile(source, control)
        with closing(sqlite3.connect(control)) as db:
            assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
        backup = tmp_path / "backup.sqlite3"
        report = copy_database(source, backup)
        assert report["integrity_check"] == "ok" and report["rows"]["tickets"] == 1
        assert table_digests(backup) == table_digests(source)
    restored = tmp_path / "restored.sqlite3"
    copy_database(backup, restored)
    assert table_digests(restored) == table_digests(backup)
    recovered = LogisticsService(restored)
    assert recovered.get_active_case("another-user", opened.conv_id) is None
    assert recovered.get_active_case(opened.user_id, opened.conv_id)["ticket_id"] == created["case"]["ticket_id"]
    again = recovered.process(request("确认建单"))
    assert again["case"]["ticket_id"] == created["case"]["ticket_id"]
    other = request()
    other.conv_id = "another-conversation"
    assert recovered.process(other)["case"]["state"] == "ticket_already_open"
    with recovered.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


def test_restored_unknown_operation_reconciles_existing_remote_ticket(tmp_path, monkeypatch):
    remote = tmp_path / "remote.sqlite3"
    process, url = start_server(remote, drop=True)
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "http_mock")
    monkeypatch.setenv("DIAN_TICKET_URL", url)
    try:
        source = tmp_path / "source.sqlite3"
        service = LogisticsService(source)
        service.process(request())
        unknown = service.process(request("确认建单"))
        assert unknown["case"]["state"] == "ticket_unknown" and remote_count(remote) == 1
        with service.connect() as db:
            original_key = db.execute("SELECT op_key FROM ticket_operations").fetchone()[0]
        snapshot = tmp_path / "backup.sqlite3"
        restored = tmp_path / "restored.sqlite3"
        copy_database(source, snapshot)
        copy_database(snapshot, restored)
        assert table_digests(restored) == table_digests(source)
        recovered = LogisticsService(restored)
        # Any new POST after restore is a failure: the remote result already exists.
        def no_create(*_args):
            raise AssertionError("Restore must look up the saved operation key")
        recovered.http_ticket_provider.create = no_create
        result = recovered.process(request("继续"))
        assert result["case"]["state"] == "ticket_created" and remote_count(remote) == 1
        with recovered.connect() as db:
            assert db.execute("SELECT op_key,state,external_id FROM ticket_operations").fetchone() == (
                original_key, "succeeded", result["case"]["ticket_id"])
        assert recovered.get_run("demo-user", "logistics-test", unknown["run_id"])["state"] == "completed"
    finally:
        process.terminate()
        process.join(5)


def test_backup_rejects_existing_target_and_wrong_database(tmp_path):
    source = tmp_path / "source.sqlite3"
    LogisticsService(source)
    target = tmp_path / "keep.sqlite3"
    target.write_bytes(b"existing file must survive")
    with pytest.raises(FileExistsError):
        copy_database(source, target)
    assert target.read_bytes() == b"existing file must survive"
    with pytest.raises(ValueError, match="must differ"):
        copy_database(source, source)
    invalid = tmp_path / "wrong.sqlite3"
    with closing(sqlite3.connect(invalid)) as db:
        db.execute("CREATE TABLE unrelated(x)")
    fresh = tmp_path / "not-created.sqlite3"
    with pytest.raises(ValueError, match="Not a current"):
        copy_database(invalid, fresh)
    assert not fresh.exists()
