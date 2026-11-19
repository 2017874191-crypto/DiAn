"""Exercise real /chat routing and composition with live models and isolated local dependencies."""
import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx
from anthropic import AsyncAnthropic

import api.main as api
from agents import agent_orchestrator
from agents.agent_orchestrator import AgentType, Request
from core import intent_recognizer
from core.concurrency import Capacity, KeyedLocks
from core.skill_loader import SkillManager
from core.llm_utils import model_scope
from core.performance import collect_performance, performance_snapshot
from evaluation.compare_rag import attach_transport_trace, model_settings
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256
from evaluation.replay_business_answers import EvaluationBM25
from memory.conversation_memory import MemoryManager

CASES = [
    ("state-list", "有哪些订单状态？"),
    ("signed-inference", "订单显示已签收，就能证明是我本人收到的吗？"),
    ("signed-conflict", "页面显示已签收，但我确实没拿到，这两件事是不是矛盾？"),
    ("signed-summary", "请解释“已发货、运输中、已签收”，尤其说明签收状态能否证明我本人收到了。"),
    ("billing-policy", "退款申请要审核多久？"),
    ("combined", "登录一直出现401，同时想知道退款申请审核多久？"),
    ("refund-live", "我刚提交的退款申请现在审核通过了吗？"),
    ("shipping-address", "订单已经发货，还能改地址吗？"),
]


async def replay(args):
    dataset = ROOT / "evaluation/datasets/dian_rag_v1"
    manifest, corpus, _queries = validate(dataset)
    settings, source = model_settings()
    result = {
        "schema": 1, "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
        "scope": "ASGI /chat, live intent and role models, production skills, evaluation-only BM25",
        "dataset_manifest": manifest, "manifest_sha256": sha256((dataset / "manifest.json").read_bytes()),
        "model": settings["model"], "configuration_source": source,
        "dependencies": {"redis": "fakeredis protocol substitute", "business": "Temporary SQLite with demo fixtures",
                         "background_memory": "Empty profile and episodic result",
                         "retrieval": "Fixed nine-passage BM25 evaluation adapter"},
        "complete": False, "rows": [],
    }
    with tempfile.TemporaryDirectory(prefix="dian-chat-evidence-") as folder:
        config = {
            "DIAN_ENV": "development", "DIAN_LOGISTICS_DB": str(Path(folder) / "cases.sqlite3"),
            "DIAN_LOGISTICS_FIXTURES": str(ROOT / "config/logistics/fixtures.json"),
            "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_TEMPORAL_LOGISTICS": "false",
            "DIAN_API_TOKENS": "", "MCP_EXTERNAL_ENABLED": "false",
            "INTENT_EMBEDDING_PROVIDER": "local", "INTENT_EMBEDDING_ENABLED": "true",
            "INTENT_RECOGNITION_MODE": "full", "INTENT_LOGISTICS_EMBEDDING_ENABLED": "true",
        }
        memory = MemoryManager.__new__(MemoryManager)
        memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        memory._request_locks = KeyedLocks()
        memory._query_episodic = AsyncMock(return_value={})
        memory._get_profile = AsyncMock(return_value={})
        skills = SkillManager(str(ROOT / "skills"))
        skills.load()
        search = EvaluationBM25(corpus)
        try:
            async with AsyncAnthropic(
                api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
                http_client=httpx.AsyncClient(
                    trust_env=False, timeout=httpx.Timeout(30, connect=5),
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
                    event_hooks={"request": [attach_transport_trace]}),
            ) as client:
                with (patch.dict(os.environ, config),
                      patch.object(agent_orchestrator, "AsyncAnthropic", return_value=client),
                      patch.object(intent_recognizer, "AsyncAnthropic", return_value=client)):
                    orch = agent_orchestrator.AgentOrchestrator(**settings, skill_manager=skills,
                                                               rag_tool_manager=search)
                    with (patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", memory),
                          patch.object(api, "_profile_updater", None), patch.object(api, "_monitor", None),
                          patch.object(api, "_chat_capacity", Capacity(4, 4, 1))):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=api.app), base_url="http://isolated",
                        ) as http:
                            for identity, message in CASES:
                                start = len(search.calls)
                                started = time.perf_counter()
                                response = await http.post("/chat", json={
                                    "message": message, "user_id": "chat-evidence-user",
                                    "conv_id": identity, "client_request_id": identity,
                                })
                                payload = response.json()
                                result["rows"].append({
                                    "case_id": identity, "message": message, "http_status": response.status_code,
                                    "response": payload, "wall_ms": round((time.perf_counter() - started) * 1000, 3),
                                    "retrievals": search.calls[start:],
                                    "tool_trace": orch.get_tool_trace(payload.get("request_id", "")),
                                })
                                write_json(args.output, result)
                                print(identity, response.status_code, payload.get("agent_types"), flush=True)
                            result["direct_general_checks"] = []
                            for identity, message in (
                                ("enum-explanation", "请列出并解释订单状态，已签收是什么意思？"),
                                ("record-versus-receipt", "系统有签收记录，是否足以证明我本人收到了？"),
                                ("preserve-user-statement", "系统显示已签收，但我本人没有拿到，能直接说我已经收货了吗？"),
                            ):
                                with collect_performance(), model_scope("evaluation", max_calls=3):
                                    answer = await orch._pool[AgentType.GENERAL][0].handle(
                                        Request(message=message, user_id="chat-evidence-user", conv_id=identity))
                                    result["direct_general_checks"].append({
                                        "case_id": identity, "message": message, "response": answer.content,
                                        "success": answer.success, "status": answer.status,
                                        "tools_used": answer.tools_used, "performance": performance_snapshot(),
                                    })
                        service = api._logistics_service()
                        with service.connect() as db:
                            result["ticket_count"] = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
        finally:
            await memory._redis.aclose()
    result["complete"] = True
    write_json(args.output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
