"""Pair a committed recognizer with the current recognizer on fixed development cases."""
import argparse
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
from anthropic import AsyncAnthropic

from core import intent_recognizer
from core.llm_utils import model_scope
from core.performance import collect_performance, performance_snapshot
from evaluation.compare_rag import model_settings
from evaluation.prepare_rag_data import ROOT, write_json
from evaluation.provenance import build_identity, git_commit, sha256


def load_baseline(revision):
    source = subprocess.check_output(
        ["git", "show", revision + ":backend/core/intent_recognizer.py"], cwd=ROOT)
    module = types.ModuleType("dian_boundary_baseline")
    module.__file__ = str(ROOT / "core/intent_recognizer.py")
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module, sha256(source.replace(b"\r\n", b"\n"))


def summarize(rows):
    latency = sorted(row["latency_ms"] for row in rows)
    usage_fields = ("calls", "errors", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {
        "attempts": len(rows), "intent_correct": sum(row["intent_correct"] for row in rows),
        "goal_correct": sum(row["goal_correct"] for row in rows),
        "joint_correct": sum(row["intent_correct"] and row["goal_correct"] for row in rows),
        "model_errors": sum(row["model_failed"] for row in rows),
        "wall_p95_ms": latency[math.ceil(len(latency) * .95) - 1],
        "usage": {field: sum(row["performance"]["llm"].get(field, 0) for row in rows) for field in usage_fields},
        "by_group": {group: {
            "attempts": sum(row["group"] == group for row in rows),
            "intent_correct": sum(row["group"] == group and row["intent_correct"] for row in rows),
            "goal_correct": sum(row["group"] == group and row["goal_correct"] for row in rows),
            "joint_correct": sum(row["group"] == group and row["intent_correct"] and row["goal_correct"] for row in rows),
        } for group in sorted({row["group"] for row in rows})},
    }


async def compare(protocol_path, output):
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    baseline, baseline_sha = load_baseline(protocol["baseline_commit"])
    settings, source = model_settings()
    result = {"schema": 1, "candidate_commit": git_commit(ROOT), "build": build_identity(ROOT),
              "model": settings["model"], "configuration_source": source,
              "model_endpoint_sha256": sha256(settings["base_url"].encode()),
              "baseline_source_sha256": baseline_sha,
              "candidate_source_sha256": sha256((ROOT / "core/intent_recognizer.py").read_bytes().replace(b"\r\n", b"\n")),
              "protocol": protocol, "protocol_sha256": sha256(protocol_path.read_bytes()),
              "rows": [], "complete": False}
    env = {"INTENT_RECOGNITION_MODE": "full", "INTENT_EMBEDDING_PROVIDER": "local",
           "INTENT_EMBEDDING_ENABLED": "true", "INTENT_LOGISTICS_EMBEDDING_ENABLED": "false"}
    semaphore = asyncio.Semaphore(protocol["pair_concurrency"])
    async with AsyncAnthropic(
        api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=5)),
    ) as client:
        with (patch.dict(os.environ, env),
              patch.object(baseline, "AsyncAnthropic", return_value=client),
              patch.object(intent_recognizer, "AsyncAnthropic", return_value=client)):
            async def pair(round_index, case_index, case):
                async with semaphore:
                    variants = [("baseline", baseline), ("candidate", intent_recognizer)]
                    if (round_index + case_index) % 2:
                        variants.reverse()
                    for variant, module in variants:
                        # A fresh recognizer prevents repeated rounds from hitting the result cache.
                        recognizer = module.IntentRecognizer(**settings)
                        with collect_performance(), model_scope("evaluation", max_calls=1):
                            observed = await recognizer.recognize(case["message"])
                            performance = performance_snapshot()
                        row = {"round": round_index + 1, "id": case["id"], "group": case["group"],
                               "variant": variant, "intent": observed.intent.value, "goal": observed.task_goal,
                               "reasoning": observed.reasoning, "source_scores": observed.source_scores,
                               "model_failed": observed.llm_failed, "latency_ms": observed.latency_ms,
                               "intent_correct": not observed.llm_failed and observed.intent.value in case["allowed_intents"],
                               "goal_correct": not observed.llm_failed and observed.task_goal in case["allowed_goals"],
                               "performance": performance}
                        result["rows"].append(row)
                        write_json(output, result)
            for round_index in range(protocol["rounds"]):
                await asyncio.gather(*(pair(round_index, index, case) for index, case in enumerate(protocol["cases"])))
                print("completed paired round", round_index + 1, flush=True)
    result["summary"] = {variant: summarize([row for row in result["rows"] if row["variant"] == variant])
                         for variant in ("baseline", "candidate")}
    by_id = {case["id"]: case for case in protocol["cases"]}
    non_action_goals = sum(row["variant"] == "candidate" and row["goal"] == "case_action"
                          and "case_action" not in by_id[row["id"]]["allowed_goals"] for row in result["rows"])
    before, after = result["summary"]["baseline"], result["summary"]["candidate"]
    thresholds = protocol["acceptance"]
    target_group = thresholds.get("target_group", "exposed_wrong_item")
    target_metric = thresholds.get("target_metric", "intent_correct")
    control_group = thresholds.get("control_group", "boundary_control")
    target_before, target_after = (item["by_group"][target_group][target_metric] for item in (before, after))
    result["checks"] = {
        "all_attempts_recorded": len(result["rows"]) == 2 * protocol["rounds"] * len(protocol["cases"]),
        "one_model_call_each": all(row["performance"]["llm"]["calls"] == 1 for row in result["rows"]),
        "no_model_errors": before["model_errors"] + after["model_errors"] == 0,
        "target_threshold": target_after >= thresholds.get("target_correct_at_least", thresholds.get("exposed_type_correct_at_least", 0)),
        "target_improved": target_after > target_before,
        "control_joint_no_regression": after["by_group"][control_group]["joint_correct"] >= before["by_group"][control_group]["joint_correct"],
        "no_unrequested_action_goal": non_action_goals == 0,
    }
    if thresholds.get("per_control_case_no_regression"):
        def correct_count(variant, case_id):
            return sum(row["variant"] == variant and row["id"] == case_id and row["intent_correct"]
                       and row["goal_correct"] for row in result["rows"])
        result["checks"]["per_control_case_no_regression"] = all(
            correct_count("candidate", case["id"]) >= correct_count("baseline", case["id"])
            for case in protocol["cases"] if case["group"] == control_group)
    result["non_action_controls_classified_as_case_action"] = non_action_goals
    result["complete"] = True
    write_json(output, result)
    print(json.dumps({"summary": result["summary"], "checks": result["checks"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, default=ROOT / "evaluation/reviews/dian_logistics_v1/intent-boundary-protocol.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args.protocol, args.output))


if __name__ == "__main__":
    main()
