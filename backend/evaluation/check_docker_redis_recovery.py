"""Opt-in real Redis /chat recovery and cross-process lock check in an owned container."""
import asyncio
import json
import multiprocessing as mp
import os
import socket
import subprocess
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import redis.asyncio as redis

import api.main as api
from core.concurrency import KeyedLocks
from evaluation.compare_temporal_logistics import ControlledOrchestrator
from memory.conversation_memory import MemoryManager


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def start_redis(name, port):
    docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine")


def lock_child(url, conv, ready, release):
    async def run():
        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        memory = object.__new__(MemoryManager)
        memory._redis = client
        memory._request_locks = KeyedLocks()
        try:
            async with memory.conversation("demo-user", conv):
                ready.set()
                if not await asyncio.to_thread(release.wait, 15):
                    raise TimeoutError("lock release timed out")
        finally:
            await client.aclose()

    asyncio.run(run())


async def wait_redis(client):
    for _ in range(50):
        try:
            if await client.ping():
                return
        except Exception:
            await asyncio.sleep(.1)
    raise RuntimeError("isolated Redis did not start")


async def scenario(url, name, port, db_path):
    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    memory = object.__new__(MemoryManager)
    memory._redis = client
    memory._request_locks = KeyedLocks()

    async def context(user_id, conv_id, query=""):
        return SimpleNamespace(state=await memory.get_conversation_state(user_id, conv_id),
                               recent_messages=[], to_prompt_text=lambda: "")

    memory.get_context = context
    conv = "real-redis-recovery-" + uuid.uuid4().hex
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": db_path, "DIAN_ENV": "development",
                                 "DIAN_API_TOKENS": "", "DIAN_TEMPORAL_LOGISTICS": "false",
                                 "DIAN_TICKET_PROVIDER": "sqlite"}):
        orchestrator = ControlledOrchestrator()
        orchestrator.issue = "logistics_lost"
        with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), \
             patch.object(api, "_profile_updater", None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
                                         base_url="http://test") as http:
                first = await http.post("/chat", json={"message": "包裹疑似丢件，订单号 DEMO1001",
                    "user_id": "demo-user", "conv_id": conv, "client_request_id": "first"})
                assert first.status_code == 200, first.text
                assert first.json()["logistics_case"]["state"] == "awaiting_confirmation"
                confirm = {"message": "确认建单", "user_id": "demo-user", "conv_id": conv,
                           "client_request_id": "confirm-after-redis-outage"}
                docker("stop", name)
                offline = await http.post("/chat", json=confirm)
                assert offline.status_code == 503, offline.text
                start_redis(name, port)  # A new ephemeral Redis has no old state or receipts.
                await wait_redis(client)
                recovered = await http.post("/chat", json=confirm)
                assert recovered.status_code == 200, recovered.text
                assert recovered.json()["logistics_case"]["state"] == "ticket_created"
                replay = await http.post("/chat", json=confirm)
                assert replay.status_code == 200
                assert replay.json()["logistics_case"]["ticket_id"] == recovered.json()["logistics_case"]["ticket_id"]
                with orchestrator.agent.service.connect() as db:
                    ticket_count = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                assert ticket_count == 1
        await client.aclose()
    return {"offline_http": offline.status_code, "recovered_http": recovered.status_code,
            "recovered_state": recovered.json()["logistics_case"]["state"], "ticket_count": ticket_count}


def lock_check(url):
    conv = "cross-process-lock-" + uuid.uuid4().hex
    first_ready, second_ready = mp.Event(), mp.Event()
    first_release, second_release = mp.Event(), mp.Event()
    first = mp.Process(target=lock_child, args=(url, conv, first_ready, first_release))
    second = mp.Process(target=lock_child, args=(url, conv, second_ready, second_release))
    try:
        first.start()
        assert first_ready.wait(15)
        second.start()
        assert not second_ready.wait(.3), "second process entered the same conversation lock"
        first_release.set()
        assert second_ready.wait(15)
        second_release.set()
        first.join(15); second.join(15)
        assert first.exitcode == second.exitcode == 0
        return {"second_blocked_until_release": True, "both_processes_exited": True}
    finally:
        first_release.set(); second_release.set()
        for child in (first, second):
            if child.is_alive():
                child.terminate(); child.join(5)


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    name = "dian-redis-recovery-" + uuid.uuid4().hex[:10]
    url = f"redis://127.0.0.1:{port}/0"
    start_redis(name, port)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(scenario(url, name, port, os.path.join(tmp, "cases.sqlite3")))
        result.update(lock_check(url))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        try:
            docker("stop", name)
        except subprocess.CalledProcessError:
            pass


if __name__ == "__main__":
    mp.freeze_support()
    main()
