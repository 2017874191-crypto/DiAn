"""Verify tracking via /chat with live, recorded-response or simulated-timeout transport."""
import argparse
import asyncio
import json
import logging
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx
from anthropic import AsyncAnthropic

import api.main as api
from agents import agent_orchestrator
from agents.tools import build_shared_rag_tools
from agents.tracking_tools import build_tracking_tools
from core import intent_recognizer
from core.concurrency import Capacity, KeyedLocks
from core.kdniao_tracking import KdniaoTrackingProvider
from core.skill_loader import SkillManager
from evaluation.compare_rag import model_settings
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256
from evaluation.replay_business_answers import EvaluationBM25
from memory.conversation_memory import MemoryManager
from dotenv import dotenv_values


class CountedTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0
        self.transport = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        self.calls += 1
        return self.transport.handle_request(request)

    def close(self):
        self.transport.close()

    @property
    def external_calls(self):
        return self.calls


class RecordedTransport(CountedTransport):
    def __init__(self, response):
        self.calls, self.response = 0, response

    def handle_request(self, request):
        self.calls += 1
        return httpx.Response(200, json=self.response, request=request)

    def close(self):
        pass

    @property
    def external_calls(self):
        return 0


class RecordedTrackingProvider(KdniaoTrackingProvider):
    def query_logistics(self, user_id, order_id):
        result = super().query_logistics(user_id, order_id)
        if result["success"]:
            result.update(source="user_supplied_kdniao_response", query_mode="response_replay",
                          query_performed=False, queried_at=None,
                          replayed_at=datetime.now(timezone.utc).isoformat(),
                          message="以下为用户提供的历史物流响应回放，非本次实时查询。签收记录不能证明本人实际收到。")
        return result


class TimeoutTransport(RecordedTransport):
    def __init__(self):
        super().__init__(None)

    def handle_request(self, request):
        self.calls += 1
        raise httpx.ReadTimeout("Simulated carrier read timeout", request=request)


class TimeoutTrackingProvider(KdniaoTrackingProvider):
    def query_logistics(self, user_id, order_id):
        result = super().query_logistics(user_id, order_id)
        if result["source"] == "kdniao":
            result.update(source="simulated_kdniao_timeout", query_mode="simulated_timeout",
                          query_performed=False, queried_at=None,
                          simulated_at=datetime.now(timezone.utc).isoformat())
            result["error"] = "本次为模拟超时验证，未向快递鸟发起实际请求。" + result["error"]
        return result


async def verify(output, response_fixture=None, simulate_timeout=False, selected_order_id=None):
    cfg = dotenv_values(ROOT / ".env")
    fixture = json.loads(response_fixture.read_text(encoding="utf-8")) if response_fixture else None
    orders = ([{"user_id": "recorded-owner", "order_id": "LIVE1001",
                "tracking_no": fixture["response"]["LogisticCode"] if fixture else "fixture-number"}]
              if fixture or simulate_timeout
              else json.loads(Path(cfg["KDNIAO_ORDER_BINDINGS"]).read_text(encoding="utf-8")))
    selected = (next((order for order in orders if order["order_id"] == selected_order_id), None)
                if selected_order_id else orders[0])
    if selected is None:
        raise ValueError("Selected order is not present in the local binding configuration")
    owner, order_id = selected["user_id"], selected["order_id"]
    settings, model_source = model_settings()
    transport = (TimeoutTransport() if simulate_timeout else
                 RecordedTransport(fixture["response"]) if fixture else CountedTransport())
    provider = (TimeoutTrackingProvider("fixture-id", "fixture-key", orders, transport=transport)
                if simulate_timeout else RecordedTrackingProvider("fixture-id", "fixture-key", orders, transport=transport)
                if fixture else KdniaoTrackingProvider(cfg["KDNIAO_EBUSINESS_ID"], cfg["KDNIAO_API_KEY"], orders,
                                                       transport=transport))
    _, corpus, _ = validate(ROOT / "evaluation/datasets/dian_rag_v1")
    owner_token, other_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    memory = MemoryManager.__new__(MemoryManager)
    memory._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    memory._request_locks = KeyedLocks()
    memory._query_episodic = AsyncMock(return_value={})
    memory._get_profile = AsyncMock(return_value={})
    result = {"schema": 1, "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
              "scope": ("Simulated carrier read timeout with live models through ASGI chat" if simulate_timeout else
                        "User-supplied KDniao response replay with live models through ASGI chat" if fixture
                        else "Live KDniao 8002 and live models via actual ASGI chat, local ownership binding"),
              "model": settings["model"], "model_configuration_source": model_source,
              "model_endpoint_sha256": sha256(settings["base_url"].encode()),
              "redis": "fakeredis", "business_database": "Temporary SQLite", "rows": [], "complete": False}
    if fixture:
        result["response_fixture"] = {"sha256": sha256(response_fixture.read_bytes()),
                                      "provenance": fixture["provenance"]}
    try:
        with tempfile.TemporaryDirectory(prefix="dian-kdniao-chat-") as folder:
            env = {"DIAN_ENV": "production", "DIAN_LOGISTICS_DB": str(Path(folder) / "cases.sqlite3"),
                   "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_TEMPORAL_LOGISTICS": "false",
                   "INTENT_EMBEDDING_PROVIDER": "local", "INTENT_RECOGNITION_MODE": "full",
                   "DIAN_API_TOKENS": json.dumps({owner_token: {"user_id": owner, "role": "user"},
                                                 other_token: {"user_id": "unbound-user", "role": "user"}})}
            async with AsyncAnthropic(
                api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
                http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=5)),
            ) as client:
                with (patch.dict(os.environ, env),
                      patch.object(agent_orchestrator, "AsyncAnthropic", return_value=client),
                      patch.object(intent_recognizer, "AsyncAnthropic", return_value=client)):
                    skills = SkillManager(str(ROOT / "skills"))
                    skills.load()
                    orch = agent_orchestrator.AgentOrchestrator(**settings, skill_manager=skills)
                    orch.set_shared_tools({**build_shared_rag_tools(EvaluationBM25(corpus)),
                                           **build_tracking_tools(provider)})
                    with (patch.object(api, "_orchestrator", orch), patch.object(api, "_memory", memory),
                          patch.object(api, "_profile_updater", None), patch.object(api, "_monitor", None),
                          patch.object(api, "_chat_capacity", Capacity(4, 4, 1))):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=api.app), base_url="http://isolated",
                        ) as http:
                            for label, token, conv in (
                                ("authorized", owner_token, "authorized"),
                                ("same_request_replay", owner_token, "authorized"),
                                ("unbound_subject", other_token, "other"),
                            ):
                                before = transport.calls
                                external_before = transport.external_calls
                                started = time.monotonic()
                                response = await http.post("/chat", headers={"Authorization": "Bearer " + token},
                                    json={"message": f"查询物流轨迹，订单号{order_id}", "user_id": owner,
                                          "conv_id": conv, "client_request_id": "read-tracking"})
                                payload = response.json()
                                result["rows"].append({
                                    "scenario": label, "http_status": response.status_code, "response": payload,
                                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                                    "external_calls": transport.external_calls - external_before,
                                    "provider_calls": transport.calls - before,
                                    "tool_trace": orch.get_tool_trace(payload.get("request_id", "")),
                                })
                                write_json(output, result)
                                print(label, response.status_code, "external_calls", transport.external_calls - external_before, flush=True)
                        with api._logistics_service().connect() as db:
                            result["ticket_count"] = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
    finally:
        await memory._redis.aclose()
    result["external_calls"] = transport.external_calls
    result["provider_calls"] = transport.calls
    authorized, replay, denied = result["rows"]
    result["checks"] = {
        "http_ok": all(row["http_status"] == 200 for row in result["rows"]),
        "one_authorized_provider_attempt": authorized["provider_calls"] == 1,
        "identical_receipt_replay": authorized["response"] == replay["response"] and replay["provider_calls"] == 0,
        "unbound_subject_rejected_locally": denied["provider_calls"] == 0 and
            (denied["response"].get("tracking_query") or {}).get("error_code") == "order_not_authorized",
        "no_tickets": result["ticket_count"] == 0,
    }
    if simulate_timeout:
        tracking = authorized["response"].get("tracking_query") or {}
        result["checks"].update(
            no_carrier_requests=result["external_calls"] == 0,
            simulated_timeout_returned=tracking.get("source") == "simulated_kdniao_timeout" and
                tracking.get("success") is False and tracking.get("error_code") == "tracking_timeout" and
                tracking.get("query_performed") is False and tracking.get("queried_at") is None,
        )
    result["complete"] = True
    write_json(output, result)
    if not all(result["checks"].values()):
        raise RuntimeError("Tracking chat verification failed; inspect report checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--order-id", help="Select an existing local order binding; defaults to the first binding")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--response-fixture", type=Path, help="Replay an explicitly sourced saved response, no carrier calls")
    modes.add_argument("--simulate-timeout", action="store_true", help="Inject a carrier read timeout, no carrier calls")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(verify(args.output, args.response_fixture, args.simulate_timeout, args.order_id))


if __name__ == "__main__":
    main()
