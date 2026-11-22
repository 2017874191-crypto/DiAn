"""Run the existing logistics assertions through isolated ASGI /chat and live models."""
import argparse
import asyncio
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx
from anthropic import AsyncAnthropic

import api.main as api
from agents import agent_orchestrator
from core import intent_recognizer
from core.concurrency import Capacity, KeyedLocks
from core.skill_loader import SkillManager
from evaluation.compare_rag import model_settings
from evaluation.logistics_eval_runner import (
    CONTRACTS, SUITE, close_fixture_case, decode_cases, load_cases, report, run_cases, verify_target,
)
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256
from evaluation.replay_business_answers import EvaluationBM25
from memory.conversation_memory import MemoryManager


class SynchronousASGIClient:
    """Let the existing synchronous runner use an AsyncClient on its owning loop."""
    def __init__(self, client, loop, result, output):
        self.client, self.loop = client, loop
        self.result, self.output = result, output

    def request(self, method, path, **kwargs):
        response = asyncio.run_coroutine_threadsafe(
            self.client.request(method, path, **kwargs), self.loop,
        ).result()
        if path == "/chat":
            self.result["http_exchanges"].append({"request": kwargs["json"],
                "http_status": response.status_code, "response": response.json()})
            write_json(self.output, self.result)
            print(len(self.result["http_exchanges"]), response.status_code,
                  kwargs["json"]["message"], flush=True)
        return response

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)


async def replay(output, cases_file=None, contract="legacy-v1"):
    dataset_bytes = (cases_file or SUITE / "test.jsonl").read_bytes()
    cases = decode_cases(dataset_bytes) if cases_file else load_cases("test")
    split = "development" if cases_file else "test"
    settings, configuration_source = model_settings()
    _, corpus, _ = validate(ROOT / "evaluation/datasets/dian_rag_v1")
    build = build_identity(ROOT)
    user_token, admin_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    result = {
        "schema": 1, "code_commit": git_commit(ROOT), "model": settings["model"],
        "case_source": cases_file.name if cases_file else "dian_logistics_v1/test.jsonl",
        "configuration_source": configuration_source, "complete": False,
        "scope": "Live models and actual ASGI /chat, frozen demo logistics, no external business calls",
        "dependencies": {"redis": "fakeredis protocol substitute", "business": "Temporary SQLite",
                         "memory": "Empty episodic/profile reads; existing conversation storage",
                         "retrieval": "Fixed nine-passage evaluation BM25 adapter"},
        "rag_corpus_sha256": sha256(json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()),
        "gold_status": "Codex-assisted initial review; independent human review pending",
        "http_exchanges": [],
    }
    memory = MemoryManager.__new__(MemoryManager)
    memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    memory._request_locks = KeyedLocks()
    memory._query_episodic = AsyncMock(return_value={})
    memory._get_profile = AsyncMock(return_value={})
    try:
        with tempfile.TemporaryDirectory(prefix="dian-logistics-candidate-") as folder:
            db = Path(folder) / "cases.sqlite3"
            env = {
                "DIAN_ENV": "production", "DIAN_LOGISTICS_DB": str(db),
                "DIAN_LOGISTICS_FIXTURES": str(ROOT / "config/logistics/fixtures.json"),
                "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_TEMPORAL_LOGISTICS": "false",
                "DIAN_TRACKING_PROVIDER": "mock", "MCP_EXTERNAL_ENABLED": "false",
                "INTENT_EMBEDDING_PROVIDER": "local", "INTENT_EMBEDDING_ENABLED": "true",
                "INTENT_RECOGNITION_MODE": "full", "INTENT_LOGISTICS_EMBEDDING_ENABLED": "false",
                "DIAN_API_TOKENS": json.dumps({user_token: {"user_id": "demo-user", "role": "user"},
                    admin_token: {"user_id": "candidate-review-admin", "role": "admin"}}),
                **{f"DIAN_{role}_MODEL": settings["model"]
                   for role in ("GENERAL", "TECHNICAL", "BILLING", "LOGISTICS", "ESCALATION")},
            }
            async with AsyncAnthropic(
                api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
                http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=5)),
            ) as model:
                with (patch.dict(os.environ, env),
                      patch.object(agent_orchestrator, "AsyncAnthropic", return_value=model),
                      patch.object(intent_recognizer, "AsyncAnthropic", return_value=model)):
                    skills = SkillManager(str(ROOT / "skills"))
                    skills.load()
                    orch = agent_orchestrator.AgentOrchestrator(
                        **settings, skill_manager=skills, rag_tool_manager=EvaluationBM25(corpus))
                    with (patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", memory),
                          patch.object(api, "_skill_manager", skills),
                          patch.object(api, "_profile_updater", None), patch.object(api, "_monitor", None),
                          patch.object(api, "_chat_capacity", Capacity(4, 4, 1)),
                          patch.object(api.app.state, "eval_runtime_build", build, create=True)):
                        async with (
                            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://isolated",
                                headers={"Authorization": "Bearer " + user_token}) as user,
                            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://isolated",
                                headers={"Authorization": "Bearer " + admin_token}) as admin,
                        ):
                            before = await admin.get("/eval/runtime")
                            before.raise_for_status()
                            target = {"runtime": before.json()["identity"], "image_id": None}
                            verify_target(target, cases, "full")
                            result["provenance"] = {
                                "schema": 1, "dataset_sha256": sha256(dataset_bytes),
                                "case_ids": sorted(case["case_id"] for case in cases),
                                "runner": {**build, "git_commit": git_commit(ROOT)}, "target": target,
                                "controlled_restart": False, "verification_errors": [],
                            }
                            loop = asyncio.get_running_loop()
                            sync_user = SynchronousASGIClient(user, loop, result, output)
                            sync_admin = SynchronousASGIClient(admin, loop, result, output)
                            results = await asyncio.to_thread(
                                run_cases, cases, sync_user, db,
                                close_case=lambda case_id, user_id, conv_id: close_fixture_case(
                                    sync_admin, case_id, user_id, conv_id), trace_client=sync_admin, contract=contract)
                            after = await admin.get("/eval/runtime")
                            after.raise_for_status()
                            if after.json()["identity"] != target["runtime"]:
                                result["provenance"]["verification_errors"].append("target_identity_changed")
                            result.update(report(results, split, "full", contract=contract))
                            with api._logistics_service().connect() as connection:
                                result["total_simulated_tickets"] = connection.execute("SELECT count(*) FROM tickets").fetchone()[0]
    finally:
        await memory._redis.aclose()
    result["complete"] = True
    write_json(output, result)
    print(json.dumps(result["summary"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases-file", type=Path, help="Explicit development cases; original test split is unchanged")
    parser.add_argument("--contract", choices=CONTRACTS, default="legacy-v1")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(replay(args.output, args.cases_file, args.contract))


if __name__ == "__main__":
    main()
