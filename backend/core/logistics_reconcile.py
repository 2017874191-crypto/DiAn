"""Read-only remote reconciliation for unresolved mock HTTP ticket operations."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.logistics import LogisticsService

log = logging.getLogger(__name__)


def backlog(service, alert_after_seconds):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=alert_after_seconds)).isoformat()
    with service.connect() as db:
        remaining, oldest, overdue, held = db.execute(
            "SELECT COUNT(*), MIN(created_at), COUNT(CASE WHEN created_at<=? THEN 1 END), "
            "COUNT(CASE WHEN state='held' THEN 1 END) "
            "FROM ticket_operations WHERE state IN ('prepared','unknown','held')", (cutoff,),
        ).fetchone()
    return {"remaining": remaining, "oldest_age_seconds": _age(oldest) if oldest else 0,
            "overdue": overdue, "held": held}


def _age(timestamp):
    return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()))


def alert_after_seconds():
    return max(1, int(os.getenv("DIAN_TICKET_RECONCILE_ALERT_AFTER", "300")))


def pending_operations(service, limit=100, *, include_held=False):
    states = "('prepared','unknown','held')" if include_held else "('prepared','unknown')"
    with service.connect() as db:
        rows = db.execute("SELECT o.op_key,o.case_id,o.user_id,o.conv_id,o.params_hash,o.state,o.external_id,o.updated_at,o.created_at,r.reason,r.actor_id "
                          "FROM ticket_operations o LEFT JOIN operation_reviews r ON r.op_key=o.op_key "
                          f"WHERE o.state IN {states} ORDER BY o.updated_at,o.op_key LIMIT ?", (limit,)).fetchall()
    return [{"op_key": key, "case_id": case_id, "user_id": user_id, "conv_id": conv_id,
             "params_hash": params_hash, "state": state, "external_id": external_id,
             "review_reason": reason, "review_actor": actor_id,
             "age_seconds": _age(created_at), "since_update_seconds": _age(updated_at)}
            for key, case_id, user_id, conv_id, params_hash, state, external_id, updated_at, created_at, reason, actor_id in rows]


def scan_once(service=None, *, min_age_seconds=10, limit=100):
    service = service or LogisticsService()
    if service.http_ticket_provider is None:
        raise RuntimeError("核对器只支持独立 HTTP 模拟工单模式")
    stats = {"scanned": 0, "resolved": 0, "not_found": 0, "unavailable": 0,
             "conflict": 0, "remaining": 0, "oldest_age_seconds": 0}
    for op in pending_operations(service, limit):
        if op["since_update_seconds"] < min_age_seconds:
            continue
        stats["scanned"] += 1
        case = service.get_case(op["user_id"], op["case_id"], op["conv_id"])
        if not case or case["state"] not in ("ticket_pending", "ticket_unknown"):
            stats["conflict"] += 1
            continue
        try:
            found = service.http_ticket_provider.lookup(op["op_key"])
        except Exception:
            stats["unavailable"] += 1
            continue
        if found is None:
            stats["not_found"] += 1
            continue  # Never create a ticket from the background scanner.
        if not isinstance(found, dict) or found.get("params_hash") != op["params_hash"] or not found.get("ticket_id"):
            stats["conflict"] += 1
            continue
        req = SimpleNamespace(user_id=op["user_id"], conv_id=op["conv_id"],
                              request_id="background-reconcile:" + op["op_key"][:12])
        result = service._finish_ticket_operation(req, op["case_id"], op["op_key"], found["ticket_id"])
        if result["state"] == "ticket_created":
            stats["resolved"] += 1
        else:
            stats["conflict"] += 1
    stats.update(backlog(service, alert_after_seconds()))
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
