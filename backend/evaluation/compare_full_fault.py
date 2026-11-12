"""Controlled Redis + independent HTTP ticket fault on both execution paths."""
import asyncio
import json
import logging
import multiprocessing as mp
import os
import socket
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import redis.asyncio as redis
from temporalio.testing import WorkflowEnvironment

import api.main as api
from core.concurrency import KeyedLocks
from core.logistics import LogisticsService
from core.mock_ticket_http import serve
from evaluation.check_docker_redis_recovery import docker, start_redis, wait_redis
from evaluation.compare_temporal_logistics import ControlledOrchestrator, worker_process
from memory.conversation_memory import MemoryManager

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def remote_count(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


async def variant(name, redis_url, redis_name, redis_port, root):
    case_db = str(Path(root) / f"{name}-cases.sqlite3")
    remote_db = str(Path(root) / f"{name}-remote.sqlite3")
    remote_port = free_port()
    remote_url = f"http://127.0.0.1:{remote_port}"
    remote = mp.Process(target=serve, args=("127.0.0.1", remote_port, remote_db, True))
    remote.start()
    redis_client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
    memory = object.__new__(MemoryManager)
    memory._redis = redis_client
    memory._request_locks = KeyedLocks()

    async def context(user_id, conv_id, query=""):
        return SimpleNamespace(state=await memory.get_conversation_state(user_id, conv_id),
                               recent_messages=[], to_prompt_text=lambda: "")

    memory.get_context = context
    conv = "full-fault-" + name + "-" + uuid.uuid4().hex
    enabled = name == "temporal"
    env_vars = {"DIAN_LOGISTICS_DB": case_db, "DIAN_TICKET_PROVIDER": "http_mock",
                "DIAN_TICKET_URL": remote_url, "DIAN_TEMPORAL_LOGISTICS": str(enabled).lower(),
                "DIAN_ENV": "development", "DIAN_API_TOKENS": ""}
    try:
        for _ in range(50):
            try:
                httpx.get(remote_url + "/tickets/probe", timeout=.2, trust_env=False)
                break
            except httpx.TransportError:
                await asyncio.sleep(.05)
        else:
            raise RuntimeError("remote mock did not start")
        with patch.dict(os.environ, env_vars):
            orchestrator = ControlledOrchestrator()
            orchestrator.issue = "logistics_lost"
            async def calls():
                original_commit = memory.commit_turn
                injected = False

                async def commit_with_outage(user, conversation, client_id, message, response, *args, state=None):
                    nonlocal injected
                    if client_id == "confirm" and state is not None and not injected:
                        injected = True
                        case = LogisticsService().get_active_case(user, conversation)
                        assert case and case["state"] == "ticket_unknown"
                        assert remote_count(remote_db) == 1
                        docker("stop", redis_name)  # The remote HTTP service has already committed.
                    return await original_commit(user, conversation, client_id, message, response, *args, state=state)

                with patch.object(memory, "commit_turn", commit_with_outage), \
                     patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), \
                     patch.object(api, "_profile_updater", None):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
                                                 base_url="http://test", timeout=55) as client:
                        first = await client.post("/chat", json={"message": "包裹疑似丢件，订单号 DEMO1001",
                            "user_id": "demo-user", "conv_id": conv, "client_request_id": "first"})
                        assert first.status_code == 200 and first.json()["logistics_case"]["state"] == "awaiting_confirmation"
                        confirm = {"message": "确认建单", "user_id": "demo-user", "conv_id": conv,
                                   "client_request_id": "confirm"}
                        started = time.perf_counter()
                        failed = await client.post("/chat", json=confirm)
                        failed_ms = round((time.perf_counter() - started) * 1000, 2)
                        assert failed.status_code == 503, failed.text
                        assert injected
                        assert remote_count(remote_db) == 1
                        start_redis(redis_name, redis_port)
                        await wait_redis(redis_client)
                        assert await redis_client.dbsize() == 0
                        started = time.perf_counter()
                        same = await client.post("/chat", json=confirm)
                        same_ms = round((time.perf_counter() - started) * 1000, 2)
                        assert same.status_code == 200, same.text
                        assert remote_count(remote_db) == 1
                        started = time.perf_counter()
                        next_turn = await client.post("/chat", json={"message": "继续", "user_id": "demo-user",
                            "conv_id": conv, "client_request_id": "reconcile"})
                        next_ms = round((time.perf_counter() - started) * 1000, 2)
                        assert next_turn.status_code == 200, next_turn.text
                        assert next_turn.json()["logistics_case"]["state"] == "ticket_created"
                        assert remote_count(remote_db) == 1
                        return {"first_state": first.json()["logistics_case"]["state"],
                                "fault_http": failed.status_code, "fault_latency_ms": failed_ms,
                                "same_id_state": same.json()["logistics_case"]["state"],
                                "same_id_latency_ms": same_ms,
                                "new_turn_state": next_turn.json()["logistics_case"]["state"],
                                "new_turn_latency_ms": next_ms,
                                "remote_tickets": remote_count(remote_db)}

            if enabled:
                async with await WorkflowEnvironment.start_local() as temporal:
                    with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                        stop = mp.Event()
                        worker = mp.Process(target=worker_process, args=(stop,))
                        worker.start()
                        try:
                            return await calls()
                        finally:
                            stop.set(); worker.join(15)
                            if worker.is_alive():
                                worker.terminate(); worker.join(5)
            return await calls()
    finally:
        await redis_client.aclose()
        if remote.is_alive():
            remote.terminate()
        remote.join(5)


async def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    redis_port = free_port()
    redis_name = "dian-full-fault-" + uuid.uuid4().hex[:10]
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    start_redis(redis_name, redis_port)
    try:
        with tempfile.TemporaryDirectory() as root:
            report = {"run_at_utc": datetime.now(timezone.utc).isoformat(),
                      "conditions": "real isolated Redis; separate mock HTTP ticket process; controlled routing; local SQLite case DB",
                      "variants": {}}
            for name in ("baseline", "temporal"):
                report["variants"][name] = await variant(name, redis_url, redis_name, redis_port, root)
                print(name, report["variants"][name], flush=True)
            assert all(item["remote_tickets"] == 1 and item["new_turn_state"] == "ticket_created"
                       for item in report["variants"].values())
            output = ROOT / "evaluation/full_fault_comparison.json"
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        try:
            docker("stop", redis_name)
        except Exception:
            pass


if __name__ == "__main__":
    mp.freeze_support()
    asyncio.run(main())
