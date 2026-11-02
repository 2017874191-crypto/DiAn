import asyncio

import pytest

from core.logistics import LogisticsService
from core.conversation_state import merge_state
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from agents.agent_orchestrator import AgentOrchestrator, AgentType, LogisticsAgent, Request

@pytest.fixture
def service(tmp_path):
    return LogisticsService(tmp_path / "cases.sqlite3")

def request(message="包裹疑似丢件", issue="logistics_lost", order="DEMO1001", state=None, user="demo-user", tracking=None):
    return Request(message=message, user_id=user, conv_id="logistics-test", intent=IntentCategory(issue),
        intent_group="logistics", urgency=UrgencyLevel.LOW, conversation_state=state or {},
        entities={"order_id": [order] if order else [], "tracking_no": [tracking] if tracking else []})

def test_missing_fields_and_ownership(service):
    result = service.process(request(order=None))
    assert result["case"]["state"] == "awaiting_info"
    result = service.process(request(user="another-user"))
    assert result["case"]["state"] == "awaiting_info"
    assert "events" not in result["calls"][0]["result"]

def test_order_tracking_mismatch(service):
    result = service.process(request(tracking="MOCK1002"))
    assert result["case"]["state"] == "awaiting_info"
    assert "不匹配" in result["reply"]

def test_confirmation_idempotency_restart(service):
    first_req = request()
    first = service.process(first_req)
    assert first["case"]["state"] == "awaiting_confirmation"
    second = service.process(request("确认建单", state=first_req.conversation_state))
    assert second["case"]["state"] == "ticket_created"
    restarted = LogisticsService(service.path)
    third = restarted.process(request("确认建单", state=first_req.conversation_state))
    assert second["case"]["ticket_id"] == third["case"]["ticket_id"]
    with service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1

def test_damage_needs_evidence_and_confirmation(service):
    req = request("包裹破损", "logistics_damaged", "DEMO1003")
    assert service.process(req)["case"]["state"] == "awaiting_evidence"
    req.message = "没有上传照片，确认建单"
    assert service.process(req)["case"]["state"] == "awaiting_evidence"
    req.message = "已上传外包装、面单和破损照片"
    assert service.process(req)["case"]["state"] == "awaiting_confirmation"
    req.message = "确认建单"
    assert service.process(req)["case"]["state"] == "ticket_created"

def test_dispute_conflict(service):
    result = service.process(request("显示签收但没收到", "logistics_dispute"))
    assert result["case"]["state"] == "manual_review"
    assert result["escalate"]

def test_timeout_retains_case_and_no_ticket(service):
    req = request(order="DEMO1004")
    result = service.process(req)
    assert result["case"]["state"] == "manual_review"
    assert service.get_case(req.user_id, result["case"]["case_id"])
    with service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0

def test_no_confirmation_on_negation(service):
    result = service.process(request("不要确认建单"))
    assert result["case"]["state"] == "awaiting_confirmation"

def test_case_switch_does_not_reuse_ticket(service):
    req = request("确认建单")
    first = service.process(req)
    result = service.process(request("显示签收但没收到", "logistics_dispute", "DEMO1002", req.conversation_state))
    assert result["case"]["case_id"] != first["case"]["case_id"]
    assert "ticket_id" not in result["case"]

def test_merge_preserves_pointer_and_resets_on_new_order():
    previous = {"domain":"commerce", "entities":{"order_id":["DEMO1001"]}, "logistics_case_id":"old"}
    merged = merge_state(previous, {}, "logistics", "确认建单", "r2")
    assert merged["logistics_case_id"] == "old"
    changed = merge_state(previous, {"order_id":["DEMO1002"]}, "logistics", "订单DEMO1002", "r3")
    assert "logistics_case_id" not in changed

@pytest.mark.parametrize("text,intent", [("疑似丢件", "logistics_lost"), ("显示签收但没收到", "logistics_dispute"), ("包裹破损", "logistics_damaged")])
def test_patterns_and_entities(text, intent):
    recognizer = IntentRecognizer("test-key")
    assert recognizer._pattern_recognize(text)["intent"].value == intent
    assert recognizer._extract_entities("运单号 MOCK1002")["tracking_no"] == ["MOCK1002"]

def test_full_orchestrator_continuation_without_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOMIND_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    async def run():
        req = request()
        first = await orch.run(req)
        assert first.agent_type == AgentType.LOGISTICS
        assert not first.supporting_agents
        follow = request("确认建单", issue="other", order=None, state=req.conversation_state)
        follow.intent_group = "other"
        second = await orch.run(follow)
        assert second.agent_type == AgentType.LOGISTICS
        assert follow.conversation_state["logistics_case_state"] == "ticket_created"
        assert "create_logistics_case" in second.tools_used
        assert second.tool_traces[-1]["event"] == "logistics_case_transition"
    asyncio.run(run())

def test_parallel_submissions_share_ticket(service):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: service.process(request("确认建单")), range(4)))
    assert len({r["case"]["ticket_id"] for r in results}) == 1


def test_explicit_handoff_is_not_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("ECHOMIND_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    req = request("包裹破损，转人工", issue="human_handoff")
    result = asyncio.run(orch.run(req))
    assert result.primary_agent == AgentType.ESCALATION
    assert result.escalated


def test_question_about_confirmation_does_not_create(service):
    result = service.process(request("如果我确认建单会怎样？"))
    assert result["case"]["state"] == "awaiting_confirmation"


@pytest.mark.parametrize("message,expected", [
    ("包裹破损，转人工", AgentType.ESCALATION),
    ("包裹破损，不要转人工", AgentType.LOGISTICS),
])
def test_handoff_overrides_misclassified_logistics(monkeypatch, tmp_path, message, expected):
    monkeypatch.setenv("ECHOMIND_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    req = request(message, issue="logistics_damaged", order="DEMO1003")
    result = asyncio.run(orch.run(req))
    assert result.primary_agent == expected
    assert result.escalated == (expected == AgentType.ESCALATION)


def test_case_view_and_manual_handoff_are_persisted(service):
    from core.logistics import case_view
    req = request()
    result = service.process(req)
    view = case_view(result["case"])
    assert view["can_confirm"] and view["source"] == "mock"
    assert "events" not in view and "facts" not in view
    service.request_manual_review(req)
    case = service.get_case(req.user_id, view["case_id"], req.conv_id)
    assert case_view(case)["state"] == "manual_review"
    assert not case_view(case)["can_confirm"]
    assert service.get_case(req.user_id, view["case_id"], "another-conversation") is None
    req.message = "确认建单"
    assert service.process(req)["case"]["state"] == "manual_review"
    assert service.get_case("another-user", view["case_id"]) is None
