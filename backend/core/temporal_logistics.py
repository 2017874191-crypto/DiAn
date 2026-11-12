"""Opt-in durable dispatch for the existing logistics turn. Requires a separate worker."""
import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

QUEUE = "dian-logistics-v1"


@contextmanager
def _db():
    path = Path(os.getenv("DIAN_LOGISTICS_DB", "data/logistics/cases.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS temporal_dispatch (id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _key(req):
    raw = json.dumps([req.user_id, req.conv_id, req.client_request_id], ensure_ascii=True)
    return "dian-logistics-" + hashlib.sha256(raw.encode()).hexdigest()


def _payload(req):
    from core.logistics import LogisticsService

    return dict(message=req.message, user_id=req.user_id, conv_id=req.conv_id,
                request_id=req.request_id, client_request_id=req.client_request_id,
                run_id=LogisticsService.run_id(req), intent=req.intent.value, entities=req.entities,
                conversation_state=req.conversation_state or {})


def accept(req):
    from core.logistics import LogisticsService

    if not req.client_request_id:
        raise ValueError("Temporal logistics requires a client request ID")
    LogisticsService()  # Initialize the Run tables before the shared acceptance transaction.
    key = _key(req)
    input_hash = hashlib.sha256(req.message.encode()).hexdigest()
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO temporal_dispatch VALUES (?,?,?,'pending')",
                   (key, input_hash, json.dumps(_payload(req), ensure_ascii=False)))
        row = db.execute("SELECT input_hash,payload FROM temporal_dispatch WHERE id=?", (key,)).fetchone()
        if row[0] != input_hash:
            raise ValueError("同一 client_request_id 不能用于不同消息")
        stored = json.loads(row[1])
        if not stored.get("run_id"):
            stored["client_request_id"] = req.client_request_id
            stored["run_id"] = LogisticsService.run_id(req)
            db.execute("UPDATE temporal_dispatch SET payload=? WHERE id=?",
                       (json.dumps(stored, ensure_ascii=False), key))
        LogisticsService.reserve_run(db, req, stored["request_id"])
    return key, stored


@activity.defn
async def process_logistics_turn(payload: dict) -> dict:
    return await asyncio.to_thread(_process, payload)


def _process(payload: dict) -> dict:
    from agents.agent_orchestrator import Request
    from core.intent_recognizer import IntentCategory
    from core.logistics import LogisticsService
    req = Request(message=payload["message"], user_id=payload["user_id"],
                  conv_id=payload["conv_id"], request_id=payload["request_id"],
                  client_request_id=payload.get("client_request_id"),
                  intent=IntentCategory(payload["intent"]), entities=payload["entities"],
                  conversation_state=payload["conversation_state"])
    result = LogisticsService().process(req)
    return {"result": result, "conversation_state": req.conversation_state}


@workflow.defn
class LogisticsTurn:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        return await workflow.execute_activity(
            process_logistics_turn, payload,
            start_to_close_timeout=timedelta(seconds=15),
            schedule_to_close_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
        )


async def execute(req):
    key, _ = await asyncio.to_thread(accept, req)
    client = await Client.connect(os.getenv("DIAN_TEMPORAL_ADDRESS", "localhost:7233"))
    # The dispatcher may not yet have started the workflow. The accepted row survives API restarts.
    for _ in range(100):
        try:
            handle = client.get_workflow_handle(key)
            await handle.describe()
            break
        except Exception as exc:
            from temporalio.service import RPCError
            if isinstance(exc, RPCError):
                await asyncio.sleep(.2)
                continue
            raise
    else:
        raise TimeoutError("accepted logistics request was not dispatched")
    return await asyncio.wait_for(handle.result(), timeout=45)


async def dispatch(client):
    while True:
        rows = await asyncio.to_thread(_pending)
        for key, payload in rows:
            try:
                await client.start_workflow(LogisticsTurn.run, json.loads(payload), id=key,
                                            task_queue=QUEUE, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            except WorkflowAlreadyStartedError:
                pass
            except Exception:
                continue  # Keep pending for the next scan.
            await asyncio.to_thread(_sent, key)
        await asyncio.sleep(.5)


def _pending():
    with _db() as db:
        return db.execute("SELECT id,payload FROM temporal_dispatch WHERE state='pending' LIMIT 100").fetchall()


def _sent(key):
    with _db() as db:
        db.execute("UPDATE temporal_dispatch SET state='dispatched' WHERE id=?", (key,))
        row = db.execute("SELECT payload FROM temporal_dispatch WHERE id=?", (key,)).fetchone()
        if row:
            run_id = json.loads(row[0]).get("run_id")
            if run_id:
                now = datetime.now(timezone.utc).isoformat()
                db.execute("UPDATE logistics_runs SET state='dispatched',updated_at=? WHERE run_id=? AND state='accepted'", (now, run_id))
                db.execute("UPDATE logistics_steps SET state='succeeded',updated_at=? WHERE run_id=? AND step_name='dispatch'", (now, run_id))


async def worker_main():
    client = await Client.connect(os.getenv("DIAN_TEMPORAL_ADDRESS", "localhost:7233"))
    async with Worker(client, task_queue=QUEUE, workflows=[LogisticsTurn], activities=[process_logistics_turn]):
        await dispatch(client)


if __name__ == "__main__":
    asyncio.run(worker_main())
