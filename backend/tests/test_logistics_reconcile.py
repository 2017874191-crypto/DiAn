import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import logistics_reconcile
from core.logistics import LogisticsService
from core.logistics_reconcile import backlog, pending_operations, scan_once


def seed_unknown(service, count):
    rows = []
    with service.connect() as db:
        for i in range(count):
            req = SimpleNamespace(user_id="demo-user", conv_id=f"conv-{i}", request_id=f"confirm-{i}")
            case = {
                "case_id": f"case-{i}", "issue": "logistics_lost", "order_id": f"DEMO{i}",
                "tracking_no": f"MOCK{i}", "policy_version": "test", "state": "ticket_unknown",
                "events": [],
            }
            key = service._prepare_ticket_operation(db, req, case)
            timestamp = f"2026-01-01T00:00:{int(i == count - 1):02d}+00:00"
            db.execute("UPDATE ticket_operations SET state='unknown',created_at=?,updated_at=? WHERE op_key=?",
                       (timestamp, timestamp, key))
            db.execute("INSERT INTO cases (id,user_id,conv_id,payload) VALUES (?,?,?,?)",
                       (case["case_id"], req.user_id, req.conv_id, json.dumps(case)))
            params_hash = db.execute("SELECT params_hash FROM ticket_operations WHERE op_key=?", (key,)).fetchone()[0]
            rows.append((key, params_hash))
    return rows


@pytest.mark.parametrize("restart_delay", [0, 10])
def test_missing_results_do_not_starve_later_operation(tmp_path, monkeypatch, restart_delay):
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "http_mock")
    monkeypatch.setenv("DIAN_TICKET_URL", "http://127.0.0.1:1")
    path = tmp_path / "cases.sqlite3"
    service = LogisticsService(path)
    *missing, (target, params_hash) = seed_unknown(service, 101)
    remote_tickets = {target: {"ticket_id": "mock-1", "params_hash": params_hash}}
    provider = Mock(spec=["lookup", "create"])
    provider.lookup.side_effect = remote_tickets.get
    service.http_ticket_provider = provider
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(logistics_reconcile, "_now", lambda: now)
    first = scan_once(service, min_age_seconds=0)

    # Reopening the database discards all process-local scheduling state.
    service = LogisticsService(path)
    service.http_ticket_provider = provider
    assert backlog(service, 300)["last_completed_timestamp_seconds"] == now.timestamp()
    now += timedelta(seconds=restart_delay)
    second = scan_once(service, min_age_seconds=0)
    print(f"restart_delay={restart_delay}s first={first}, second={second}, "
          f"GETs={provider.lookup.call_count}, POSTs={provider.create.call_count}")

    assert first["scanned"] == first["not_found"] == 100
    assert second["scanned"] == (100 if restart_delay else 1)
    assert second["resolved"] == 1
    assert provider.lookup.call_args_list[100].args == (target,)
    assert second["remaining"] == len(missing)
    provider.create.assert_not_called()
    with service.connect() as db:
        assert db.execute("SELECT state,external_id FROM ticket_operations WHERE op_key=?", (target,)).fetchone() == (
            "succeeded", "mock-1")


def test_scan_backoff_and_heartbeat_are_separate_from_business_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "http_mock")
    monkeypatch.setenv("DIAN_TICKET_URL", "http://127.0.0.1:1")
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(logistics_reconcile, "_now", lambda: now)
    service = LogisticsService(tmp_path / "cases.sqlite3")
    [(target, _)] = seed_unknown(service, 1)
    provider = Mock(spec=["lookup", "create"])
    provider.lookup.side_effect = [ConnectionError(), {"params_hash": "wrong", "ticket_id": "mock-1"}, None]
    service.http_ticket_provider = provider
    before = pending_operations(service)[0]
    assert backlog(service, 300)["last_completed_timestamp_seconds"] == 0

    assert scan_once(service)["unavailable"] == 1
    first = pending_operations(service)[0]
    assert first["reconcile_attempts"] == 1
    assert first["last_reconcile_result"] == "unavailable"
    assert datetime.fromisoformat(first["next_attempt_at"]) - now == timedelta(seconds=10)

    now += timedelta(seconds=9)
    assert scan_once(service)["scanned"] == 0
    assert provider.lookup.call_count == 1
    assert backlog(service, 300)["last_completed_timestamp_seconds"] == now.timestamp()

    now += timedelta(seconds=1)
    assert scan_once(service)["conflict"] == 1
    second = pending_operations(service)[0]
    assert second["reconcile_attempts"] == 2
    assert second["last_reconcile_result"] == "conflict"
    assert datetime.fromisoformat(second["next_attempt_at"]) - now == timedelta(seconds=20)
    assert second["age_seconds"] == before["age_seconds"] + 10
    assert second["since_update_seconds"] == before["since_update_seconds"] + 10
    assert second["state"] == "unknown"

    completed_at = now.timestamp()
    now += timedelta(seconds=20)
    # Persisting scheduling state is part of a complete scan. A failed scan
    # cannot refresh its liveness metric.
    monkeypatch.setattr(logistics_reconcile, "_record_attempt", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        scan_once(service)
    assert backlog(service, 300)["last_completed_timestamp_seconds"] == completed_at
    provider.create.assert_not_called()
    assert service.get_case("demo-user", "case-0", "conv-0")["state"] == "ticket_unknown"
