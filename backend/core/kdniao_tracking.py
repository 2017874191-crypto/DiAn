"""Read-only KDniao 8002 tracking, using server-managed user/order bindings."""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


class KdniaoTrackingProvider:
    URL = "https://api.kdniao.com/api/dist"

    def __init__(self, business_id, api_key, orders, *, transport=None):
        if not business_id or not api_key:
            raise ValueError("KDNIAO_EBUSINESS_ID and KDNIAO_API_KEY are required")
        self.business_id, self.api_key, self.orders = business_id, api_key, orders
        self.transport = transport

    def query_logistics(self, user_id, order_id):
        order = next((o for o in self.orders if o["user_id"] == user_id and o["order_id"] == order_id), None)
        if order is None:
            return {"success": False, "source": "local_order_binding", "query_performed": False,
                    "error_code": "order_not_authorized",
                    "error": "订单不存在或当前用户无权查询。尚未调用外部物流接口，不能据此判断物流轨迹。"}
        params = {"LogisticCode": order["tracking_no"], "CustomerName": order.get("customer_name", ""),
                  "Sort": 1}
        if order.get("shipper_code"):
            params["ShipperCode"] = order["shipper_code"]
        payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        signature = base64.b64encode(hashlib.md5(
            (payload + self.api_key).encode("utf-8")).hexdigest().encode("ascii")).decode("ascii")
        try:
            with httpx.Client(timeout=httpx.Timeout(10, connect=5), trust_env=False,
                              transport=self.transport) as client:
                response = client.post(self.URL, data={
                    "EBusinessID": self.business_id, "RequestType": "8002",
                    "RequestData": payload, "DataSign": signature, "DataType": "2"})
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            return {"success": False, "source": "kdniao", "error": "物流接口查询超时，请稍后主动查询"}
        except (httpx.HTTPError, ValueError):
            return {"success": False, "source": "kdniao", "error": "物流接口暂不可用，请稍后主动查询"}
        if not isinstance(data, dict) or data.get("Success") is not True:
            return {"success": False, "source": "kdniao", "error": "快递鸟未接受查询，请核对账户权限或运单验证信息"}
        traces = data.get("Traces") or []
        if not isinstance(traces, list) or any(not isinstance(t, dict) for t in traces):
            return {"success": False, "source": "kdniao", "error": "物流接口返回的轨迹格式无效"}
        events = [{"at": t.get("AcceptTime"), "description": t.get("AcceptStation"),
                   "location": t.get("Location"), "state": t.get("Action")} for t in traces]
        chronology_verified = False
        latest = None
        if events:
            try:
                dated = [(datetime.strptime(event["at"], "%Y-%m-%d %H:%M:%S"), event) for event in events]
            except (ValueError, TypeError):
                pass  # Preserve unsupported timestamps without inventing which event is newest.
            else:
                events = [event for _at, event in sorted(dated, key=lambda pair: pair[0], reverse=True)]
                latest, chronology_verified = events[0], True
        return {
            "success": True, "source": "kdniao", "order_id": order_id,
            "query_performed": True,
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "has_traces": bool(events), "status": "tracking_found" if events else "no_tracking",
            "message": ("查询到物流轨迹，系统签收记录不证明用户实际收到。"
                        if events else "接口已受理，暂未查询到物流轨迹，不能据此判断丢件、运输中或已签收。"),
            "tracking": {"state": data.get("State"), "state_ex": data.get("StateEx"),
                         "location": data.get("Location"), "events": events,
                         "latest": latest, "chronology_verified": chronology_verified},
        }


def configured_tracking():
    if os.getenv("DIAN_TRACKING_PROVIDER", "mock") != "kdniao":
        return None
    orders = json.loads(Path(os.environ["KDNIAO_ORDER_BINDINGS"]).read_text(encoding="utf-8"))
    if not isinstance(orders, list) or any(
        not all(isinstance(o.get(k), str) and o[k] for k in ("user_id", "order_id", "tracking_no"))
        for o in orders
    ):
        raise ValueError("Tracking bindings require user_id, order_id and tracking_no")
    return KdniaoTrackingProvider(os.environ["KDNIAO_EBUSINESS_ID"], os.environ["KDNIAO_API_KEY"], orders)
