"""HTTP 链路回归：使用 Redis 协议模拟器验证锁、事务、记忆和幂等行为。"""
import asyncio
import json
import sys
import threading
import types
from collections import deque
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry

try:
    import chromadb
except ModuleNotFoundError:
    sys.modules["chromadb"] = types.ModuleType("chromadb")

import api.main as api
from agents.agent_orchestrator import (
    AgentOrchestrator, AgentType, BillingAgent, GeneralAgent, LogisticsAgent, TechnicalAgent,
    ResponseComposer, Request,
)
from agents.tools import AgentToolSpec
from evaluation.evaluator import EndToEndEvaluator, QualityScores
from core.concurrency import Capacity, KeyedLocks, LeaseLostError
from core.intent_recognizer import IntentCategory, IntentRecognizer, IntentResult, UrgencyLevel
from memory.conversation_memory import MemoryManager
from monitor.performance_monitor import PerformanceMonitor


def text_response(text="回复完成", stop="end_turn"):
    return SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason=stop)


@pytest_asyncio.fixture
async def services(monkeypatch):
    monkeypatch.setenv("DIAN_ENV", "development")
    monkeypatch.delenv("DIAN_API_TOKENS", raising=False)
    memory = MemoryManager.__new__(MemoryManager)
    memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    memory._request_locks = KeyedLocks()
    memory._query_episodic = AsyncMock(return_value={})
    memory._get_profile = AsyncMock(return_value={})
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=text_response())))
    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch._pool = {
        kind: [cls(client, "test-model")]
        for kind, cls in [(AgentType.GENERAL, GeneralAgent), (AgentType.BILLING, BillingAgent),
                          (AgentType.TECHNICAL, TechnicalAgent)]
    }
    orch._composer = ResponseComposer(client, "test-model")
    orch._recent_tool_traces = deque(maxlen=200)
    recognizer = IntentRecognizer.__new__(IntentRecognizer)

    async def recognize(message, history=None):
        return IntentResult(IntentCategory.REFUND, .95, UrgencyLevel.LOW, "billing",
                            recognizer._extract_entities(message), "test", 0)

    orch.recognize_intent = AsyncMock(side_effect=recognize)
    monkeypatch.setattr(api, "_memory", memory)
    monkeypatch.setattr(api, "_orchestrator", orch)
    monkeypatch.setattr(api, "_profile_updater", None)
    monkeypatch.setattr(api, "_monitor", None)
    monkeypatch.setattr(api, "_chat_capacity", Capacity(16, 32, 2, per_user=4))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as http:
        yield memory, orch, client, http
    await memory._redis.aclose()


def body(message="退款规则", request_id="one", conv_id="c"):
    return {"message": message, "user_id": "u", "conv_id": conv_id, "client_request_id": request_id}


@pytest.mark.asyncio
async def test_policy_goal_bypasses_logistics_issue_intake(services):
    _, orch, model, http = services
    orch.recognize_intent.return_value = IntentResult(
        IntentCategory.LOGISTICS_DISPUTE, .95, UrgencyLevel.LOW, "logistics", {}, "", 0,
        task_goal="policy_question")
    orch.recognize_intent.side_effect = None
    response = await http.post("/chat", json=body("显示签收但没收到，签收记录能证明本人收到吗？"))
    assert response.status_code == 200
    assert response.json()["agent_type"] == "general"
    assert response.json()["task_goal"] == "policy_question"
    assert model.messages.create.await_count == 1
    supplied = model.messages.create.call_args.kwargs["messages"]
    packet = json.loads(next(m["content"].split("\n", 1)[1] for m in supplied
                             if isinstance(m.get("content"), str) and m["content"].startswith("[角色输入契约]")))
    assert packet["task_goal"] == "policy_question"
    assert "triage_targets" not in packet and "verification_fields" not in packet


@pytest.mark.asyncio
async def test_unavailable_status_query_and_uncertain_goal_do_not_collect_fields(services):
    _, orch, model, http = services
    orch.recognize_intent.side_effect = None
    for goal, message in (("status_lookup", "退款申请审核通过了吗"), ("uncertain", "我没收到现在怎么办")):
        orch.recognize_intent.return_value = IntentResult(
            IntentCategory.REFUND, .95, UrgencyLevel.LOW, "billing", {}, "", 0, task_goal=goal)
        response = await http.post("/chat", json=body(message, goal, goal))
        assert response.status_code == 200 and response.json()["tools_used"] == []
        if goal == "status_lookup":
            assert "没有接入" in response.json()["response"]
        else:
            assert "您是想" in response.json()["response"]
    assert model.messages.create.await_count == 0


@pytest.mark.asyncio
async def test_uncertain_overall_goal_preserves_explicit_multi_role_subquestions(services):
    _, orch, _, http = services
    orch.recognize_intent.side_effect = None
    orch.recognize_intent.return_value = IntentResult(
        IntentCategory.TECHNICAL_LOGIN, .95, UrgencyLevel.LOW, "technical", {}, "", 0,
        task_goal="uncertain")
    response = await http.post("/chat", json=body("登录401，同时想知道退款申请审核多久"))
    assert response.status_code == 200
    assert set(response.json()["agent_types"]) == {"technical", "billing"}


@pytest.mark.asyncio
async def test_declared_status_capability_allows_real_tool_execution(services):
    _, orch, model, http = services
    seen = []
    async def lookup(req, args):
        seen.append(args["order_id"])
        return {"success": True, "status": "under_review"}
    orch.set_shared_tools({"lookup_status": AgentToolSpec(
        "lookup_status", "查询已授权退款状态", {"type": "object", "properties": {
            "order_id": {"type": "string"}}, "required": ["order_id"]}, lookup,
        capabilities=("status_lookup",))})
    packet = json.loads(orch._pool[AgentType.BILLING][0]._build_role_packet(
        Request(
            message="查询退款状态", user_id="u", conv_id="c", task_goal="status_lookup")))
    assert packet["verification_fields"]["missing_fields"] == ["order_id"]
    orch.recognize_intent.side_effect = None
    orch.recognize_intent.return_value = IntentResult(
        IntentCategory.REFUND, .95, UrgencyLevel.LOW, "billing", {}, "", 0, task_goal="status_lookup")
    model.messages.create.side_effect = [
        SimpleNamespace(content=[{"type": "tool_use", "id": "one", "name": "lookup_status",
                                  "input": {"order_id": "fixture"}}], stop_reason="tool_use"),
        text_response("查询结果是审核中"),
    ]
    response = await http.post("/chat", json=body("查询退款状态"))
    assert response.status_code == 200 and seen == ["fixture"]
    assert response.json()["tools_used"] == ["lookup_status"]


@pytest.mark.asyncio
async def test_existing_case_confirmation_preserves_write_gate_when_goal_is_misclassified(services, tmp_path, monkeypatch):
    _, orch, model, http = services
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "goal-case.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("DIAN_TEMPORAL_LOGISTICS", "false")
    agent = LogisticsAgent(model, "test-model")
    orch._pool[AgentType.LOGISTICS] = [agent]
    orch.recognize_intent.side_effect = None
    orch.recognize_intent.return_value = IntentResult(
        IntentCategory.LOGISTICS_LOST, .95, UrgencyLevel.LOW, "logistics", {"order_id": ["DEMO1001"]}, "", 0,
        task_goal="case_action")
    first = {**body("包裹疑似丢件，订单号 DEMO1001", "open"), "user_id": "demo-user"}
    opened = await http.post("/chat", json=first)
    assert opened.json()["logistics_case"]["state"] == "awaiting_confirmation"
    orch.recognize_intent.return_value = IntentResult(
        IntentCategory.LOGISTICS_LOST, .95, UrgencyLevel.LOW, "logistics", {}, "", 0,
        task_goal="policy_question")
    confirmed = await http.post("/chat", json={**first, "message": "确认建单", "client_request_id": "confirm"})
    assert confirmed.status_code == 200 and confirmed.json()["task_goal"] == "case_action"
    with agent.service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_damage_chat_reaches_ticket_and_preserves_refusal_ownership_and_replay(services, tmp_path, monkeypatch):
    _, orch, model, http = services
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "damage-main-path.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("DIAN_TEMPORAL_LOGISTICS", "false")
    agent = LogisticsAgent(model, "test-model")
    orch._pool[AgentType.LOGISTICS] = [agent]
    async def recognize(message, history=None):
        if message == "我同意提交工单":
            # A model/vector fallback misclassification must not abandon the active case.
            return IntentResult(IntentCategory.HUMAN_HANDOFF, .95, UrgencyLevel.HIGH, "escalation",
                                {}, "fallback", 0, task_goal="case_action")
        if message == "面单照片已经上传":
            return IntentResult(IntentCategory.OTHER, 0, UrgencyLevel.LOW, "other", {}, "fallback", 0,
                                task_goal="uncertain")
        return IntentResult(IntentCategory.LOGISTICS_DAMAGED, .95, UrgencyLevel.LOW, "logistics",
                            IntentRecognizer.__new__(IntentRecognizer)._extract_entities(message), "test", 0,
                            task_goal="case_action")
    orch.recognize_intent.side_effect = recognize
    statement = "订单 DEMO1003 商品破损，外包装、面单和商品照片我都已经提供了，请申请人工审核"
    async def send(user, conv, message, key):
        response = await http.post("/chat", json={"user_id": user, "conv_id": conv,
            "message": message, "client_request_id": key})
        assert response.status_code == 200, response.text
        return response.json()
    partial = await send("demo-user", "damage-positive", "订单 DEMO1003 商品破损，已提供外包装照片", "open")
    assert partial["logistics_case"]["state"] == "awaiting_evidence"
    assert not partial["logistics_case"]["can_confirm"]
    premature = await send("demo-user", "damage-positive", "我同意提交工单", "premature")
    assert premature["logistics_case"]["state"] == "awaiting_evidence"
    with agent.service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
    supplemented = await send("demo-user", "damage-positive", "面单照片已经上传", "supplement")
    assert supplemented["agent_type"] == "logistics"
    assert supplemented["logistics_case"]["declared_materials"] == ["外包装照片", "面单照片"]
    assert supplemented["logistics_case"]["state"] == "awaiting_evidence"
    offered = await send("demo-user", "damage-positive", statement, "materials")
    assert offered["logistics_case"]["state"] == "awaiting_confirmation"
    assert offered["logistics_case"]["declared_materials"] == ["外包装照片", "面单照片", "商品问题照片"]
    created = await send("demo-user", "damage-positive", "我确认提交这一笔破损模拟审核。", "confirm")
    assert created["logistics_case"]["state"] == "ticket_created"
    replay = await send("demo-user", "damage-positive", "我确认提交这一笔破损模拟审核。", "confirm")
    repeated = await send("demo-user", "damage-positive", "同一件事情，再提交一次", "repeat")
    assert replay["request_id"] == created["request_id"]
    assert repeated["logistics_case"]["ticket_id"] == created["logistics_case"]["ticket_id"]
    denied = await send("another-user", "damage-denied", statement, "open")
    assert denied["logistics_case"]["state"] == "awaiting_info"
    # A separate order keeps this refusal independent of the positive case's deduplication.
    refused_offer = await send("demo-user", "damage-refusal", statement.replace("DEMO1003", "DEMO1002"), "open")
    assert refused_offer["logistics_case"]["state"] == "awaiting_confirmation"
    refused = await send("demo-user", "damage-refusal", "先别提交，我不确认这个申请。", "refuse")
    assert refused["logistics_case"]["state"] == "awaiting_confirmation"
    with agent.service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    model.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_transaction_rejects_takeover_after_ownership_check(services, monkeypatch):
    memory, _, _, _ = services
    async with memory.conversation("u", "c") as lease:
        owned = lease.owned

        async def taken_over_after_read():
            still_owner = await owned()
            if still_owner:
                await memory._redis.set(lease.name, "next-owner", ex=10)
            return still_owner

        monkeypatch.setattr(lease, "owned", taken_over_after_read)
        with pytest.raises(LeaseLostError):
            await memory.commit_turn("u", "c", "stale", "old", {"status": "success", "response": "old"},
                                     state={"logistics_case_id": "old"}, lease=lease)
    assert await memory.get_turn_receipt("u", "c", "stale") is None
    assert await memory._redis.llen(memory._wm_key("u", "c")) == 0
    assert not await memory._redis.exists(memory._state_key("u", "c"))
    assert await memory._redis.get(lease.name) == "next-owner"


@pytest.mark.asyncio
async def test_health_responds_while_logistics_waits_for_sqlite_write_lock(services, tmp_path, monkeypatch):
    _, orch, model, http = services
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("DIAN_TEMPORAL_LOGISTICS", "false")
    agent = LogisticsAgent(model, "test-model")
    orch._pool[AgentType.LOGISTICS] = [agent]
    held, release, finished = threading.Event(), threading.Event(), threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = agent.service.process

    def lock_db():
        with agent.service.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            held.set()
            release.wait(2)

    def process(request):
        loop.call_soon_threadsafe(entered.set)
        try:
            return original(request)
        finally:
            finished.set()

    lock_holder = threading.Thread(target=lock_db)
    lock_holder.start()
    monkeypatch.setattr(agent.service, "process", process)
    request = None
    try:
        assert await asyncio.to_thread(held.wait, 1)
        request = asyncio.create_task(http.post("/chat", json=body("包裹丢件，订单号 DEMO1001")))
        await asyncio.wait_for(entered.wait(), 3)
        health = await http.get("/health")
        print(f"health={health.status_code}, logistics_finished={finished.is_set()}")
        assert health.status_code == 200 and not finished.is_set()
    finally:
        release.set()
        await asyncio.to_thread(lock_holder.join)
        if request:
            response = await request
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_timed_out_logistics_write_keeps_its_slot_and_conversation_lock(services, tmp_path, monkeypatch):
    memory, orch, model, http = services
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("DIAN_TEMPORAL_LOGISTICS", "false")
    monkeypatch.setattr(LogisticsAgent, "capacity", Capacity(1, 0, 2))
    agent = LogisticsAgent(model, "test-model")
    orch._pool[AgentType.LOGISTICS] = [agent]
    first = {**body("包裹丢件，订单号 DEMO1001", "ask"), "user_id": "demo-user"}
    response = await http.post("/chat", json=first)
    assert response.json()["logistics_case"]["state"] == "awaiting_confirmation"
    response = await http.post("/chat", json={**first, "conv_id": "other"})
    assert response.json()["logistics_case"]["state"] == "awaiting_confirmation"

    release, entered = threading.Event(), threading.Event()
    original = agent.service.process

    def pause(req):
        entered.set()
        assert release.wait(5)
        return original(req)

    monkeypatch.setattr(agent.service, "process", pause)
    confirmation = {**first, "message": "确认建单", "client_request_id": "confirm"}
    monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", ".35")
    try:
        timed_out = await http.post("/chat", json=confirmation)
        assert timed_out.status_code == 504 and entered.is_set()
        assert api._chat_capacity.active == LogisticsAgent.capacity.active == 1
        assert len(await memory._redis.keys("lock:conversation:*")) == 1

        monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", "2")
        busy = await http.post("/chat", json={**confirmation, "conv_id": "other"})
        assert busy.status_code == 429 and busy.headers["retry-after"] == "2"
        assert await memory.get_turn_receipt("demo-user", "other", "confirm") is None
        metrics = (await http.get("/metrics")).text
        assert 'dian_capacity_active{resource="logistics"} 1.0' in metrics
        assert 'dian_capacity_rejected_total{resource="logistics"} 1.0' in metrics
        assert agent.service.get_active_case("demo-user", "c")["state"] == "awaiting_confirmation"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*api._chat_tasks, return_exceptions=True), 3)

    assert api._chat_capacity.active == LogisticsAgent.capacity.active == 0
    assert not await memory._redis.keys("lock:conversation:*")
    replay = await http.post("/chat", json=confirmation)
    assert replay.status_code == 503 and replay.json()["error_code"] == "tool_outcome_unknown"
    assert agent.service.get_active_case("demo-user", "c")["state"] == "ticket_created"
    with agent.service.connect() as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_background_is_skipped_for_fixed_logistics_and_loaded_for_model_fallback(services, tmp_path, monkeypatch):
    memory, orch, model, http = services
    monkeypatch.setenv("DIAN_LOGISTICS_DB", str(tmp_path / "cases.sqlite3"))
    monkeypatch.setenv("DIAN_TICKET_PROVIDER", "sqlite")
    monkeypatch.setenv("DIAN_TEMPORAL_LOGISTICS", "false")
    agent = LogisticsAgent(model, "test-model")
    orch._pool[AgentType.LOGISTICS] = [agent]
    memory._query_episodic.return_value = {"documents": [["历史片段"] * memory.HISTORY_TOP_K]}
    memory._get_profile.return_value = {"name": "profile-fixture"}

    request = {**body("包裹丢件，订单号 DEMO1001"), "user_id": "demo-user"}
    response = await http.post("/chat", json=request)
    assert response.status_code == 200
    assert response.json()["logistics_case"]["state"] == "awaiting_confirmation"
    assert "memory_background" not in response.json()["performance"]["stages_ms"]
    memory._query_episodic.assert_not_awaited()
    memory._get_profile.assert_not_awaited()
    model.messages.create.assert_not_awaited()

    monkeypatch.setattr(agent, "_call_llm", AsyncMock(side_effect=RuntimeError("fixture: read-only logistics failure")))
    response = await http.post("/chat", json={**request, "conv_id": "fallback"})
    assert response.status_code == 200 and response.json()["agent_type"] == "general"
    assert "memory_background" in response.json()["performance"]["stages_ms"]
    memory._query_episodic.assert_awaited_once()
    memory._get_profile.assert_awaited_once()
    messages = model.messages.create.await_args.kwargs["messages"]
    assert "历史片段" in messages[0]["content"] and "profile-fixture" in messages[0]["content"]


@pytest.mark.asyncio
async def test_parallel_model_agents_share_one_full_background_read(services):
    memory, orch, model, http = services
    memory._query_episodic.return_value = {"documents": [["历史片段"] * memory.HISTORY_TOP_K]}
    memory._get_profile.return_value = {"name": "profile-fixture"}
    await memory._redis.set(memory._summary_key("u", "c"), "已有摘要")
    await memory.commit_turn("u", "c", "previous", "上一轮用户文本", {"status": "success", "response": "已记录"})

    response = await http.post("/chat", json=body("登录失败而且被多扣了钱，怎么退款？"))
    assert response.status_code == 200
    assert set(response.json()["agent_types"]) == {"technical", "billing"}
    history = orch.recognize_intent.await_args.kwargs["history"]
    assert history[0]["content"] == "上一轮用户文本"
    memory._query_episodic.assert_awaited_once()
    memory._get_profile.assert_awaited_once()
    backgrounds = [call.kwargs["messages"][0]["content"] for call in model.messages.create.await_args_list
                   if call.kwargs["messages"][0]["content"].startswith("[背景信息]")]
    assert len(backgrounds) == 2
    for background in backgrounds:
        assert all(value in background for value in ("历史片段", "profile-fixture", "已有摘要", "上一轮用户文本"))


@pytest.mark.asyncio
async def test_background_read_checks_lease_before_model_and_receipt(services):
    memory, _, model, http = services

    async def take_over(_user_id):
        [lease_key] = await memory._redis.keys("lock:conversation:*")
        await memory._redis.set(lease_key, "fixture-next-owner", ex=10)
        return {}

    memory._get_profile.side_effect = take_over
    response = await http.post("/chat", json=body())
    assert response.status_code == 503
    assert "读取回复背景期间会话锁已失效" in response.json()["detail"]
    model.messages.create.assert_not_awaited()
    assert await memory.get_turn_receipt("u", "c", "one") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_timeout, chat_timeout, expected_status", [(.35, 2, 429), (2, .35, 504)])
async def test_chat_bounds_active_waiting_and_authenticated_user_admission(
    services, monkeypatch, queue_timeout, chat_timeout, expected_status,
):
    memory, orch, _, http = services
    monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", "2")
    gate = Capacity(1, 1, queue_timeout, per_user=1)
    monkeypatch.setattr(api, "_chat_capacity", gate)
    tokens = {f"local-test-key-with-more-than-24-{user}": {"user_id": user, "role": "user"} for user in ("a", "b", "c")}
    monkeypatch.setenv("DIAN_API_TOKENS", json.dumps(tokens))
    entered, release = asyncio.Event(), asyncio.Event()
    original = orch.recognize_intent.side_effect

    async def recognize(message, history=None):
        if message == "暂停请求":
            entered.set()
            await release.wait()
        return await original(message, history)

    orch.recognize_intent.side_effect = recognize

    async def post(user, message="退款规则", conv="c"):
        return await http.post("/chat", json=body(message, conv_id=conv),
                               headers={"Authorization": f"Bearer local-test-key-with-more-than-24-{user}"})

    first = asyncio.create_task(post("a", "暂停请求"))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await post("a", conv="another")).status_code == 429
        monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", str(chat_timeout))
        queued = asyncio.create_task(post("b"))
        async with asyncio.timeout(1):
            while gate.waiting != 1:
                await asyncio.sleep(.001)
        assert (gate.active, gate.waiting) == (1, 1)
        assert (await post("c")).status_code == 429
        assert (await queued).status_code == expected_status
        assert await memory.get_turn_receipt("b", "c", "one") is None
        await asyncio.wait_for(asyncio.gather(*[task for task in api._chat_tasks if task.cancelling()],
                                             return_exceptions=True), 1)
        assert (gate.active, gate.waiting, gate.rejected) == (1, 0, 3 if expected_status == 429 else 2)
    finally:
        release.set()
        if queued is not None:
            await asyncio.gather(queued, return_exceptions=True)
        first_result = await first
    assert first_result.status_code == 200
    monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", "2")
    assert (await post("b")).status_code == 200
    assert (gate.active, gate.waiting) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("active,per_user,expected", [(8, None, (6, 0, 0)), (2, None, (2, 4, 0)), (2, 1, (2, 1, 3))])
async def test_capacity_ablation_same_six_request_burst(services, monkeypatch, active, per_user, expected):
    _, orch, _, http = services
    gate = Capacity(active, 4, 2, per_user=per_user)
    monkeypatch.setattr(api, "_chat_capacity", gate)
    tokens = {f"local-test-key-with-more-than-24-{user}": {"user_id": user, "role": "user"} for user in ("a", "b", "c")}
    monkeypatch.setenv("DIAN_API_TOKENS", json.dumps(tokens))
    release = asyncio.Event()
    recognize = orch.recognize_intent.side_effect

    async def pause(message, history=None):
        await release.wait()
        return await recognize(message, history)

    orch.recognize_intent.side_effect = pause
    tasks = [
        asyncio.create_task(http.post("/chat", json=body(conv_id=f"conv-{index}"),
                                      headers={"Authorization": f"Bearer local-test-key-with-more-than-24-{user}"}))
        for index, user in enumerate(("a", "a", "a", "a", "b", "c"))
    ]
    try:
        async with asyncio.timeout(1.5):
            while gate.active + gate.waiting + gate.rejected < len(tasks):
                await asyncio.sleep(.001)
        observed = (gate.active, gate.waiting, gate.rejected)
        print(f"burst_active={observed[0]} burst_waiting={observed[1]} burst_rejected={observed[2]}")
        assert observed == expected
    finally:
        release.set()
        responses = await asyncio.gather(*tasks)
    assert sum(reply.status_code == 429 for reply in responses) == expected[2]
    assert sum(reply.status_code == 200 for reply in responses) == len(tasks) - expected[2]


@pytest.mark.asyncio
async def test_chat_reports_stage_timings_and_provider_usage_without_changing_replay(services):
    memory, orch, client, http = services
    client.messages.create.return_value = SimpleNamespace(content=[{"type":"text", "text":"回复完成"}],
        stop_reason="end_turn", usage=SimpleNamespace(input_tokens=15, output_tokens=8))
    first = await http.post("/chat", json=body())
    perf = first.json()["performance"]
    assert {"memory_read", "intent", "agent_execution"} <= set(perf["stages_ms"])
    assert perf["llm"]["calls"] == 1 and perf["llm"]["output_tokens"] == 8
    assert first.json() == (await http.post("/chat", json=body())).json()


@pytest.mark.asyncio
async def test_failed_turn_can_retry_after_recovery_without_duplicate_history(services):
    memory, orch, model, http = services
    model.messages.create.side_effect = RuntimeError("provider down")
    first = await http.post("/chat", json=body())
    assert first.status_code == 503
    assert first.json()["status"] == "failed" and first.json()["retryable"]
    assert await memory._redis.llen(memory._wm_key("u", "c")) == 0
    assert not await memory._redis.exists(memory._state_key("u", "c"))
    assert (await http.post("/chat", json=body("另一条消息"))).status_code == 409
    model.messages.create.side_effect = None
    recovered = await http.post("/chat", json=body())
    repeat = await http.post("/chat", json=body())
    assert recovered.status_code == repeat.status_code == 200
    assert recovered.json() == repeat.json()
    assert recovered.json()["status"] == "success"
    assert model.messages.create.await_count == 3  # 两次失败尝试 + 一次成功
    assert await memory._redis.llen(memory._wm_key("u", "c")) == 2
    assert await memory.get_trace(recovered.json()["request_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text,stop,status,http_status", [
    ("", "end_turn", "failed", 503),
    ("退款需要先", "max_tokens", "partial", 200),
])
async def test_empty_and_truncated_answers_are_not_recorded_as_complete(services, text, stop, status, http_status):
    memory, orch, model, http = services
    model.messages.create.return_value = text_response(text, stop)
    response = await http.post("/chat", json=body())
    assert response.status_code == http_status and response.json()["status"] == status
    assert orch._pool[AgentType.BILLING][0].stats.success == 0
    if status == "partial":
        repeat = await http.post("/chat", json=body())
        assert repeat.json() == response.json()
        assert model.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_unsafe_tool_is_recorded_before_execution_and_not_replayed(services):
    memory, orch, model, http = services
    writes = []

    async def write(req, args):
        marker = await memory.get_turn_receipt("u", "c", "one")
        assert marker["response"]["error_code"] == "tool_outcome_unknown"
        assert not marker["response"]["retryable"]
        writes.append(args)
        return {"success": True}

    orch.set_shared_tools({"write": AgentToolSpec("write", "test", {"type": "object"}, write, retry_safe=False)})
    model.messages.create.side_effect = [
        SimpleNamespace(content=[{"type": "tool_use", "id": "w", "name": "write", "input": {}}]),
        RuntimeError("lost model response"), RuntimeError("fallback down"),
    ]
    first = await http.post("/chat", json=body())
    assert first.status_code == 503 and not first.json()["retryable"]
    model.messages.create.side_effect = None
    repeat = await http.post("/chat", json=body())
    assert repeat.json() == first.json()
    assert len(writes) == 1 and model.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_repeated_write_within_one_model_tool_loop_executes_once(services):
    memory, orch, model, http = services
    write = AsyncMock(return_value={"success": True})
    orch.set_shared_tools({"write": AgentToolSpec("write", "test", {"type": "object"}, write, retry_safe=False)})
    model.messages.create.side_effect = [
        SimpleNamespace(content=[{"type": "tool_use", "id": "w1", "name": "write", "input": {}}]),
        SimpleNamespace(content=[{"type": "tool_use", "id": "w2", "name": "write", "input": {}}]),
        text_response("已处理"),
    ]
    response = await http.post("/chat", json=body())
    assert response.status_code == 200 and write.await_count == 1
    trace = await memory.get_trace(response.json()["request_id"])
    assert trace["tool_calls"][1]["success"] is False


@pytest.mark.asyncio
async def test_unsafe_tool_timeout_preserves_non_replayable_receipt(services, monkeypatch):
    memory, orch, model, http = services
    calls = 0

    async def generate(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(content=[{"type": "tool_use", "id": "w", "name": "write", "input": {}}])
        await asyncio.Event().wait()

    write = AsyncMock(return_value={"success": True})
    orch.set_shared_tools({"write": AgentToolSpec("write", "test", {"type": "object"}, write, retry_safe=False)})
    model.messages.create.side_effect = generate
    monkeypatch.setenv("DIAN_CHAT_TIMEOUT_S", ".2")
    first = await http.post("/chat", json=body())
    assert first.status_code == 504
    repeat = await http.post("/chat", json=body())
    assert repeat.status_code == 503 and not repeat.json()["retryable"]
    assert write.await_count == 1 and calls == 2
    assert await memory._redis.llen(memory._wm_key("u", "c")) == 0


@pytest.mark.asyncio
async def test_concurrent_retries_execute_once_and_bind_authenticated_identity(services, monkeypatch):
    memory, orch, model, http = services
    token = "test-token-with-at-least-24-characters"
    monkeypatch.setenv("DIAN_API_TOKENS", json.dumps({token: {"user_id": "real", "role": "user"}}))
    headers = {"Authorization": "Bearer " + token}
    first, second = await asyncio.gather(*(http.post("/chat", json=body(), headers=headers) for _ in range(2)))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and model.messages.create.await_count == 1
    assert await memory._redis.llen(memory._wm_key("real", "c")) == 2
    assert not await memory._redis.exists(memory._wm_key("u", "c"))
    assert (await http.get("/monitor", headers=headers)).status_code == 403
    assert (await http.post("/chat", json=body())).status_code == 401


@pytest.mark.asyncio
async def test_slots_survive_turns_but_reset_for_another_order_or_conversation(services):
    memory, orch, model, http = services
    await http.post("/chat", json=body("订单号 #12345，昨天支付99元", "first"))
    second = await http.post("/chat", json=body("退款多久到？", "second"))
    assert second.json()["entities"]["order_id"] == ["12345"]
    assert second.json()["entities"]["amount"] == ["99元"]
    assert second.json()["entities"]["date"] == ["昨天"]
    third = await http.post("/chat", json=body("另一笔订单 #67890", "third"))
    assert third.json()["entities"]["order_id"] == ["67890"]
    assert not third.json()["entities"]["amount"] and not third.json()["entities"]["date"]
    other = await http.post("/chat", json=body("退款多久到？", "fourth", "other"))
    assert not other.json()["entities"]["order_id"]
    state = json.loads(await memory._redis.get(memory._state_key("u", "c")))
    assert state["sources"]["order_id"] == third.json()["request_id"]


@pytest.mark.asyncio
async def test_dialog_evaluation_uses_same_slot_merge_as_chat(services):
    memory, orch, model, http = services
    orch._intent_recognizer = SimpleNamespace(recognize=orch.recognize_intent)
    evaluator = EndToEndEvaluator.__new__(EndToEndEvaluator)
    evaluator._orchestrator = orch
    evaluator._judge = SimpleNamespace(judge=AsyncMock(return_value=QualityScores(1, 1, 1, 1)))
    results = await evaluator._evaluate_dialog_case({
        "turns": ["订单号 #12345，昨天支付99元", "退款多久到？"],
        "expected_entities": {"order_id": ["12345"], "amount": ["99元"]},
    }, 0)
    assert results[-1].passed and results[-1].metadata["deterministic_checks"]["entities"]
    assert results[-1].metadata["entities"]["date"] == ["昨天"]


def raw_message(index):
    return json.dumps({"role": "user", "content": f"old-{index}",
                       "ts": datetime.now().isoformat(), "metadata": {}})


@pytest.mark.asyncio
async def test_background_compression_does_not_block_chat_or_drop_new_messages(services):
    memory, orch, model, http = services
    started, finish = asyncio.Event(), asyncio.Event()

    async def summarize(**kwargs):
        started.set()
        await finish.wait()
        return text_response("有效摘要")

    memory._client = SimpleNamespace(messages=SimpleNamespace(create=summarize))
    memory._model = "fake"
    memory._merge_summary = AsyncMock(return_value="有效摘要")
    memory._store_episodic = AsyncMock()
    memory.update_profile = AsyncMock()
    key = memory._wm_key("u", "c")
    await memory._redis.rpush(key, *(raw_message(i) for i in reversed(range(25))))
    maintain = asyncio.create_task(memory.maintain("u", "c"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        response = await asyncio.wait_for(http.post("/chat", json=body()), 1)
        assert response.status_code == 200 and not maintain.done()
        finish.set()
        await maintain
        rows = [json.loads(row) for row in await memory._redis.lrange(key, 0, -1)]
        assert len(rows) == 7 and rows[0]["role"] == "assistant" and rows[1]["content"] == "退款规则"
        assert await memory._redis.get(memory._summary_key("u", "c")) == "有效摘要"
        memory._store_episodic.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(maintain, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_compression_commits_only_one_snapshot(services):
    memory, *_ = services
    count = 0
    started, finish = asyncio.Event(), asyncio.Event()

    async def summarize(**kwargs):
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await finish.wait()
        return text_response("有效摘要")

    memory._client = SimpleNamespace(messages=SimpleNamespace(create=summarize))
    memory._model = "fake"
    memory._merge_summary = AsyncMock(return_value="有效摘要")
    memory._store_episodic = AsyncMock()
    key = memory._wm_key("u", "c")
    await memory._redis.rpush(key, *(raw_message(i) for i in reversed(range(25))))
    tasks = [asyncio.create_task(memory._compress("u", "c")) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), 1)
        finish.set()
        await asyncio.gather(*tasks)
        assert await memory._redis.llen(key) == 5
        memory._store_episodic.assert_awaited_once()
    finally:
        finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_timing_includes_memory_commit_and_metrics_work_without_extra_port(services, monkeypatch):
    memory, orch, model, http = services
    registry = CollectorRegistry()
    monitor = PerformanceMonitor(orch, SimpleNamespace(), registry=registry)
    monkeypatch.setattr(api, "_monitor", monitor)
    original_commit = memory.commit_turn

    async def delayed_commit(*args, **kwargs):
        await asyncio.sleep(.05)
        return await original_commit(*args, **kwargs)

    monkeypatch.setattr(memory, "commit_turn", delayed_commit)
    response = await http.post("/chat", json=body())
    total = float(response.headers["Server-Timing"].split("=")[1])
    assert total >= response.json()["latency_ms"] + 20
    samples = [s for metric in registry.collect() for s in metric.samples]
    assert next(s.value for s in samples if s.name == "requests_total") == 1
    assert next(s.value for s in samples if s.name == "chat_latency_ms_count") == 1
    histogram = monitor._prom["agent_latency_ms"].labels(agent="bucket-check")
    histogram.observe(3000)
    assert next(s.value for s in histogram.collect()[0].samples
                if s.name.endswith("_bucket") and s.labels["le"] == "3000.0") == 1
