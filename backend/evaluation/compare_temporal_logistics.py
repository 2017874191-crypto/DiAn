"""Controlled /chat comparison: identical dataset turns, real Temporal, no paid model."""
import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import api.main as api
from agents.agent_orchestrator import AgentType, LogisticsAgent, OrchestratorResult
from core.intent_recognizer import IntentCategory
from core.logistics import LogisticsService
from core.temporal_logistics import QUEUE, LogisticsTurn, dispatch, process_logistics_turn

SELECTED = ("DIAN-007", "DIAN-008", "DIAN-013", "DIAN-014", "DIAN-033", "DIAN-034", "DIAN-049", "DIAN-050")
ROOT = Path(__file__).resolve().parents[1]


class Memory:
    def __init__(self):
        self.states = {}
        self.receipts = {}
        self.fail_next_final = False

    @asynccontextmanager
    async def conversation(self, *_):
        yield

    async def get_turn_receipt(self, user, conv, key):
        return self.receipts.get((user, conv, key))

    async def get_context(self, user, conv, **_):
        return SimpleNamespace(state=self.states.get((user, conv), {}).copy(), recent_messages=[], to_prompt_text=lambda: "")

    async def get_conversation_state(self, user, conv):
        return self.states.get((user, conv), {}).copy()

    async def commit_turn(self, user, conv, key, message, response, *_args, state=None):
        if state is not None and self.fail_next_final:
            self.fail_next_final = False
            raise RuntimeError("injected: final receipt lost")
        self.receipts[(user, conv, key)] = {"message": message, "response": response}
        if state is not None:
            self.states[(user, conv)] = state.copy()


class ControlledOrchestrator:
    """Keep routing/recognition constant so the execution mechanism is isolated."""
    def __init__(self):
        self.agent = object.__new__(LogisticsAgent)
        self.agent.service = LogisticsService()
        self._pool = {AgentType.LOGISTICS: [self.agent]}
        self.issue = "logistics_lost"

    async def recognize_intent(self, message, **_):
        match = re.search(r"DEMO\d{4}", message)
        return SimpleNamespace(intent=IntentCategory(self.issue), intent_group="logistics", urgency=None,
                               entities={"order_id": [match.group()] if match else []}, confidence=1.0,
                               source_scores={}, embedding_source="controlled")

    async def run(self, req):
        result = await LogisticsAgent._call_llm(self.agent, req)
        return OrchestratorResult(request_id=req.request_id, response=result.content,
                                  agent_type=AgentType.LOGISTICS, intent=req.intent,
                                  agent_types=[AgentType.LOGISTICS], tools_used=result.tools_used)

    def get_tool_trace(self, *_):
        return {}


def worker_process(stop_event, fault_marker=None):
    async def run():
        from temporalio.client import Client
        client = await Client.connect(os.environ["DIAN_TEMPORAL_ADDRESS"])
        functions = [process_logistics_turn]
        if fault_marker:
            @activity.defn(name="process_logistics_turn")
            async def crash_after_commit(payload: dict) -> dict:
                result = await process_logistics_turn(payload)
                if payload["message"] == "确认建单" and not Path(fault_marker).exists():
                    Path(fault_marker).write_text("business transaction committed", encoding="utf-8")
                    os._exit(86)
                return result
            functions = [crash_after_commit]
        async with Worker(client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=functions):
            task = asyncio.create_task(dispatch(client))
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(.2)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    asyncio.run(run())


def count(db_path, conv):
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute("SELECT COUNT(*) FROM tickets t JOIN cases c ON c.id=t.case_id WHERE c.conv_id=?", (conv,)).fetchone()[0]


async def receipt_fault(enabled, root):
    """Lose the final API receipt after a committed simulated ticket."""
    db_path = str(Path(root) / ("receipt-temporal.sqlite3" if enabled else "receipt-baseline.sqlite3"))
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": db_path, "DIAN_TEMPORAL_LOGISTICS": str(enabled).lower(), "DIAN_ENV": "development"}):
        memory, orchestrator = Memory(), ControlledOrchestrator()
        orchestrator.issue = "logistics_lost"
        async def requests():
            with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test", timeout=55) as client:
                    first = {"message": "包裹疑似丢件，订单号 DEMO1001", "user_id": "demo-user", "conv_id": "receipt-fault", "client_request_id": "first"}
                    second = {**first, "message": "确认建单", "client_request_id": "second"}
                    initial = await client.post("/chat", json=first)
                    assert initial.status_code == 200 and initial.json()["logistics_case"]["state"] == "awaiting_confirmation"
                    memory.fail_next_final = True
                    try:
                        await client.post("/chat", json=second)
                    except Exception:
                        pass
                    else:
                        raise AssertionError("receipt failure was not injected")
                    retry = await client.post("/chat", json=second)
                    return {"retry_http": retry.status_code, "retry_state": (retry.json().get("logistics_case") or {}).get("state"),
                            "retry_error": retry.json().get("error_code"), "ticket_count": count(db_path, "receipt-fault")}
        if not enabled:
            return await requests()
        async with await WorkflowEnvironment.start_local() as temporal:
            with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                stop = mp.Event()
                proc = mp.Process(target=worker_process, args=(stop,)); proc.start()
                try:
                    return await requests()
                finally:
                    stop.set(); proc.join(15)
                    if proc.is_alive():
                        proc.terminate(); proc.join(5)


async def worker_crash_after_commit(root):
    db_path = str(Path(root) / "worker-crash.sqlite3")
    marker = str(Path(root) / "worker-crash.marker")
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": db_path, "DIAN_TEMPORAL_LOGISTICS": "true", "DIAN_ENV": "development"}):
        memory, orchestrator = Memory(), ControlledOrchestrator()
        orchestrator.issue = "logistics_lost"
        async with await WorkflowEnvironment.start_local() as temporal:
            with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                stop = mp.Event()
                proc = mp.Process(target=worker_process, args=(stop, marker)); proc.start()
                try:
                    with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test", timeout=55) as client:
                            first = {"message": "包裹疑似丢件，订单号 DEMO1001", "user_id": "demo-user", "conv_id": "worker-crash", "client_request_id": "first"}
                            second = {**first, "message": "确认建单", "client_request_id": "second"}
                            initial = await client.post("/chat", json=first)
                            assert initial.status_code == 200 and initial.json()["logistics_case"]["state"] == "awaiting_confirmation"
                            start = time.perf_counter()
                            pending = asyncio.create_task(client.post("/chat", json=second))
                            for _ in range(250):
                                if Path(marker).exists():
                                    break
                                await asyncio.sleep(.1)
                            assert Path(marker).exists(), "worker did not reach post-commit crash"
                            proc.join(5)
                            assert proc.exitcode == 86, f"worker exit={proc.exitcode}"
                            replacement_stop = mp.Event()
                            replacement = mp.Process(target=worker_process, args=(replacement_stop,)); replacement.start()
                            try:
                                response = await asyncio.wait_for(pending, timeout=48)
                                view = response.json().get("logistics_case") or {}
                                result = {"worker_exit": proc.exitcode, "http": response.status_code, "state": view.get("state"),
                                          "ticket_count": count(db_path, "worker-crash"),
                                          "recovery_ms": round((time.perf_counter() - start) * 1000, 2)}
                                assert result["http"] == 200 and result["state"] == "ticket_created" and result["ticket_count"] == 1
                                return result
                            finally:
                                replacement_stop.set(); replacement.join(15)
                                if replacement.is_alive():
                                    replacement.terminate(); replacement.join(5)
                finally:
                    stop.set(); proc.join(15)
                    if proc.is_alive():
                        proc.terminate(); proc.join(5)


async def cancel_before_dispatch(root):
    """Cancel the waiting API task after its durable row is written, then retry."""
    db_path = str(Path(root) / "cancel.sqlite3")
    with patch.dict(os.environ, {"DIAN_LOGISTICS_DB": db_path, "DIAN_TEMPORAL_LOGISTICS": "true", "DIAN_ENV": "development"}):
        memory, orchestrator = Memory(), ControlledOrchestrator()
        orchestrator.issue = "logistics_lost"
        async with await WorkflowEnvironment.start_local() as temporal:
            with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test", timeout=55) as client:
                        payload = {"message": "包裹疑似丢件，订单号 DEMO1001", "user_id": "demo-user", "conv_id": "cancel", "client_request_id": "accepted"}
                        pending = asyncio.create_task(client.post("/chat", json=payload))
                        for _ in range(100):
                            if Path(db_path).exists():
                                try:
                                    with closing(sqlite3.connect(db_path)) as db:
                                        found = db.execute("SELECT count(*) FROM temporal_dispatch WHERE state='pending'").fetchone()[0]
                                except sqlite3.OperationalError:
                                    found = 0
                                if found:
                                    break
                            await asyncio.sleep(.05)
                        else:
                            raise AssertionError("API did not durably accept request")
                        pending.cancel()
                        try:
                            await pending
                        except asyncio.CancelledError:
                            pass
                        stop = mp.Event()
                        proc = mp.Process(target=worker_process, args=(stop,)); proc.start()
                        try:
                            retry = await client.post("/chat", json=payload)
                            result = {"http": retry.status_code, "state": (retry.json().get("logistics_case") or {}).get("state"),
                                      "ticket_count": count(db_path, "cancel")}
                            assert result == {"http": 200, "state": "awaiting_confirmation", "ticket_count": 0}
                            return result
                        finally:
                            stop.set(); proc.join(15)
                            if proc.is_alive():
                                proc.terminate(); proc.join(5)


async def run_cases(cases, enabled, db_path):
    memory, orchestrator = Memory(), ControlledOrchestrator()
    records = []
    with patch.object(api, "_memory", memory), patch.object(api, "_orchestrator", orchestrator), patch.object(api, "_profile_updater", None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test", timeout=55) as client:
            for case in cases:
                conv = case["case_id"]
                for index, turn in enumerate(case["turns"]):
                    orchestrator.issue = turn["expected"]["case_issue"]
                    start = time.perf_counter()
                    response = await client.post("/chat", json={"message": turn["message"], "user_id": "demo-user",
                                                                "conv_id": conv, "client_request_id": f"{conv}-{index}"})
                    elapsed = round((time.perf_counter() - start) * 1000, 2)
                    body = response.json()
                    view = body.get("logistics_case") or {}
                    records.append({"case": conv, "turn": index, "http": response.status_code,
                                    "status": body.get("status"), "state": view.get("state"),
                                    "issue": view.get("issue"), "ticket_count": count(db_path, conv),
                                    "expected_state": turn["expected"]["case_state"],
                                    "expected_tickets": turn["expected"]["ticket_count"], "latency_ms": elapsed})
            memory.states.pop(("demo-user", "DIAN-013"), None)
            expired = await client.get("/logistics/case", params={"user_id": "demo-user", "conv_id": "DIAN-013"})
            boundary = {"http": expired.status_code, "case_visible_after_pointer_loss": expired.json().get("logistics_case") is not None,
                        "ticket_count_in_db": count(db_path, "DIAN-013")}
    return records, boundary


async def compare():
    source = ROOT / "evaluation/datasets/dian_logistics_v1/test.jsonl"
    lines = source.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if json.loads(line)["case_id"] in SELECTED]
    assert len(cases) == len(SELECTED), "selected dataset case missing"
    result = {"run_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
              "platform": platform.platform(), "temporalio": version("temporalio"),
              "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "suite": list(SELECTED), "variants": {}, "fault": {}}
    with tempfile.TemporaryDirectory() as temp:
        for variant in ("baseline", "temporal"):
            path = str(Path(temp) / f"{variant}.sqlite3")
            env_vars = {"DIAN_LOGISTICS_DB": path, "DIAN_TEMPORAL_LOGISTICS": str(variant == "temporal").lower(), "DIAN_ENV": "development"}
            with patch.dict(os.environ, env_vars):
                if variant == "baseline":
                    records, pointer = await run_cases(cases, False, path)
                else:
                    async with await WorkflowEnvironment.start_local() as temporal:
                        with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                            stop = mp.Event()
                            proc = mp.Process(target=worker_process, args=(stop,)); proc.start()
                            try:
                                records, pointer = await run_cases(cases, True, path)
                            finally:
                                stop.set(); proc.join(15)
                                if proc.is_alive():
                                    proc.terminate(); proc.join(5)
                result["variants"][variant] = records
                result["fault"]["pointer_loss_" + variant] = pointer
                print(variant, len(records), sum(r["state"] == r["expected_state"] and r["ticket_count"] == r["expected_tickets"] for r in records), flush=True)
        a, b = result["variants"]["baseline"], result["variants"]["temporal"]
        result["equivalent"] = all((x["http"], x["status"], x["state"], x["issue"], x["ticket_count"]) ==
                                   (y["http"], y["status"], y["state"], y["issue"], y["ticket_count"]) for x, y in zip(a, b))
        result["variants"]["baseline_summary"] = summary(a)
        result["variants"]["temporal_summary"] = summary(b)
        print(json.dumps({"equivalent": result["equivalent"], "baseline": summary(a), "temporal": summary(b)}, ensure_ascii=False), flush=True)
        result["fault"]["receipt_baseline"] = await receipt_fault(False, temp)
        result["fault"]["receipt_temporal"] = await receipt_fault(True, temp)
        result["fault"]["worker_crash_temporal"] = await worker_crash_after_commit(temp)
        result["fault"]["cancel_before_dispatch"] = await cancel_before_dispatch(temp)
        assert result["equivalent"] and summary(a)["passed"] == len(a) and summary(b)["passed"] == len(b)
        assert result["fault"]["receipt_baseline"]["retry_http"] == 503
        assert result["fault"]["receipt_temporal"]["retry_http"] == 200
        assert result["fault"]["worker_crash_temporal"]["ticket_count"] == 1
        assert result["fault"]["pointer_loss_baseline"]["case_visible_after_pointer_loss"]
        assert result["fault"]["pointer_loss_temporal"]["case_visible_after_pointer_loss"]
        print(json.dumps(result["fault"], ensure_ascii=False), flush=True)
    (ROOT / "evaluation/temporal_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def summary(records):
    times = sorted(row["latency_ms"] for row in records)
    warm = sorted(row["latency_ms"] for row in records[1:])
    passed = sum(row["http"] == 200 and row["state"] == row["expected_state"] and row["ticket_count"] == row["expected_tickets"] for row in records)
    return {"turns": len(records), "passed": passed, "p50_ms": times[len(times)//2],
            "p95_ms": times[min(len(times)-1, int(len(times)*.95))],
            "warm_p50_ms": warm[len(warm)//2], "max_ms": times[-1]}


if __name__ == "__main__":
    mp.freeze_support()
    asyncio.run(compare())
