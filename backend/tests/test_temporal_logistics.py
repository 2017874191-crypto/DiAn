"""Exercises the opt-in logistics route against a real local Temporal server."""
import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agents.agent_orchestrator import AgentType, LogisticsAgent, OrchestratorResult, Request
import api.main as api
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from core.temporal_logistics import QUEUE, LogisticsTurn, accept, dispatch, execute, process_logistics_turn


class TemporalLogisticsContract(unittest.IsolatedAsyncioTestCase):
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
