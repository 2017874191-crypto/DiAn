"""Repeated controlled latency and process-resource comparison; no paid model calls."""
import asyncio
import hashlib
import json
import logging
import multiprocessing as mp
import os
import platform
import statistics
import sys
import tempfile
import threading
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import psutil
from temporalio.testing import WorkflowEnvironment

from evaluation.compare_temporal_logistics import ROOT, SELECTED, run_cases, summary, worker_process


def sample_process_tree(stop, samples, result):
    root = psutil.Process(os.getpid())
    counters = {}
    while not stop.is_set():
        rss = 0
        names = set()
        for process in [root, *root.children(recursive=True)]:
            try:
                info = process.memory_info()
                cpu = process.cpu_times()
                key = (process.pid, process.create_time())
                observed = counters.setdefault(key, [cpu.user + cpu.system, cpu.user + cpu.system])
                observed[1] = max(observed[1], cpu.user + cpu.system)
                rss += info.rss
                names.add(process.name())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        samples.append((rss, len(names)))
        stop.wait(.05)
    result.update({"peak_rss_mb": round(max((value for value, _ in samples), default=0) / 1048576, 2),
                   "observed_cpu_seconds": round(sum(end - start for start, end in counters.values()), 3),
                   "samples": len(samples)})


async def one_variant(cases, variant, db_path):
    enabled = variant == "temporal"
    env_vars = {"DIAN_LOGISTICS_DB": db_path, "DIAN_TEMPORAL_LOGISTICS": str(enabled).lower(),
                "DIAN_TICKET_PROVIDER": "sqlite", "DIAN_ENV": "development"}
    with patch.dict(os.environ, env_vars):
        if enabled:
            async with await WorkflowEnvironment.start_local() as temporal:
                with patch.dict(os.environ, {"DIAN_TEMPORAL_ADDRESS": temporal.client.service_client.config.target_host}):
                    stop_worker = mp.Event()
                    worker = mp.Process(target=worker_process, args=(stop_worker,))
                    worker.start()
                    try:
                        return await measure(cases, enabled, db_path)
                    finally:
                        stop_worker.set(); worker.join(15)
                        if worker.is_alive():
                            worker.terminate(); worker.join(5)
        return await measure(cases, enabled, db_path)


async def measure(cases, enabled, db_path):
    stop, samples, resources = threading.Event(), [], {}
    sampler = threading.Thread(target=sample_process_tree, args=(stop, samples, resources), daemon=True)
    sampler.start()
    try:
        records, pointer = await run_cases(cases, enabled, db_path)
    finally:
        stop.set()
        sampler.join(5)
    result = summary(records)
    assert result["passed"] == result["turns"] == 14
    assert pointer["case_visible_after_pointer_loss"]
    return {"summary": result, "resources": resources,
            "records": [{"case": row["case"], "turn": row["turn"], "state": row["state"],
                         "ticket_count": row["ticket_count"], "latency_ms": row["latency_ms"]} for row in records]}


async def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    source = ROOT / "evaluation/datasets/dian_logistics_v1/test.jsonl"
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
             if json.loads(line)["case_id"] in SELECTED]
    assert len(cases) == len(SELECTED)
    repeats = max(2, int(os.getenv("DIAN_COST_REPEATS", "3")))
    report = {"run_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
              "platform": platform.platform(), "temporalio": version("temporalio"),
              "psutil": psutil.__version__, "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "cases": list(SELECTED), "repeats": repeats,
              "scope": "local ASGI, controlled memory, SQLite mock ticket, no paid model or real Redis",
              "variants": {"baseline": [], "temporal": []}}
    with tempfile.TemporaryDirectory() as temp:
        for index in range(repeats):
            order = ("baseline", "temporal") if index % 2 == 0 else ("temporal", "baseline")
            for variant in order:
                result = await one_variant(cases, variant, str(Path(temp) / f"{variant}-{index}.sqlite3"))
                report["variants"][variant].append(result)
                print(f"repeat {index + 1} {variant}: {result['summary']} {result['resources']}", flush=True)
    report["medians"] = {variant: {key: round(statistics.median(item["summary"][key] if key == "p50_ms"
                             else item["resources"][key] for item in runs), 2)
                         for key in ("p50_ms", "peak_rss_mb", "observed_cpu_seconds")}
                         for variant, runs in report["variants"].items()}
    output = ROOT / "evaluation/temporal_cost_profile.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["medians"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    asyncio.run(main())
