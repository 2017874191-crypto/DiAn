"""案件快照接口的身份与会话边界；不调用模型或外部服务。"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import api.main as api
from agents.agent_orchestrator import AgentType, Request
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService

class CaseAPIContract(unittest.IsolatedAsyncioTestCase):
    async def test_case_snapshot_respects_identity_and_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LogisticsService(tmp + "/cases.sqlite3")
            req = Request(message="包裹疑似丢件", user_id="demo-user", conv_id="owned", intent=IntentCategory.LOGISTICS_LOST,
                entities={"order_id":["DEMO1001"]}, conversation_state={})
            service.process(req)
            pointer_missing = False
            async def state(user, conv):
                if pointer_missing:
                    return {}
                return req.conversation_state if conv in ("owned", "wrong-pointer") else {}
            token = "test-token-" + "a" * 32
            env = {"DIAN_ENV":"production", "DIAN_API_TOKENS":json.dumps({token:{"user_id":"demo-user","role":"user"}})}
            orch = SimpleNamespace(_pool={AgentType.LOGISTICS:[SimpleNamespace(service=service)]})
            with patch.dict(os.environ, env), patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", SimpleNamespace(get_conversation_state=state)), patch.object(api, "_monitor", None):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url="http://test") as client:
                    headers={"Authorization":"Bearer " + token}
                    response=await client.get("/logistics/case",params={"user_id":"demo-user","conv_id":"owned"},headers=headers)
                    self.assertEqual(response.status_code,200)
                    snapshot=response.json()["logistics_case"]
                    self.assertTrue(snapshot["can_confirm"])
                    self.assertNotIn("events",snapshot)
                    pointer_missing = True  # Redis state expired; ownership remains in SQLite.
                    recovered=await client.get("/logistics/case",params={"user_id":"demo-user","conv_id":"owned"},headers=headers)
                    self.assertEqual(recovered.json()["logistics_case"],snapshot)
                    denied=await client.get("/logistics/case",params={"user_id":"another-user","conv_id":"owned"},headers=headers)
                    self.assertEqual(denied.status_code,403)
                    for conv in ("missing","wrong-pointer"):
                        empty=await client.get("/logistics/case",params={"user_id":"demo-user","conv_id":conv},headers=headers)
                        self.assertIsNone(empty.json()["logistics_case"])
                    unsigned=await client.get("/logistics/case",params={"user_id":"demo-user","conv_id":"owned"})
                    self.assertEqual(unsigned.status_code,401)

if __name__ == "__main__":
    unittest.main(argv=["case-contract"])
