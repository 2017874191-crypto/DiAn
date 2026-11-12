import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from core.logistics import LogisticsService, detect_issue
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


def test_cross_conversation_confirmation_and_reapplication_after_closure(service):
    def run(conv, message, order="DEMO1001", tracking=None):
        req = request(message, order=order, tracking=tracking)
        req.conv_id = conv
        return service.process(req)["case"]

    assert run("one", "包裹疑似丢件")["state"] == "awaiting_confirmation"
    assert run("two", "包裹疑似丢件", None, "MOCK1001")["state"] == "awaiting_confirmation"
    barrier = Barrier(2)

    def confirm(conv):
        barrier.wait(timeout=5)
        return run(conv, "确认建单", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = dict(zip(("one", "two"), executor.map(confirm, ("one", "two"))))
    assert {case["state"] for case in outcomes.values()} == {"ticket_created", "ticket_already_open"}
    opened = next(conv for conv, case in outcomes.items() if case["state"] == "ticket_created")
    blocked = "two" if opened == "one" else "one"
    assert "ticket_id" not in outcomes[blocked]
    case_id = outcomes[opened]["case_id"]
    assert service.close_ticket_case(case_id, "模拟工单已完成人工处理并确认结案", "admin",
                                     user_id="demo-user", conv_id=blocked) is None
    with service.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
    service.close_ticket_case(case_id, "模拟工单已完成人工处理并确认结案", "admin",
                              user_id="demo-user", conv_id=opened)
    assert run(opened, "确认建单", None)["case_id"] == case_id
    assert run(blocked, "确认建单", None)["state"] == "awaiting_confirmation"
    reopened = run(blocked, "确认建单", None)
    assert reopened["state"] == "ticket_created"
    assert reopened["ticket_id"] != outcomes[opened]["ticket_id"]
    with service.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2

def test_first_turn_confirmation_requires_prior_verified_offer(service):
    req = request("确认建单")
    first = service.process(req)
    assert first["case"]["state"] == "awaiting_confirmation"
    assert "ticket_id" not in first["case"]
    assert all(call["tool_name"] != "create_logistics_case" for call in first["calls"])
    second = service.process(request("确认建单", state=req.conversation_state))
    assert second["case"]["state"] == "ticket_created"

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


def test_provider_ports_allow_mock_substitution_without_changing_case_rules(tmp_path):
    from core.logistics_providers import MockOrderProvider, MockTrackingProvider, SQLiteTicketProvider, DeclarationEvidenceProvider
    import json
    from pathlib import Path
    orders = json.loads((Path(__file__).resolve().parents[1] / "config/logistics/fixtures.json").read_text(encoding="utf-8"))["orders"]
    order = MockOrderProvider(orders)
    service = LogisticsService(tmp_path / "provider.sqlite3", order_provider=order,
        tracking_provider=MockTrackingProvider(orders, order), ticket_provider=SQLiteTicketProvider(),
        evidence_provider=DeclarationEvidenceProvider())
    req = request()
    first = service.process(req)
    second = service.process(request("确认建单", state=req.conversation_state))
    assert first["case"]["state"] == "awaiting_confirmation"
    assert second["case"]["state"] == "ticket_created"
    assert second["calls"][-1]["source"] == "mock"
    with pytest.raises(ValueError, match="物流提供者"):
        LogisticsService(tmp_path / "invalid.sqlite3", order_provider=order)

def test_no_confirmation_on_negation(service):
    result = service.process(request("不要确认建单"))
    assert result["case"]["state"] == "awaiting_confirmation"

def test_case_switch_does_not_reuse_ticket(service):
    req = request()
    service.process(req)
    first = service.process(request("确认建单", state=req.conversation_state))
    assert first["case"]["state"] == "ticket_created"
    result = service.process(request("显示签收但没收到", "logistics_dispute", "DEMO1002", req.conversation_state))
    assert result["case"]["case_id"] != first["case"]["case_id"]
    assert "ticket_id" not in result["case"]


def test_active_case_switch_and_conversation_boundary(service):
    first_req = request()
    first = service.process(first_req)["case"]
    second_req = Request(message="换个订单查询丢件", user_id="demo-user", conv_id=first_req.conv_id,
        intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1002"]},
        conversation_state=first_req.conversation_state)
    second = service.process(second_req)["case"]
    assert second["case_id"] != first["case_id"]
    assert service.get_active_case("demo-user", first_req.conv_id)["case_id"] == second["case_id"]
    assert service.get_case("demo-user", first["case_id"], first_req.conv_id)
    assert service.get_active_case("demo-user", "other-conversation") is None
    assert service.get_active_case("another-user", first_req.conv_id) is None
    # A stale pointer from another conversation must not move its case.
    forged = Request(message="确认建单", user_id="demo-user", conv_id="other-conversation",
        intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
        conversation_state={"logistics_case_id": first["case_id"]})
    service.process(forged)
    assert service.get_case("demo-user", first["case_id"], first_req.conv_id)
    assert service.get_case("demo-user", first["case_id"], "other-conversation") is None


def test_legacy_cases_backfill_latest_owned_case(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute("CREATE TABLE cases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conv_id TEXT NOT NULL, payload TEXT NOT NULL)")
            for case_id, updated in (("old", "2026-11-10T23:00:00+08:00"), ("new", "2026-11-10T18:00:00+00:00")):
                db.execute("INSERT INTO cases VALUES (?,?,?,?)", (case_id, "demo-user", "owned",
                    json.dumps({"case_id": case_id, "issue": "logistics_lost", "updated_at": updated})))
    service = LogisticsService(path)
    assert service.get_active_case("demo-user", "owned")["case_id"] == "new"
    assert service.get_active_case("demo-user", "another") is None
    assert LogisticsService(path).get_active_case("demo-user", "owned")["case_id"] == "new"

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
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    async def run():
        req = request()
        first = await orch.run(req)
        assert first.agent_type == AgentType.LOGISTICS
        assert not first.supporting_agents
        from unittest.mock import patch
        import api.main as api
        with patch.object(api, "_orchestrator", orch):
            recovered = await api._restore_logistics_state(req.user_id, req.conv_id, {})
        assert recovered["logistics_case_id"] == req.conversation_state["logistics_case_id"]
        follow = request("确认建单", issue="other", order=None, state=recovered)
        follow.intent_group = "other"
        second = await orch.run(follow)
        assert second.agent_type == AgentType.LOGISTICS
        assert follow.conversation_state["logistics_case_state"] == "ticket_created"
        assert "create_logistics_case" in second.tools_used
        assert second.tool_traces[-1]["event"] == "logistics_case_transition"
    asyncio.run(run())

def test_parallel_submissions_share_ticket(service):
    from concurrent.futures import ThreadPoolExecutor
    req = request()
    service.process(req)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: service.process(request("确认建单", state=req.conversation_state)), range(4)))
    assert all(r["case"]["state"] == "ticket_created" for r in results)
    assert len({r["case"]["ticket_id"] for r in results}) == 1

def test_negated_damage_does_not_create_issue():
    assert detect_issue("我想问运费，没有破损") is None
    assert detect_issue("没有破损，但是物流一直不更新") == "logistics_lost"

def test_rules_only_skips_model_and_embedding(monkeypatch):
    monkeypatch.setenv("INTENT_RECOGNITION_MODE", "rules_only")
    recognizer = IntentRecognizer("test-key")
    async def fail(*args, **kwargs):
        raise AssertionError("model or embedding was called")
    recognizer._llm_recognize = fail
    recognizer._embedding_recognize = fail
    result = asyncio.run(recognizer.recognize("包裹破损，订单号 DEMO1003"))
    assert result.intent == IntentCategory.LOGISTICS_DAMAGED
    assert result.source_scores["llm"] == result.source_scores["embedding"] == 0

def test_rules_only_unknown_route_does_not_call_general_model(monkeypatch, tmp_path):
    monkeypatch.setenv("INTENT_RECOGNITION_MODE", "rules_only")
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    async def fail(_req):
        raise AssertionError("general model was called")
    orch._pool[AgentType.GENERAL][0]._call_llm = fail
    result = asyncio.run(orch.run(request("我的商品压坏了怎么办", issue="other", order=None)))
    assert result.agent_type == AgentType.GENERAL
    assert not result.tools_used


def test_shipping_fee_with_model_timeout_does_not_open_damage_case(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from agents.agent_orchestrator import AgentResponse
    monkeypatch.setenv("INTENT_RECOGNITION_MODE", "full")
    monkeypatch.setenv("INTENT_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    rec = IntentRecognizer("test-key")
    rec._llm_recognize = AsyncMock(return_value={"intent": IntentCategory.OTHER, "confidence": 0.0,
                                                  "failed": True})
    rec._embedding_recognize = AsyncMock(return_value={"intent": IntentCategory.LOGISTICS_DAMAGED,
                                                        "confidence": .205, "source": "local-ngram"})
    message = "寄送一件商品的运费怎么算？"
    recognized = asyncio.run(rec.recognize(message))
    assert recognized.intent == IntentCategory.QUERY
    orch = AgentOrchestrator("test-key")
    async def fake_handle(_req):
        return AgentResponse(content="请查看配送费用规则", agent_type=AgentType.GENERAL, success=True)
    orch._pool[AgentType.GENERAL][0].handle = fake_handle
    req = request(message, issue=recognized.intent.value, order=None)
    req.intent_group = recognized.intent_group
    req.entities = recognized.entities
    result = asyncio.run(orch.run(req))
    assert result.primary_agent != AgentType.LOGISTICS
    assert not req.conversation_state.get("logistics_case_id")


def test_explicit_handoff_is_not_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    req = request("包裹破损，转人工", issue="human_handoff")
    result = asyncio.run(orch.run(req))
    assert result.primary_agent == AgentType.ESCALATION
    assert result.escalated


def test_question_about_confirmation_does_not_create(service):
    result = service.process(request("如果我确认建单会怎样？"))
    assert result["case"]["state"] == "awaiting_confirmation"


@pytest.mark.parametrize("message", [
    "我想了解配送费用，不是要查询丢件。",
    "我想了解订单号 DEMO1001 的运费是否能退，不是包裹丢件。",
    "我想了解运费，没有包裹破损。",
])
def test_negated_lost_issue_is_not_created(monkeypatch, tmp_path, message):
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    assert detect_issue(message) is None
    orch = AgentOrchestrator("test-key")
    req = request(message, issue="logistics_lost", order="DEMO1001")
    async def fake_handle(_req):
        from agents.agent_orchestrator import AgentResponse
        return AgentResponse(content="费用请按账单规则核对", agent_type=AgentType.BILLING, success=True)
    for agent_type in (AgentType.BILLING, AgentType.GENERAL):
        orch._pool[agent_type][0].handle = fake_handle
    result = asyncio.run(orch.run(req))
    assert result.primary_agent != AgentType.LOGISTICS
    assert not req.conversation_state.get("logistics_case_id")


def test_confirmation_question_and_negation_continue_without_ticket(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    orch = AgentOrchestrator("test-key")
    first = request()
    asyncio.run(orch.run(first))
    state = first.conversation_state
    for message in ("如果我确认建单会怎样？", "不要确认建单"):
        follow = request(message, issue="other", order=None, state=state)
        follow.intent_group = "other"
        result = asyncio.run(orch.run(follow))
        assert result.primary_agent == AgentType.LOGISTICS
        assert follow.conversation_state["logistics_case_state"] == "awaiting_confirmation"
        assert "create_logistics_case" not in result.tools_used
        state = follow.conversation_state


@pytest.mark.parametrize("message,expected", [
    ("包裹破损，转人工", AgentType.ESCALATION),
    ("包裹破损，不要转人工", AgentType.LOGISTICS),
])
def test_handoff_overrides_misclassified_logistics(monkeypatch, tmp_path, message, expected):
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
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
