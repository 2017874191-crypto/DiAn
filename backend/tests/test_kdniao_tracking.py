import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from agents.agent_orchestrator import GeneralAgent, Request
from agents.tracking_tools import build_tracking_tools
from core.intent_recognizer import IntentCategory
from core.kdniao_tracking import KdniaoTrackingProvider


ORDERS = [{"user_id": "owner", "order_id": "TEST1001", "tracking_no": "fixture-number"}]


def test_user_supplied_ascending_and_descending_traces_choose_the_same_latest_event():
    fixture = Path(__file__).resolve().parents[1] / "evaluation/fixtures/kdniao_signed_redacted.json"
    reply = json.loads(fixture.read_text(encoding="utf-8"))["response"]
    for traces in (reply["Traces"], list(reversed(reply["Traces"]))):
        body = {**reply, "Traces": traces}
        provider = KdniaoTrackingProvider("fixture-id", "fixture-key", ORDERS,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        tracking = provider.query_logistics("owner", "TEST1001")["tracking"]
        assert len(tracking["events"]) == 16 and tracking["chronology_verified"]
        assert tracking["latest"]["at"] == "2026-11-16 18:09:02"
        assert tracking["latest"]["description"] == "快件领取成功"
        assert tracking["latest"]["location"] == "成都市"
    invalid = {**reply, "Traces": [{**reply["Traces"][0], "AcceptTime": "unsupported-format"}]}
    provider = KdniaoTrackingProvider("fixture-id", "fixture-key", ORDERS,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=invalid)))
    tracking = provider.query_logistics("owner", "TEST1001")["tracking"]
    assert tracking["events"] and tracking["latest"] is None and not tracking["chronology_verified"]


def test_signature_ownership_and_accepted_empty_tracking():
    sent = []
    def respond(request):
        body = parse_qs(request.content.decode())
        payload = body["RequestData"][0]
        expected = base64.b64encode(hashlib.md5((payload + "fixture-key").encode()).hexdigest().encode()).decode()
        assert body["DataSign"] == [expected] and body["RequestType"] == ["8002"]
        assert json.loads(payload) == {"LogisticCode": "fixture-number", "CustomerName": "", "Sort": 1}
        sent.append(request)
        return httpx.Response(200, json={"Success": True, "State": "0", "Traces": [], "Reason": "暂无轨迹信息"})
    provider = KdniaoTrackingProvider("fixture-id", "fixture-key", ORDERS, transport=httpx.MockTransport(respond))
    assert not provider.query_logistics("other", "TEST1001")["success"] and sent == []
    result = provider.query_logistics("owner", "TEST1001")
    assert result["success"] and result["status"] == "no_tracking" and not result["has_traces"]
    assert result["tracking"]["latest"] is None and len(sent) == 1


def test_tracking_and_failure_outcomes_preserve_source_without_mock_fallback():
    for reply, success, status in (
        ({"Success": True, "State": "3", "Location": "测试城市", "Traces": [
            {"AcceptTime": "2026-11-19 10:00:00", "AcceptStation": "签收记录", "Action": "301"}]}, True, "tracking_found"),
        ({"Success": False, "Reason": "fixture rejected"}, False, None),
    ):
        provider = KdniaoTrackingProvider("fixture-id", "fixture-key", ORDERS,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply)))
        result = provider.query_logistics("owner", "TEST1001")
        assert result["source"] == "kdniao" and result["success"] == success
        assert result.get("status") == status
    def timeout(request):
        raise httpx.ReadTimeout("fixture", request=request)
    provider = KdniaoTrackingProvider("fixture-id", "fixture-key", ORDERS, transport=httpx.MockTransport(timeout))
    assert "超时" in provider.query_logistics("owner", "TEST1001")["error"]


@pytest.mark.asyncio
async def test_tracking_capability_does_not_enable_refund_queries_or_policy_calls():
    class Provider:
        def query_logistics(self, user, order):
            raise AssertionError("Policy questions must not consume query quota")
    tools = build_tracking_tools(Provider())
    agent = GeneralAgent(None, "fixture")
    agent.set_shared_tools(tools)
    req = Request(message="订单位置", user_id="owner", conv_id="test", task_goal="status_lookup",
                  intent=IntentCategory.ORDER_STATUS)
    assert agent.status_lookup_tools(req)
    req.intent = IntentCategory.REFUND
    assert not agent.status_lookup_tools(req)
    req.task_goal = "policy_question"
    result = await tools["query_authorized_tracking"].handler(req, {"order_id": "TEST1001"})
    assert not result["success"]
