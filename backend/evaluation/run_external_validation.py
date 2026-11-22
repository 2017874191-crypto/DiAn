"""Replay external public tasks with capability-limited fixtures, never private labels."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx
from anthropic import AsyncAnthropic

import api.main as api
from agents import agent_orchestrator, tracking_tools
from core import intent_recognizer
from core.concurrency import Capacity, KeyedLocks
from core.logistics import is_confirmed
from core.logistics_providers import DeclarationEvidenceProvider
from core.skill_loader import SkillManager
from evaluation.compare_rag import model_settings
from evaluation.prepare_rag_data import ROOT, write_json
from evaluation.provenance import build_identity, git_commit
from memory.conversation_memory import MemoryManager

PINNED = "edfd318583399cae4f2a061e43d56ce4e0602027"
RUNTIME_DIRS = ("agents", "api", "core", "memory", "skills", "tooling", "config/logistics")


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_runtime():
    diff = subprocess.check_output(["git", "diff", PINNED, "--", *RUNTIME_DIRS], cwd=ROOT)
    if diff.strip():
        raise RuntimeError("Pinned production source changed; do not run external validation")


def public_corpus():
    # Public A-E rules from the authoring prompt; no test labels or case hints.
    policies = json.loads((ROOT / "config/logistics/policies.json").read_text(encoding="utf-8"))
    existing = json.loads((ROOT / "evaluation/datasets/dian_rag_v1/corpus.json").read_text(encoding="utf-8"))
    texts = [
        ("破损错发举证演示规则", policies["logistics_damaged"]["action"] +
         "需要外包装照片、面单照片、商品问题照片。只记录用户材料声明，不验证图片真实性。"),
        ("疑似丢件核查演示规则", policies["logistics_lost"]["action"]),
        ("签收争议演示规则", policies["logistics_dispute"]["action"]),
        ("退款政策", next(doc["text"] for doc in existing if doc["title"] == "退款政策")),
        ("配送与地址演示规则", "标准配送3—5个工作日，加急配送1—2个工作日，同城配送当日或次日。"
         "偏远地区可能额外增加2—3天，节假日可能延迟。修改收货地址需要在发货前联系客服。"
         "未提供发货后的改址政策，也未提供取消、拦截或拒收政策。"
         "一般配送时效不能直接决定某笔订单的实际预计送达时间。"),
    ]
    return [{"id": hashlib.sha256(text.encode()).hexdigest(), "title": title, "text": text,
             "version": "demo-policy-2026-11", "chunk": 0} for title, text in texts]


class FixturePorts:
    def __init__(self, task, fixture, user):
        self.caps = task["available_capabilities"]
        self.world, self.user = fixture["initial_world_state"], user
        self.results = {item["capability"]: item for item in fixture["authoritative_tool_results"]}
        self.events = []
        self.evidence = DeclarationEvidenceProvider()

    def enabled(self, name):
        return self.caps.get(name, {}).get("enabled") is True

    def event(self, operation, result):
        self.events.append({"operation": operation, "result": result})
        return result

    def owned(self, user, order):
        return (user == self.user and order == self.world.get("order_id")
                and self.world.get("actual_owner") == user)

    def query_order(self, user_id, order_id=None, tracking_no=None):
        permitted = self.enabled("order_ownership_verify") or self.enabled("order_logistics_read")
        allowed = permitted and self.owned(user_id, order_id)
        result = ({"success": True, "source": "mock", "order": {
            "order_id": order_id, "tracking_no": "SIM-" + order_id}}
            if allowed else {"success": False, "source": "mock",
                            "error": "订单归属未通过或本案未开放该查询能力"})
        return self.event("order_ownership_verify", result)

    def query_logistics(self, user_id, order_id):
        if not self.owned(user_id, order_id):
            return self.event("tracking_denied", {"success": False, "source": "mock",
                                                  "error": "订单归属未通过，未读取业务记录"})
        if self.enabled("order_logistics_read"):
            raw = self.results["order_logistics_read"]["result"]
            if not isinstance(raw, dict):
                raise ValueError("Missing declared tracking fixture")
            tracking = {**raw, "status": raw["logistics_status"], "events": [{
                "at": raw["latest_scan_at"], "description": raw["latest_scan_description"]}]}
            tracking["latest"] = tracking["events"][0]
            result = {"success": True, "source": "mock", "query_mode": "simulated_snapshot",
                      "query_performed": False, "queried_at": None,
                      "snapshot_as_of": raw["snapshot_as_of"], "tracking": tracking,
                      "message": "模拟接口固定快照，非本次真实供应商查询。"}
            return self.event("order_logistics_read", result)
        if self.enabled("owned_order_signature_read"):
            raw = self.results["owned_order_signature_read"]["result_if_ownership_verified"]
            record = str(raw["signature_record"])
            if record.lower() in {"signed", "delivered", "exists"} or (
                    "签收" in record and not re.search(r"未|没有|不存在|无", record)):
                status = "delivered"
            else:
                # Preserve uncertainty; never invent a delivered fact from an unknown enum.
                status = "unmapped_signature_record"
            return self.event("owned_order_signature_read", {"success": True, "source": "mock",
                "tracking": {**raw, "status": status, "order_id": order_id}})
        return self.event("tracking_capability_unavailable", {"success": False, "source": "mock",
                          "error": "本案未开放物流或签收记录读取能力"})

    def declared(self, message, previous):
        result = self.enabled("material_declaration_record") and self.evidence.declared(message, previous)
        self.event("material_declaration_record", {"recorded": bool(result)})
        return result

    def create_ticket(self, db, case_id, idempotency_key):
        if (not self.enabled("mock_ticket_create") or idempotency_key != case_id
                or not self.owned(self.user, self.world.get("order_id"))):
            raise ValueError("Ticket capability or operation key rejected")
        spec = self.results["mock_ticket_create"]
        raw = spec.get("result_if_preconditions_fulfilled") or spec.get("result_if_ownership_verified_and_informed_confirmation")
        if not raw or raw.get("created") is not True:
            raise ValueError("Missing successful mock-ticket fixture")
        db.execute("INSERT OR IGNORE INTO tickets VALUES (?,?)", (case_id, raw["ticket_id"]))
        return self.event("mock_ticket_create", {"success": True, "source": "mock", "ticket_id": raw["ticket_id"]})


def next_turn_allowed(script_index, reply, ticket_count):
    if script_index == 0:
        case = reply.get("logistics_case") or {}
        text = reply.get("response", "")
        return (case.get("state") == "awaiting_confirmation" and "确认" in text
                and ("模拟" in text or "人工审核" in text or "核查" in text))
    if script_index == 1:
        return ticket_count == 1 and (reply.get("logistics_case") or {}).get("state") == "ticket_created"
    raise ValueError("Unregistered conversation trigger")


def chat_payload(message, user, conversation):
    return {"message": message, "user_id": user, "conv_id": conversation,
            "client_request_id": uuid.uuid4().hex}


async def run(args):
    # Retrieval is an optional live-evaluation dependency, not needed by adapter tests.
    from evaluation.replay_business_answers import EvaluationBM25

    check_runtime()
    intake = json.loads(args.manifest.read_text(encoding="utf-8"))
    if (intake["candidate_commit"] != PINNED or
            intake["source_files"][args.inputs.name]["sha256"] != digest(args.inputs) or
            intake["extracted_tool_fixtures"]["sha256"] != digest(args.fixtures)):
        raise ValueError("Input, fixture or candidate identity differs from intake")
    if args.output.exists():
        raise FileExistsError("Report already exists; preserve the original run")
    tasks = read_rows(args.inputs)
    fixtures = {row["case_id"]: row for row in read_rows(args.fixtures)}
    assert len(tasks) == len(fixtures) == 10 and {t["case_id"] for t in tasks} == set(fixtures)
    settings, model_source = model_settings()
    corpus = public_corpus()
    report = {"schema": 1, "candidate_commit": PINNED, "runner_commit": git_commit(ROOT),
              "build": build_identity(ROOT), "model": settings["model"], "model_source": model_source,
              "inputs_sha256": digest(args.inputs), "fixtures_sha256": digest(args.fixtures),
              "public_corpus": corpus, "scope": "Context-isolated model-authored validation draft, actual ASGI chat, simulated business",
              "private_labels_loaded": False, "real_business_calls": 0, "cases": [], "complete": False,
              "projection": "Only original user messages, declared tools and legitimate tool results. No P/N IDs, scene hints, script triggers or answer metadata sent to agent.",
              "trigger_policy": "Pending confirmation state plus explicit request in the anchored single-case conversation; duplicate only after observed ticket creation. No confirmation-text rewriting.",
              "dependencies": {"redis": "fakeredis", "database": "per-case temporary SQLite", "retrieval": "five public authoring rules, evaluation BM25"}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve this attempt before any model call; partial files are deliberately retained.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False)
    async with AsyncAnthropic(api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=5))) as client:
        for task in tasks:
            opaque = uuid.uuid4().hex
            user = task["observable_context"].get("simulated_session_account", "validation-" + opaque)
            ports = FixturePorts(task, fixtures[task["case_id"]], user)
            token = secrets.token_urlsafe(32)
            memory = MemoryManager.__new__(MemoryManager)
            memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            memory._request_locks = KeyedLocks()
            memory._query_episodic = AsyncMock(return_value={})
            memory._get_profile = AsyncMock(return_value={})
            row = {"case_id": task["case_id"], "turns": [], "skipped_script_turns": []}
            report["cases"].append(row)
            try:
                with tempfile.TemporaryDirectory(prefix="dian-external-validation-") as folder:
                    env = {"DIAN_ENV": "production", "DIAN_LOGISTICS_DB": str(Path(folder) / "cases.db"),
                        "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_TEMPORAL_LOGISTICS": "false",
                        "DIAN_TRACKING_PROVIDER": "mock", "MCP_EXTERNAL_ENABLED": "false",
                        "INTENT_RECOGNITION_MODE": "full", "INTENT_EMBEDDING_PROVIDER": "local",
                        "INTENT_EMBEDDING_ENABLED": "true", "INTENT_LOGISTICS_EMBEDDING_ENABLED": "false",
                        "DIAN_API_TOKENS": json.dumps({token: {"user_id": user, "role": "user"}}),
                        **{f"DIAN_{role}_MODEL": settings["model"] for role in ("GENERAL", "BILLING", "TECHNICAL", "LOGISTICS", "ESCALATION")}}
                    with (patch.dict(os.environ, env),
                          patch.object(agent_orchestrator, "AsyncAnthropic", return_value=client),
                          patch.object(intent_recognizer, "AsyncAnthropic", return_value=client)):
                        skills = SkillManager(str(ROOT / "skills")); skills.load()
                        orch = agent_orchestrator.AgentOrchestrator(**settings, skill_manager=skills,
                                                                  rag_tool_manager=EvaluationBM25(corpus))
                        if ports.enabled("order_logistics_read"):
                            orch.set_shared_tools({**orch._shared_tools, **tracking_tools.build_tracking_tools(ports)})
                        service = orch._pool[agent_orchestrator.AgentType.LOGISTICS][0].service
                        service.order_provider = service.tracking_provider = ports
                        service.evidence_provider = service.ticket_provider = ports
                        with (patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", memory),
                              patch.object(api, "_profile_updater", None), patch.object(api, "_monitor", None),
                              patch.object(api, "_chat_capacity", Capacity(4, 4, 1))):
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                base_url="http://isolated", headers={"Authorization": "Bearer " + token}) as http:
                                messages = [task["initial_user_input"]] + [s["user_input"] for s in task["conversation_script"]]
                                count, reply = 0, {}
                                for index, message in enumerate(messages):
                                    if index and not next_turn_allowed(index - 1, reply, count):
                                        row["skipped_script_turns"] = list(range(index, len(messages)))
                                        break
                                    response = await http.post("/chat", json=chat_payload(message, user, opaque))
                                    reply = response.json()
                                    with service.connect() as db:
                                        count = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
                                    row["turns"].append({"user_message": message, "http_status": response.status_code,
                                        "response": reply, "ticket_count": count,
                                        "tool_trace": orch.get_tool_trace(reply.get("request_id", ""))})
                                    write_json(args.output, report)
                                    if response.status_code != 200:
                                        break
                        row["ticket_count"] = count
                        row["fixture_events"] = ports.events
                        row["rule_checks"] = {
                            "at_most_one_ticket": count <= 1,
                            "no_undeclared_ticket": ports.enabled("mock_ticket_create") or count == 0,
                            "no_ticket_without_exact_runtime_confirmation": count == 0 or any(
                                is_confirmed(t["user_message"]) for t in row["turns"][1:]),
                        }
                        row["profile_limitations"] = sorted({e["operation"] for e in ports.events
                            if e["operation"] == "tracking_capability_unavailable"})
                        row["signature_mapping_unresolved"] = any(
                            e["result"].get("tracking", {}).get("status") == "unmapped_signature_record" for e in ports.events)
            finally:
                await memory._redis.aclose()
            write_json(args.output, report)
            print("executed case", len(report["cases"]), "turns", len(row["turns"]), flush=True)
    check_runtime()
    report["complete"] = True
    write_json(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
