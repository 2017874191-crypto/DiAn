"""Opt-in integration check against a real Redis server; never uses a fake cache."""
import asyncio
import json
import os
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import redis.asyncio as redis

import api.main as api
from agents.agent_orchestrator import AgentType, Request
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from memory.conversation_memory import MemoryManager


async def run(url):
    conv = "real-redis-ttl-" + uuid.uuid4().hex
    user = "demo-user"
    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
    keys = []
    try:
        await client.ping()
        memory = object.__new__(MemoryManager)
        memory._redis = client
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "DIAN_LOGISTICS_DB": tmp + "/cases.sqlite3", "DIAN_ENV": "development",
                "DIAN_API_TOKENS": ""}):
            service = LogisticsService()
            keys = [memory.receipt_key(user, conv, "ttl-check"), memory._state_key(user, conv),
                    memory._wm_key(user, conv)]
            request = Request(message="包裹疑似丢件，订单号 DEMO1001", user_id=user, conv_id=conv,
                              intent=IntentCategory.LOGISTICS_LOST, entities={"order_id": ["DEMO1001"]})
            case = service.process(request)["case"]
            await memory.commit_turn(user, conv, "ttl-check", request.message,
                                     {"status": "success", "response": "ok", "request_id": "ttl-check"},
                                     state={"logistics_case_id": case["case_id"]})
            for key in keys:
                await client.expire(key, 2)
            assert await memory.get_turn_receipt(user, conv, "ttl-check") is not None
            assert (await memory.get_conversation_state(user, conv))["logistics_case_id"] == case["case_id"]
            await asyncio.sleep(2.3)
            assert await memory.get_turn_receipt(user, conv, "ttl-check") is None
            assert await memory.get_conversation_state(user, conv) == {}
            orchestrator = SimpleNamespace(_pool={AgentType.LOGISTICS: [SimpleNamespace(service=service)]})
            with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as http:
                    response = await http.get("/logistics/case", params={"user_id": user, "conv_id": conv})
            assert response.status_code == 200, response.text
            assert response.json()["logistics_case"]["case_id"] == case["case_id"]
            return {"real_redis": True, "receipt_expired": True, "state_expired": True,
                    "case_visible_after_ttl": True}
    finally:
        if keys:
            try:
                await client.delete(*keys)
            except Exception:
                pass
        await client.aclose()


if __name__ == "__main__":
    address = os.getenv("DIAN_REDIS_TEST_URL")
    if not address:
        raise SystemExit("Set DIAN_REDIS_TEST_URL to an isolated real Redis test instance")
    print(json.dumps(asyncio.run(run(address)), ensure_ascii=False))
