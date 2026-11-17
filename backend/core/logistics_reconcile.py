"""Read-only remote reconciliation for unresolved mock HTTP ticket operations."""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.logistics import LogisticsService

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def backlog(service, alert_after_seconds):
    cutoff = (_now() - timedelta(seconds=alert_after_seconds)).isoformat()
    with service.connect() as db:
        remaining, oldest, overdue, held = db.execute(
            "SELECT COUNT(*), MIN(created_at), COUNT(CASE WHEN created_at<=? THEN 1 END), "
            "COUNT(CASE WHEN state='held' THEN 1 END) "
            "FROM ticket_operations WHERE state IN ('prepared','unknown','held')", (cutoff,),
        ).fetchone()
        heartbeat = db.execute("SELECT last_completed_timestamp_seconds FROM ticket_reconcile_status WHERE id=1").fetchone()
    return {"remaining": remaining, "oldest_age_seconds": _age(oldest) if oldest else 0,
            "overdue": overdue, "held": held, "last_completed_timestamp_seconds": heartbeat[0] if heartbeat else 0}


def _age(timestamp):
    return max(0, int((_now() - datetime.fromisoformat(timestamp)).total_seconds()))


def alert_after_seconds():
    return max(1, int(os.getenv("DIAN_TICKET_RECONCILE_ALERT_AFTER", "300")))


def pending_operations(service, limit=100, *, include_held=False):
    states = "('prepared','unknown','held')" if include_held else "('prepared','unknown')"
    with service.connect() as db:
        rows = db.execute("SELECT o.op_key,o.case_id,o.user_id,o.conv_id,o.params_hash,o.state,o.external_id,o.updated_at,o.created_at,r.reason,r.actor_id, "
                          "COALESCE(s.attempts,0),s.last_attempt_at,s.next_attempt_at,s.last_result "
                          "FROM ticket_operations o LEFT JOIN operation_reviews r ON r.op_key=o.op_key "
                          "LEFT JOIN ticket_reconcile_schedule s ON s.op_key=o.op_key "
                          f"WHERE o.state IN {states} ORDER BY o.updated_at,o.op_key LIMIT ?", (limit,)).fetchall()
    return [{"op_key": key, "case_id": case_id, "user_id": user_id, "conv_id": conv_id,
             "params_hash": params_hash, "state": state, "external_id": external_id,
             "review_reason": reason, "review_actor": actor_id,
             "reconcile_attempts": attempts, "last_attempt_at": last_attempt_at,
             "next_attempt_at": next_attempt_at if state != "held" else None, "last_reconcile_result": last_result,
             "age_seconds": _age(created_at), "since_update_seconds": _age(updated_at)}
            for (key, case_id, user_id, conv_id, params_hash, state, external_id, updated_at, created_at,
                 reason, actor_id, attempts, last_attempt_at, next_attempt_at, last_result) in rows]


def _due_operations(service, min_age_seconds, limit):
    now = _now()
    cutoff = now - timedelta(seconds=min_age_seconds)
    with service.connect() as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            """SELECT o.op_key,o.case_id,o.user_id,o.conv_id,o.params_hash,COALESCE(s.attempts,0) AS attempts
               FROM ticket_operations o LEFT JOIN ticket_reconcile_schedule s ON s.op_key=o.op_key
               WHERE o.state IN ('prepared','unknown') AND o.updated_at<=?
                 AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=?)
               ORDER BY COALESCE(s.next_attempt_at,o.updated_at),o.op_key LIMIT ?""",
            (cutoff.isoformat(), now.isoformat(), limit),
        ).fetchall()


def _record_attempt(service, op, outcome):
    now = _now()
    attempts = op["attempts"] + 1
    delay = min(300, 10 * 2 ** min(attempts - 1, 5))
    next_attempt = None if outcome == "resolved" else (now + timedelta(seconds=delay)).isoformat()
    with service.connect() as db:
        # Concurrent lookups may finish in either order. Older scans cannot
        # overwrite newer scheduling metadata. The domain owns business state.
        db.execute(
            """INSERT INTO ticket_reconcile_schedule VALUES (?,?,?,?,?)
               ON CONFLICT(op_key) DO UPDATE SET attempts=excluded.attempts,
                 last_attempt_at=excluded.last_attempt_at, next_attempt_at=excluded.next_attempt_at,
                 last_result=excluded.last_result
               WHERE ticket_reconcile_schedule.attempts<excluded.attempts""",
            (op["op_key"], attempts, now.isoformat(), next_attempt, outcome),
        )


def _reconcile_one(service, op):
    case = service.get_case(op["user_id"], op["case_id"], op["conv_id"])
    if not case or case["state"] not in ("ticket_pending", "ticket_unknown"):
        return "conflict"
    try:
        found = service.http_ticket_provider.lookup(op["op_key"])
    except Exception:
        return "unavailable"
    if found is None:
        return "not_found"
    if not isinstance(found, dict) or found.get("params_hash") != op["params_hash"] or not found.get("ticket_id"):
        return "conflict"
    req = SimpleNamespace(user_id=op["user_id"], conv_id=op["conv_id"],
                          request_id="background-reconcile:" + op["op_key"][:12])
    result = service._finish_ticket_operation(req, op["case_id"], op["op_key"], found["ticket_id"])
    return "resolved" if result["state"] == "ticket_created" else "conflict"


def scan_once(service=None, *, min_age_seconds=10, limit=100):
    service = service or LogisticsService()
    if service.http_ticket_provider is None:
        raise RuntimeError("核对器只支持独立 HTTP 模拟工单模式")
    stats = {"scanned": 0, "resolved": 0, "not_found": 0, "unavailable": 0, "conflict": 0}
    for op in _due_operations(service, min_age_seconds, max(0, min(limit, 100))):
        stats["scanned"] += 1
        outcome = _reconcile_one(service, op)
        _record_attempt(service, op, outcome)
        stats[outcome] += 1
    stats.update(backlog(service, alert_after_seconds()))
    completed_at = _now().timestamp()
    with service.connect() as db:
        db.execute("""INSERT INTO ticket_reconcile_status VALUES (1,?)
                      ON CONFLICT(id) DO UPDATE SET last_completed_timestamp_seconds=
                        MAX(last_completed_timestamp_seconds,excluded.last_completed_timestamp_seconds)""",
                   (completed_at,))
    stats["last_completed_timestamp_seconds"] = completed_at
    return stats


def main():
    interval = max(1, int(os.getenv("DIAN_TICKET_RECONCILE_INTERVAL", "10")))
    logging.basicConfig(level=logging.WARNING)
    service = LogisticsService()
    alerted = False
    while True:
        stats = scan_once(service)
        print(json.dumps(stats), flush=True)
        if stats["overdue"] and not alerted:
            log.warning("logistics_ticket_reconciliation_overdue count=%d oldest_age_seconds=%d held=%d",
                        stats["overdue"], stats["oldest_age_seconds"], stats["held"])
        alerted = bool(stats["overdue"])
        time.sleep(interval)


if __name__ == "__main__":
    main()
