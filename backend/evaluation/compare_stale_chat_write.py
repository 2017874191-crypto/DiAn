"""Pause a real API process while an isolated Redis lease expires."""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import socket
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import psutil
import redis.asyncio as redis

import api.main as api
from core.concurrency import KeyedLocks
from evaluation.check_docker_redis_recovery import docker, start_redis, wait_redis
from evaluation.compare_temporal_logistics import ControlledOrchestrator
from memory.conversation_memory import MemoryManager

USER = "demo-user"


def memory_at(url):
    memory = object.__new__(MemoryManager)
    memory._redis = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
    memory._request_locks = KeyedLocks()

    async def context(user_id, conv_id, **_):
        return SimpleNamespace(state=await memory.get_conversation_state(user_id, conv_id),
                               recent_messages=[], to_prompt_text=lambda: "")

    memory.get_context = context
    return memory


async def chat(memory, orch, conv, request_id, order):
    with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orch):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://local") as client:
            response = await client.post("/chat", json={
                "user_id": USER, "conv_id": conv, "client_request_id": request_id,
                "message": f"包裹疑似丢件，订单号 {order}",
            })
    return response.status_code


def stale_worker(url, conv, checkpoint, ready, release, result):
    def pause():
        ready.set()
        if not release.wait(20):
            raise TimeoutError("parent did not release suspended request")

    async def run():
        memory = memory_at(url)
        orch = ControlledOrchestrator()
        if checkpoint == "before_sqlite":
            process = orch.agent.service.process

            def run_after_pause(req):
                pause()
                return process(req)

            orch.agent.service.process = run_after_pause
        else:
            commit = memory.commit_turn

            async def commit_after_pause(*args, **kwargs):
                await asyncio.to_thread(pause)
                return await commit(*args, **kwargs)

            memory.commit_turn = commit_after_pause
        try:
            result.put(await chat(memory, orch, conv, "old", "DEMO1001"))
        finally:
            await memory._redis.aclose()

    asyncio.run(run())


async def scenario(url, checkpoint):
    conv = f"suspend-{checkpoint}-{uuid.uuid4().hex}"
    memory = memory_at(url)
    orch = ControlledOrchestrator()
    spawn = mp.get_context("spawn")
    ready, release, result = spawn.Event(), spawn.Event(), spawn.Queue()
    child = spawn.Process(target=stale_worker, args=(url, conv, checkpoint, ready, release, result))
    child.start()
    process = psutil.Process(child.pid)
    suspended = False
    try:
        assert await asyncio.to_thread(ready.wait, 12), "old request did not reach its checkpoint"
        process.suspend()
        suspended = True
        key = "lock:conversation:" + memory._conversation_id(USER, conv)
        # Accelerate the fixture's TTL while A is suspended. Redis performs
        # the expiration itself, before A's normal renewal tick is due.
        assert await memory._redis.pexpire(key, 300)
        async with asyncio.timeout(3):
            while await memory._redis.exists(key):
                await asyncio.sleep(.02)
        assert await chat(memory, orch, conv, "new", "DEMO1002") == 200
        accepted = orch.agent.service.get_active_case(USER, conv)
        assert accepted["order_id"] == "DEMO1002"
        process.resume()
        suspended = False
        release.set()
        old_http = await asyncio.to_thread(result.get, True, 8)
        await asyncio.to_thread(child.join, 8)
        assert child.exitcode == 0
        final = orch.agent.service.get_active_case(USER, conv)
        state = await memory.get_conversation_state(USER, conv)
        old_receipt = await memory.get_turn_receipt(USER, conv, "old")
        return {"old_http": old_http, "new_http": 200, "case_order": final["order_id"],
                "case_pointer_preserved": final["case_id"] == accepted["case_id"],
                "redis_pointer_preserved": state["logistics_case_id"] == accepted["case_id"],
                "old_receipt_written": old_receipt is not None}
    finally:
        if suspended:
            process.resume()
        release.set()
        if child.is_alive():
            child.terminate()
            child.join(5)
        result.close()
        await memory._redis.aclose()


async def compare(url):
    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
    try:
        await wait_redis(client)
        info = await client.info("server")
    finally:
        await client.aclose()
    return {
        "redis_version": info["redis_version"],
        "before_sqlite": await scenario(url, "before_sqlite"),
        "before_redis_receipt": await scenario(url, "before_redis_receipt"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", choices=["baseline", "guarded"], default="guarded")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    name = "dian-stale-write-" + uuid.uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="dian-stale-write-") as temp, patch.dict(os.environ, {
        "DIAN_ENV": "development", "DIAN_API_TOKENS": "", "DIAN_TICKET_PROVIDER": "sqlite",
        "DIAN_TEMPORAL_LOGISTICS": "false", "DIAN_LOGISTICS_DB": str(Path(temp) / "cases.sqlite3"),
    }):
        start_redis(name, port)
        try:
            report = asyncio.run(compare(f"redis://127.0.0.1:{port}/0"))
        finally:
            docker("stop", name)
    output = json.dumps(report, indent=2)
    print(output)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    sql = report["before_sqlite"]
    receipt = report["before_redis_receipt"]
    if args.expect == "baseline":
        assert sql["old_http"] == 200 and sql["case_order"] == "DEMO1001"
        assert receipt["old_http"] == 200 and not receipt["redis_pointer_preserved"]
        assert sql["old_receipt_written"] and receipt["old_receipt_written"]
    else:
        assert sql["old_http"] == 409 and receipt["old_http"] == 503
        assert all(row["case_pointer_preserved"] and row["redis_pointer_preserved"]
                   and not row["old_receipt_written"] for row in (sql, receipt))


if __name__ == "__main__":
    main()
