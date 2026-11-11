"""Business-system ports and demo adapters; the HTTP path has bounded I/O and lookup."""
import copy
import hashlib
import re
import httpx
from typing import Protocol


class OrderProvider(Protocol):
    def query_order(self, user_id, order_id=None, tracking_no=None) -> dict: ...


class TrackingProvider(Protocol):
    def query_logistics(self, user_id, order_id) -> dict: ...


class TicketProvider(Protocol):
    def create_ticket(self, db, case_id, idempotency_key) -> dict: ...


class EvidenceProvider(Protocol):
    def declared(self, message, previous) -> bool: ...


class MockOrderProvider:
    def __init__(self, orders):
        self.orders = orders

    def query_order(self, user_id, order_id=None, tracking_no=None):
        matches = [o for o in self.orders if (o["order_id"] == order_id if order_id else o["tracking_no"] == tracking_no)]
        if not matches or matches[0]["user_id"] != user_id:
            return {"success": False, "error": "订单不存在或当前用户无权查询", "source": "mock"}
        order = matches[0]
        if tracking_no and order["tracking_no"] != tracking_no:
            return {"success": False, "error": "订单与运单不匹配，请重新核对", "source": "mock"}
        return {"success": True, "source": "mock", "order": {k: v for k, v in order.items() if k not in ("user_id", "fault")}}


class MockTrackingProvider:
    def __init__(self, orders, order_provider):
        self.orders = orders
        self.order_provider = order_provider

    def query_logistics(self, user_id, order_id):
        found = self.order_provider.query_order(user_id, order_id)
        if not found["success"]:
            return found
        order = next(o for o in self.orders if o["order_id"] == order_id)
        if order.get("fault") == "query_timeout":
            return {"success": False, "error": "模拟物流查询超时", "source": "mock", "retryable": True}
        return {"success": True, "source": "mock", "tracking": copy.deepcopy(found["order"])}


class SQLiteTicketProvider:
    def create_ticket(self, db, case_id, idempotency_key):
        if idempotency_key != case_id:
            raise ValueError("工单幂等键与案件不匹配")
        ticket_id = "MOCK-" + hashlib.sha256(case_id.encode()).hexdigest()[:16]
        db.execute("INSERT OR IGNORE INTO tickets VALUES (?, ?)", (case_id, ticket_id))
        ticket_id = db.execute("SELECT ticket_id FROM tickets WHERE case_id=?", (case_id,)).fetchone()[0]
        return {"success": True, "source": "mock", "ticket_id": ticket_id}


class HttpTicketProvider:
    """Separate mock service contract; lookup precedes any uncertain retry."""

    def __init__(self, url):
        self.url = url.rstrip("/")
        self.timeout = httpx.Timeout(3.0, connect=1.0)

    def lookup(self, key):
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.get(f"{self.url}/tickets/{key}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def create(self, key, params, params_hash):
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(f"{self.url}/tickets", json={"key": key, "params": params,
                                                             "params_hash": params_hash})
        response.raise_for_status()
        return response.json()


class DeclarationEvidenceProvider:
    def declared(self, message, previous):
        stated = bool(re.search(r"(?:已上传|已提供|已提交).{0,12}(?:照片|图片|视频)", message))
        denied = bool(re.search(r"未|没有|没上传|没提供", message))
        return bool(previous or (stated and not denied))
