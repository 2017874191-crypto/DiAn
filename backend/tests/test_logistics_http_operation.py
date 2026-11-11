"""External ticket outcome is separate from the case transaction."""
import multiprocessing as mp
import json
import hashlib
import os
import socket
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agents.agent_orchestrator import AgentType, LogisticsAgent, OrchestratorResult, Request
import api.main as api
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from core.logistics_providers import HttpTicketProvider
from core.mock_ticket_http import serve
from core.temporal_logistics import QUEUE, LogisticsTurn, dispatch, execute, process_logistics_turn


def start_server(path, drop):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = mp.Process(target=serve, args=("127.0.0.1", port, str(path), drop))
    process.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url + "/tickets/probe", timeout=.2, trust_env=False)
            return process, url
        except httpx.TransportError:
            time.sleep(.05)
    process.terminate(); process.join(5)
    raise RuntimeError("mock server failed to start")


def request(message, state=None, client_id=None):
    return Request(message=message, user_id="demo-user", conv_id="http-case",
                   intent=IntentCategory.LOGISTICS_LOST,
                   entities={"order_id": ["DEMO1001"]} if not state else {},
                   conversation_state=state or {}, client_request_id=client_id)


def remote_count(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT count(*) FROM tickets").fetchone()[0]


def test_ablation_unstable_key_creates_second_remote_ticket(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=False)
    try:
        provider = HttpTicketProvider(url)
        params = {"case_id": "same-case", "issue": "logistics_lost"}
        params_hash = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        first = provider.create("fixed-key", params, params_hash)
        assert provider.create("fixed-key", params, params_hash) == first
        assert remote_count(remote_db) == 1
        provider.create("new-key-on-retry", params, params_hash)  # Ablated stable Operation key.
        assert remote_count(remote_db) == 2
    finally:
        process.terminate(); process.join(5)


def crash_after_remote_create(local_db, url):
    from core.logistics_providers import HttpTicketProvider

    original = HttpTicketProvider.create

    def create_then_crash(provider, key, params, params_hash):
        original(provider, key, params, params_hash)
        os._exit(73)  # Remote commit completed; local outcome was not persisted.

    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(local_db),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}), \
         patch.object(HttpTicketProvider, "create", create_then_crash):
        LogisticsService().process(request("确认建单"))


def test_process_crash_after_remote_commit_recovers(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    local_db = tmp_path / "cases.sqlite3"
    process, url = start_server(remote_db, drop=False)
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(local_db),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}):
        try:
            service = LogisticsService()
            assert service.process(request("包裹疑似丢件，订单号 DEMO1001"))["case"]["state"] == "awaiting_confirmation"
            child = mp.Process(target=crash_after_remote_create, args=(local_db, url))
            child.start(); child.join(10)
            assert child.exitcode == 73
            assert remote_count(remote_db) == 1
            with service.connect() as db:
                assert db.execute("SELECT state FROM ticket_operations").fetchone()[0] == "prepared"
            recovered = LogisticsService().process(request("继续"))
            assert recovered["case"]["state"] == "ticket_created"
            assert remote_count(remote_db) == 1
        finally:
            process.terminate(); process.join(5)


def test_response_lost_reconciles_without_duplicate(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=True)
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(tmp_path / "cases.sqlite3"),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}):
        try:
            service = LogisticsService()
            first = request("包裹疑似丢件，订单号 DEMO1001")
            assert service.process(first)["case"]["state"] == "awaiting_confirmation"
            confirmed = request("确认建单", first.conversation_state)
            unknown = service.process(confirmed)
            assert unknown["case"]["state"] == "ticket_unknown"
            assert remote_count(remote_db) == 1
            with service.connect() as db:
                op = db.execute("SELECT state,confirmation_id,case_version,params_hash FROM ticket_operations").fetchone()
                assert op[0] == "unknown" and op[1] == confirmed.request_id and op[2] and op[3]
                assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
            service = LogisticsService()  # Restart the local process and lose the chat cache.
            resolved = service.process(request("继续"))
            assert resolved["case"]["state"] == "ticket_created"
            assert remote_count(remote_db) == 1
            with service.connect() as db:
                assert db.execute("SELECT state,external_id FROM ticket_operations").fetchone() == ("succeeded", resolved["case"]["ticket_id"])
        finally:
            process.terminate(); process.join(5)


def test_changed_case_during_remote_call_does_not_accept_old_result(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=False)
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(tmp_path / "cases.sqlite3"),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}):
        try:
            service = LogisticsService()
            first = service.process(request("包裹疑似丢件，订单号 DEMO1001"))
            assert first["case"]["state"] == "awaiting_confirmation"
            original = service.http_ticket_provider.create

            def create_then_change(key, params, params_hash):
                result = original(key, params, params_hash)
                with service.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT payload FROM cases WHERE id=?", (first["case"]["case_id"],)).fetchone()
                    case = json.loads(row[0])
                    case["order_id"] = "DEMO1002"
                    db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case), case["case_id"]))
                return result

            with patch.object(service.http_ticket_provider, "create", create_then_change):
                result = service.process(request("确认建单"))
            assert result["case"]["state"] == "ticket_unknown"
            assert result["case"].get("ticket_id") is None
            assert remote_count(remote_db) == 1
            with service.connect() as db:
                state, external_id = db.execute("SELECT state,external_id FROM ticket_operations").fetchone()
                assert state == "unknown" and external_id.startswith("HTTP-MOCK-")
        finally:
            process.terminate(); process.join(5)


def test_unavailable_provider_keeps_unknown_then_recovers(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=False)
    process.terminate(); process.join(5)
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(tmp_path / "cases.sqlite3"),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}):
        service = LogisticsService()
        first = request("包裹疑似丢件，订单号 DEMO1001")
        service.process(first)
        unknown = service.process(request("确认建单", first.conversation_state))
        assert unknown["case"]["state"] == "ticket_unknown"
        assert service.get_active_case("demo-user", "http-case")["state"] == "ticket_unknown"
        process = mp.Process(target=serve, args=("127.0.0.1", int(url.rsplit(":", 1)[1]), str(remote_db), False))
        process.start()
        try:
            for _ in range(50):
                try:
                    httpx.get(url + "/tickets/probe", timeout=.2, trust_env=False)
                    break
                except httpx.TransportError:
                    time.sleep(.05)
            resolved = service.process(request("继续", first.conversation_state))
            assert resolved["case"]["state"] == "ticket_created"
            assert remote_count(remote_db) == 1
        finally:
            process.terminate(); process.join(5)


async def _temporal_case(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=True)
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(tmp_path / "cases.sqlite3"),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url}):
        try:
            async with await WorkflowEnvironment.start_local() as env:
                with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                    async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                        import asyncio
                        task = asyncio.create_task(dispatch(env.client))
                        try:
                            first = await execute(request("包裹疑似丢件，订单号 DEMO1001", client_id="first"))
                            assert first["result"]["case"]["state"] == "awaiting_confirmation"
                            state = first["conversation_state"]
                            unknown = await execute(request("确认建单", state, client_id="confirm"))
                            assert unknown["result"]["case"]["state"] == "ticket_unknown"
                            resolved = await execute(request("继续", state, client_id="reconcile"))
                            assert resolved["result"]["case"]["state"] == "ticket_created"
                            assert remote_count(remote_db) == 1
                        finally:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
        finally:
            process.terminate(); process.join(5)


def test_temporal_path_uses_same_operation_ledger(tmp_path):
    import asyncio
    asyncio.run(_temporal_case(tmp_path))


async def _chat_case(tmp_path):
    remote_db = tmp_path / "remote.sqlite3"
    process, url = start_server(remote_db, drop=True)

    class Memory:
        def __init__(self):
            self.state = {}
            self.receipts = {}
            self.unsafe_markers = 0

        @asynccontextmanager
        async def conversation(self, *_):
            yield

        async def get_turn_receipt(self, user, conv, client):
            return self.receipts.get(client)

        async def get_context(self, *_args, **_kwargs):
            return SimpleNamespace(state=self.state.copy(), recent_messages=[], to_prompt_text=lambda: "")

        async def get_conversation_state(self, *_):
            return self.state.copy()

        async def commit_turn(self, user, conv, client, message, response, *_args, state=None):
            if response.get("error_code") == "tool_outcome_unknown":
                self.unsafe_markers += 1
            self.receipts[client] = {"message": message, "response": response}
            if state is not None:
                self.state = state.copy()

    class Orchestrator:
        def __init__(self):
            self.agent = object.__new__(LogisticsAgent)
            self.agent.service = LogisticsService()
            self._pool = {AgentType.LOGISTICS: [self.agent]}

        async def recognize_intent(self, *_args, **_kwargs):
            return SimpleNamespace(intent=IntentCategory.LOGISTICS_LOST, intent_group="logistics",
                                   urgency=None, entities={"order_id": ["DEMO1001"]}, confidence=1.0,
                                   source_scores={}, embedding_source="rules")

        async def run(self, req):
            output = await LogisticsAgent._call_llm(self.agent, req)
            return OrchestratorResult(request_id=req.request_id, response=output.content,
                                      agent_type=AgentType.LOGISTICS, intent=req.intent,
                                      agent_types=[AgentType.LOGISTICS], tools_used=output.tools_used)

        def get_tool_trace(self, *_):
            return {}

    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": str(tmp_path / "cases.sqlite3"),
                                 "DIAN_TICKET_PROVIDER": "http_mock", "DIAN_TICKET_URL": url,
                                 "DIAN_TEMPORAL_LOGISTICS": "false", "DIAN_ENV": "development"}):
        try:
            memory = Memory()
            orchestrator = Orchestrator()
            with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                    states = []
                    for message, client_id in [("包裹疑似丢件，订单号 DEMO1001", "first"),
                                               ("确认建单", "confirm"), ("继续", "reconcile")]:
                        response = await client.post("/chat", json={"message": message,
                            "user_id": "demo-user", "conv_id": "http-chat", "client_request_id": client_id})
                        assert response.status_code == 200, response.text
                        states.append(response.json()["logistics_case"]["state"])
                    assert states == ["awaiting_confirmation", "ticket_unknown", "ticket_created"]
                    assert memory.unsafe_markers == 0
                    assert remote_count(remote_db) == 1
        finally:
            process.terminate(); process.join(5)


def test_chat_route_reconciles_http_ticket(tmp_path):
    import asyncio
    asyncio.run(_chat_case(tmp_path))
