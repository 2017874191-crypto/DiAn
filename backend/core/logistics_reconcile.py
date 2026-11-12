"""Read-only remote reconciliation for unresolved mock HTTP ticket operations."""
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from core.logistics import LogisticsService


def pending_operations(service, limit=100, *, include_held=False):
    now = datetime.now(timezone.utc)
    states = "('prepared','unknown','held')" if include_held else "('prepared','unknown')"
    with service.connect() as db:
        rows = db.execute("SELECT o.op_key,o.case_id,o.user_id,o.conv_id,o.params_hash,o.state,o.external_id,o.updated_at,r.reason,r.actor_id "
                          "FROM ticket_operations o LEFT JOIN operation_reviews r ON r.op_key=o.op_key "
                          f"WHERE o.state IN {states} ORDER BY o.updated_at,o.op_key LIMIT ?", (limit,)).fetchall()
    return [{"op_key": key, "case_id": case_id, "user_id": user_id, "conv_id": conv_id,
             "params_hash": params_hash, "state": state, "external_id": external_id,
             "review_reason": reason, "review_actor": actor_id,
             "age_seconds": max(0, int((now - datetime.fromisoformat(updated_at)).total_seconds()))}
            for key, case_id, user_id, conv_id, params_hash, state, external_id, updated_at, reason, actor_id in rows]


def scan_once(service=None, *, min_age_seconds=10, limit=100):
    service = service or LogisticsService()
    if service.http_ticket_provider is None:
        raise RuntimeError("核对器只支持独立 HTTP 模拟工单模式")
    stats = {"scanned": 0, "resolved": 0, "not_found": 0, "unavailable": 0,
             "conflict": 0, "remaining": 0, "oldest_age_seconds": 0}
    for op in pending_operations(service, limit):
        if op["age_seconds"] < min_age_seconds:
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
    remaining = pending_operations(service, limit=100000)
    stats["remaining"] = len(remaining)
    stats["oldest_age_seconds"] = max((op["age_seconds"] for op in remaining), default=0)
    return stats


def main():
    interval = max(1, int(os.getenv("DIAN_TICKET_RECONCILE_INTERVAL", "10")))
    while True:
        print(scan_once(), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
