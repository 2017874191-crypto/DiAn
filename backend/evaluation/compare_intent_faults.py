"""Replay the A1 failure samples with model connection errors at fixed turn/stage coordinates."""
import argparse
import asyncio
import json
import os
import tempfile
from collections import Counter
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
from evaluation.logistics_eval_runner import ROOT, SUITE, close_fixture_case, load_cases, run_cases
from evaluation.provenance import build_identity, git_commit, runtime_snapshot, sha256
from memory.conversation_memory import MemoryManager

LABELS = {
    "DIAN-019": "order_status", "DIAN-020": "order_status",
    "DIAN-023": "billing", "DIAN-024": "billing",
    "DIAN-025": "logistics_lost", "DIAN-026": "logistics_lost",
}
VARIANTS = {"full": ("full", True), "llm_rules": ("llm_rules", True), "full_off": ("full", False)}
PROFILES = {"healthy": (), "intent_disconnect": ("intent",), "model_disconnect": ("intent", "reply")}
USER_TOKEN = "isolated-fault-user-token-local"
ADMIN_TOKEN = "isolated-fault-admin-token-local"


class ScriptedModel:
    def __init__(self, intent_label, stages, fault_turns):
        self.intent_label = intent_label
        self.stages = stages
        self.fault_turns = fault_turns
        self.turn = -1
        self.attempts = Counter()

    def transport(self, stage):
        def handle(request):
            disconnected = self.turn in self.fault_turns and stage in self.stages
            self.attempts[(self.turn, stage, "connect_error" if disconnected else "success")] += 1
            if disconnected:
                raise httpx.ConnectError("scripted connection failure", request=request)
            text = (json.dumps({"intent": self.intent_label, "confidence": .97})
                    if stage == "intent" else "这是模拟评测回复，请核对订单信息。")
            return httpx.Response(200, json={
                "id": "msg_fault_fixture", "type": "message", "role": "assistant", "model": "fault-fixture",
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        return httpx.MockTransport(handle)


class ReplayClient:
    """Run the existing synchronous replay driver against the actual ASGI app."""
    def __init__(self, loop, http, model, cleanup_fault):
        self.loop, self.http, self.model = loop, http, model
        self.cleanup_fault = cleanup_fault
        self.cleanup_attempts = 0
        self.committed_closures = []

    def get(self, path, **kwargs):
        token = USER_TOKEN if path == "/logistics/case" else ADMIN_TOKEN
        headers = {"Authorization": f"Bearer {token}"}
        return self._request("GET", path, headers=headers, **kwargs)

    def post(self, path, **kwargs):
        closing = path.endswith("/close")
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN if closing else USER_TOKEN}"}
        if closing:
            self.cleanup_attempts += 1
            if self.cleanup_fault in {"before", "persistent"} and (
                    self.cleanup_attempts == 1 or self.cleanup_fault == "persistent"):
                raise httpx.ReadError("scripted lost cleanup connection before commit")
        elif path == "/chat":
            self.model.turn += 1
        response = self._request("POST", path, headers=headers, **kwargs)
        if closing and response.is_success:
            self.committed_closures.append(response.json()["closed_at"])
            if self.cleanup_fault == "after" and self.cleanup_attempts == 1:
                raise httpx.ReadError("scripted lost cleanup response after commit")
        return response

    def _request(self, method, path, **kwargs):
        return asyncio.run_coroutine_threadsafe(self.http.request(method, path, **kwargs), self.loop).result(15)


def _single_close(client, case_id, user_id, conv_id):
    client.post(f"/logistics/cases/{case_id}/close", json={
        "user_id": user_id, "conv_id": conv_id, "reason": "Isolated logistics evaluation case completed",
    }).raise_for_status()


async def replay(case, mode, embedding, stages, build, *, fault_turns=(0,),
                 cleanup_fault=None, close=close_fixture_case, intent_label=None, configure_memory=None):
    with tempfile.TemporaryDirectory(prefix="dian-intent-fault-") as folder:
        path = Path(folder) / "cases.sqlite3"
        settings = {
            "DIAN_ENV": "development", "DIAN_LOGISTICS_DB": str(path),
            "DIAN_LOGISTICS_FIXTURES": str(ROOT / "config/logistics/fixtures.json"),
            "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_TEMPORAL_LOGISTICS": "false",
            "MCP_EXTERNAL_ENABLED": "false", "INTENT_EMBEDDING_PROVIDER": "local",
            "INTENT_EMBEDDING_ENABLED": "true", "INTENT_RECOGNITION_MODE": mode,
            "INTENT_LOGISTICS_EMBEDDING_ENABLED": str(embedding).lower(),
            "DIAN_API_TOKENS": json.dumps({
                USER_TOKEN: {"role": "user", "user_id": case["setup"]["user_id"]},
                ADMIN_TOKEN: {"role": "admin", "user_id": "local-fault-admin"},
            }),
        }
        model = ScriptedModel(intent_label or LABELS[case["case_id"]], stages, fault_turns)
        skills = SkillManager(str(ROOT / "skills"))
        skills.load()
        memory = MemoryManager.__new__(MemoryManager)
        memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        memory._request_locks = KeyedLocks()
        memory._query_episodic = AsyncMock(return_value={})
        memory._get_profile = AsyncMock(return_value={})
        try:
            if configure_memory is not None:
                configure_memory(memory)
            async with (
                AsyncAnthropic(api_key="local-fixture-only", base_url="http://model.fixture", max_retries=0,
                               http_client=httpx.AsyncClient(transport=model.transport("intent"))) as intent_client,
                AsyncAnthropic(api_key="local-fixture-only", base_url="http://model.fixture", max_retries=0,
                               http_client=httpx.AsyncClient(transport=model.transport("reply"))) as reply_client,
                httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as http,
            ):
                with (patch.dict(os.environ, settings),
                      patch.object(intent_recognizer, "AsyncAnthropic", return_value=intent_client),
                      patch.object(agent_orchestrator, "AsyncAnthropic", return_value=reply_client)):
                    orch = agent_orchestrator.AgentOrchestrator("local-fixture-only", model="fault-fixture",
                                                               skill_manager=skills)
                    with (patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", memory),
                          patch.object(api, "_profile_updater", None), patch.object(api, "_monitor", None),
                          patch.object(api, "_chat_capacity", Capacity(4, 4, 1))):
                        service = api._logistics_service()
                        client = ReplayClient(asyncio.get_running_loop(), http, model, cleanup_fault)
                        [result] = await asyncio.to_thread(run_cases, [case], client, path,
                                                           close_case=lambda *args: close(client, *args))
                        runtime = runtime_snapshot(build, orch, service, skills)["identity"]
                        with service.connect() as db:
                            ticket_count = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
                            closed = db.execute("SELECT count(*) FROM cases WHERE json_extract(payload,'$.closed_at') IS NOT NULL").fetchone()[0]
                            cases_created = db.execute("SELECT count(*) FROM cases").fetchone()[0]
        finally:
            await memory._redis.aclose()
    turns = []
    for turn in result["turns"]:
        obs = turn.get("observed") or {}
        turns.append({
            "turn": turn["turn"], "status": turn["status"], "failed_checks": turn.get("failures", []),
            "http_status": obs.get("http_status") or (200 if "response" in obs else None),
            "intent": obs.get("intent"), "agent": obs.get("agent_type"), "case_state": obs.get("case_state"),
            "routing_reason": obs.get("routing_reason"),
            "embedding_source": obs.get("intent_embedding_source"), "ticket_count": obs.get("ticket_count"),
            "unexpected_tickets": obs.get("unexpected_ticket_count"),
            "chat_wall_ms": obs.get("wall_latency_ms"),
        })
    return {
        "runtime": runtime,
        "status": result["status"], "turns": turns, "cases_created": cases_created, "ticket_count": ticket_count,
        "model_attempts": [{"turn": turn, "stage": stage, "outcome": outcome, "count": count}
                           for (turn, stage, outcome), count in sorted(model.attempts.items())],
        "cleanup": {"attempts": client.cleanup_attempts, "closed_cases": closed,
                    "committed_responses": len(client.committed_closures),
                    "distinct_closed_at": len(set(client.committed_closures)),
                    "runner_observations": result.get("fixture_cleanup", [])},
    }


async def compare():
    cases = {case["case_id"]: case for case in load_cases("dev")}
    build = build_identity(ROOT)
    rows = {}
    common = None
    for variant, (mode, embedding) in VARIANTS.items():
        rows[variant] = {}
        for profile, stages in PROFILES.items():
            group = {}
            for case_id in sorted(LABELS.keys() - {"DIAN-026"}):
                row = await replay(cases[case_id], mode, embedding, stages, build)
                identity = row.pop("runtime")
                assert identity["configuration"].pop("intent_mode") == mode
                assert identity["configuration"].pop("logistics_embedding") == embedding
                if common is None:
                    common = identity
                assert identity == common
                group[case_id] = row
            rows[variant][profile] = dict(sorted(group.items()))
    cleanup = {}
    for fault in ("before", "after", "persistent"):
        cleanup[fault] = {}
        for name, close in (("single_attempt", _single_close), ("bounded_retry", close_fixture_case)):
            row = await replay(cases["DIAN-026"], "full", True, (), build, cleanup_fault=fault, close=close)
            cleanup[fault][name] = {key: row[key] for key in ("status", "ticket_count", "cleanup")}
    return {
        "execution": "ASGI /chat, SDK connect-error transport, Redis substitute, temp SQLite; no live model",
        "dataset_sha256": sha256((SUITE / "dev.jsonl").read_bytes()),
        "runner_commit": git_commit(ROOT), "runtime": common,
        "fault_schedule": {name: {"turn": 0, "disconnect_stages": list(stages)} for name, stages in PROFILES.items()},
        "scenarios": rows, "cleanup_disconnects": cleanup,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = asyncio.run(compare())
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for variant, profiles in data["scenarios"].items():
        print(variant, {profile: sum(row["status"] == "flow_pass_semantic_pending" for row in cases.values())
                        for profile, cases in profiles.items()})
    print({fault: {name: row["status"] for name, row in variants.items()}
           for fault, variants in data["cleanup_disconnects"].items()})


if __name__ == "__main__":
    main()
