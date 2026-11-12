"""Exercises the opt-in logistics route against a real local Temporal server."""
import asyncio
import json
import multiprocessing as mp
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from temporalio.testing import WorkflowEnvironment
from temporalio.service import RPCError
from temporalio.worker import Worker

from agents.agent_orchestrator import AgentType, LogisticsAgent, OrchestratorResult, Request
import api.main as api
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from core.temporal_logistics import QUEUE, LogisticsTurn, _db, _key, accept, dispatch, execute, process_logistics_turn


def accept_then_exit(db_path):
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": db_path}):
        req = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="accept-crash",
                      intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                      conversation_state={}, client_request_id="same-client-turn")
        accept(req)
        os._exit(88)


class TemporalLogisticsContract(unittest.IsolatedAsyncioTestCase):
    async def test_in_memory_server_restart_loses_pending_workflow_control(self):
        key = "dian-in-memory-control-" + os.urandom(8).hex()
        async with await WorkflowEnvironment.start_local() as first:
            await first.client.start_workflow(LogisticsTurn.run, {"message": "control"}, id=key, task_queue=QUEUE)
            self.assertEqual((await first.client.get_workflow_handle(key).describe()).id, key)
        async with await WorkflowEnvironment.start_local() as second:
            with self.assertRaises(RPCError):
                await second.client.get_workflow_handle(key).describe()

    async def test_persistent_dev_server_restart_preserves_dispatched_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3"}):
                req = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="restart",
                              intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                              conversation_state={}, client_request_id="restart-turn")
                key, _ = accept(req)
                temporal_db = tmp + "/temporal-dev.sqlite3"
                async with await WorkflowEnvironment.start_local(dev_server_database_filename=temporal_db) as first:
                    task = asyncio.create_task(dispatch(first.client))
                    try:
                        for _ in range(50):
                            run = LogisticsService().get_run(req.user_id, req.conv_id, LogisticsService.run_id(req))
                            if run["state"] == "dispatched":
                                break
                            await asyncio.sleep(.1)
                        self.assertEqual(run["state"], "dispatched")
                        self.assertEqual((await first.client.get_workflow_handle(key).describe()).id, key)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                # A new server process reads the same on-disk Temporal history.
                async with await WorkflowEnvironment.start_local(dev_server_database_filename=temporal_db) as second:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": second.client.service_client.config.target_host}):
                        async with Worker(second.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                            result = await execute(req)
                            self.assertEqual(result["result"]["case"]["state"], "awaiting_confirmation")
                            self.assertEqual(LogisticsService().get_run(req.user_id, req.conv_id,
                                                                        LogisticsService.run_id(req))["state"], "waiting_confirmation")

    async def test_legacy_pending_row_is_upgraded_on_same_id_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3"}):
                req = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="legacy",
                              intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                              conversation_state={}, client_request_id="legacy-client-id")
                key, payload = accept(req)
                payload.pop("run_id"); payload.pop("client_request_id")
                with _db() as db:
                    db.execute("UPDATE temporal_dispatch SET payload=? WHERE id=?", (json.dumps(payload), key))
                    db.execute("DELETE FROM logistics_steps")
                    db.execute("DELETE FROM logistics_runs")
                accept(req)
                with _db() as db:
                    restored = json.loads(db.execute("SELECT payload FROM temporal_dispatch WHERE id=?", (key,)).fetchone()[0])
                self.assertEqual(restored["run_id"], LogisticsService.run_id(req))
                self.assertEqual(restored["client_request_id"], req.client_request_id)
                self.assertEqual(LogisticsService().get_run(req.user_id, req.conv_id, restored["run_id"])["state"], "accepted")

    async def test_dispatched_run_is_queryable_before_worker_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3"}):
                req = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="dispatch-no-worker",
                              intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                              conversation_state={}, client_request_id="queued-turn")
                accept(req)
                async with await WorkflowEnvironment.start_local() as env:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                        task = asyncio.create_task(dispatch(env.client))
                        try:
                            for _ in range(50):
                                run = LogisticsService().get_run(req.user_id, req.conv_id, LogisticsService.run_id(req))
                                if run["state"] == "dispatched":
                                    break
                                await asyncio.sleep(.1)
                            self.assertEqual(run["state"], "dispatched")
                            self.assertEqual(run["steps"][0]["state"], "succeeded")
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                            output = await execute(req)
                            self.assertEqual(output["result"]["case"]["state"], "awaiting_confirmation")

    async def test_accept_and_run_are_atomic_across_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/cases.sqlite3"
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": path}):
                req = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="accept-crash",
                              intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                              conversation_state={}, client_request_id="same-client-turn")
                with patch.object(LogisticsService, "reserve_run", side_effect=RuntimeError("before commit")):
                    with self.assertRaisesRegex(RuntimeError, "before commit"):
                        accept(req)
                with _db() as db:
                    self.assertIsNone(db.execute("SELECT id FROM temporal_dispatch WHERE id=?", (_key(req),)).fetchone())
                self.assertIsNone(LogisticsService().get_run(req.user_id, req.conv_id, LogisticsService.run_id(req)))

                ablated = Request(message=req.message, user_id=req.user_id, conv_id="ablation",
                                  intent=req.intent, entities=req.entities, conversation_state={},
                                  client_request_id="ablate-run-reservation")
                with patch.object(LogisticsService, "reserve_run", return_value=None):
                    accept(ablated)
                with _db() as db:
                    self.assertEqual(db.execute("SELECT state FROM temporal_dispatch WHERE id=?", (_key(ablated),)).fetchone()[0], "pending")
                self.assertIsNone(LogisticsService().get_run(ablated.user_id, ablated.conv_id, LogisticsService.run_id(ablated)))
                accept(ablated)  # Retry repairs the ablated pending row without a second dispatch.
                self.assertEqual(LogisticsService().get_run(ablated.user_id, ablated.conv_id,
                                                            LogisticsService.run_id(ablated))["state"], "accepted")

                child = mp.Process(target=accept_then_exit, args=(path,))
                child.start(); child.join(10)
                self.assertEqual(child.exitcode, 88)
                queued = LogisticsService().get_run(req.user_id, req.conv_id, LogisticsService.run_id(req))
                self.assertEqual(queued["state"], "accepted")
                self.assertEqual(queued["steps"][0]["state"], "pending")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as http:
                    response = await http.get(f"/logistics/runs/{queued['run_id']}",
                                              params={"user_id": req.user_id, "conv_id": req.conv_id})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["state"], "accepted")
                with _db() as db:
                    self.assertEqual(db.execute("SELECT state FROM temporal_dispatch WHERE id=?", (_key(req),)).fetchone()[0], "pending")

                async with await WorkflowEnvironment.start_local() as env:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                        async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                            task = asyncio.create_task(dispatch(env.client))
                            try:
                                output = await execute(req)
                                self.assertEqual(output["result"]["case"]["state"], "awaiting_confirmation")
                                run = LogisticsService().get_run(req.user_id, req.conv_id, LogisticsService.run_id(req))
                                self.assertEqual(run["state"], "waiting_confirmation")
                                self.assertEqual(run["steps"][1]["state"], "succeeded")
                            finally:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task

    async def test_pending_row_dispatches_after_worker_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3"}):
                request = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="offline",
                                  intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                                  conversation_state={}, client_request_id="accepted-before-worker")
                key, _ = accept(request)  # Persisted while the worker is absent.
                async with await WorkflowEnvironment.start_local() as env:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                        async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                            task = asyncio.create_task(dispatch(env.client))
                            try:
                                result = await execute(request)
                                self.assertEqual(result["result"]["case"]["state"], "awaiting_confirmation")
                                self.assertEqual((await env.client.get_workflow_handle(key).describe()).id, key)
                            finally:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task

    async def test_chat_route_matches_baseline(self):
        class Memory:
            def __init__(self):
                self.state = {}
                self.receipts = {}

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
                if req.message == "确认建单":
                    assert req.conversation_state.get("logistics_case_id"), "durable case link was not restored"
                output = await LogisticsAgent._call_llm(self.agent, req)
                return OrchestratorResult(request_id=req.request_id, response=output.content,
                                          agent_type=AgentType.LOGISTICS, intent=req.intent,
                                          agent_types=[AgentType.LOGISTICS], tools_used=output.tools_used)

            def get_tool_trace(self, *_):
                return {}

        async def scenario(enabled):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3",
                                             "DIAN_TEMPORAL_LOGISTICS": str(enabled).lower(),
                                             "DIAN_ENV": "development"}):
                    memory = Memory()
                    orchestrator = Orchestrator()
                    async def calls():
                        with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                                results = []
                                for message, request_id in [("包裹疑似丢件，订单号 DEMO1001", "first"), ("确认建单", "second")]:
                                    response = await client.post("/chat", json={"message": message, "user_id": "demo-user", "conv_id": "chat-c1", "client_request_id": request_id})
                                    self.assertEqual(response.status_code, 200, response.text)
                                    results.append(response.json()["logistics_case"])
                                    if request_id == "first":
                                        memory.state = {}  # Simulate Redis expiry before confirmation.
                                return results
                    if enabled:
                        async with await WorkflowEnvironment.start_local() as env:
                            with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                                async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                                    task = asyncio.create_task(dispatch(env.client))
                                    try:
                                        return await calls()
                                    finally:
                                        task.cancel()
                                        with self.assertRaises(asyncio.CancelledError):
                                            await task
                    return await calls()

        baseline = await scenario(False)
        temporal = await scenario(True)
        self.assertEqual([item["state"] for item in baseline], [item["state"] for item in temporal])
        self.assertEqual([item["state"] for item in temporal], ["awaiting_confirmation", "ticket_created"])
        self.assertEqual(temporal[1]["ticket_id"], baseline[1]["ticket_id"])

    async def test_dispatch_and_confirmed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3"}):
                async with await WorkflowEnvironment.start_local() as env:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": env.client.service_client.config.target_host}):
                        async with Worker(env.client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
                            task = asyncio.create_task(dispatch(env.client))
                            try:
                                first = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id="demo-user", conv_id="c1",
                                                intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]},
                                                conversation_state={}, client_request_id="turn-1")
                                initial = await execute(first)
                                self.assertEqual(initial["result"]["case"]["state"], "awaiting_confirmation")
                                second = Request(message="确认建单", user_id="demo-user", conv_id="c1",
                                                 intent=IntentCategory.LOGISTICS_LOST, entities={},
                                                 conversation_state=initial["conversation_state"], client_request_id="turn-2")
                                key, _ = accept(second)
                                with self.assertRaisesRegex(ValueError, "client_request_id"):
                                    accept(Request(**{**second.__dict__, "message": "不同消息"}))
                                result = await execute(second)
                                self.assertEqual(result["result"]["case"]["state"], "ticket_created")
                                again = await execute(second)
                                self.assertEqual(again["result"]["case"]["ticket_id"], result["result"]["case"]["ticket_id"])
                                self.assertEqual(LogisticsService().get_case("demo-user", result["result"]["case"]["case_id"])["ticket_id"], result["result"]["case"]["ticket_id"])
                            finally:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task


if __name__ == "__main__":
    unittest.main()
