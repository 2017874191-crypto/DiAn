"""Measure logistics flow and background reads against the dev set with fixed dependencies."""

import argparse
import asyncio
import json
import math
from collections import Counter
from pathlib import Path
from statistics import median

from evaluation.compare_intent_faults import replay
from evaluation.logistics_eval_runner import ROOT, SUITE, load_cases
from evaluation.provenance import build_identity, git_commit, sha256

CASES = {
    "DIAN-001": "logistics_damaged", "DIAN-017": "logistics_dispute",
    "DIAN-019": "order_status", "DIAN-023": "billing", "DIAN-026": "logistics_lost",
    "DIAN-041": "logistics_lost", "DIAN-042": "logistics_lost",
    "DIAN-043": "logistics_lost", "DIAN-047": "logistics_lost", "DIAN-048": "logistics_damaged",
}
EAGER_VARIANTS = ("eager", "without_profile", "without_episodic", "recent_only")
STALE_ORDER = "历史已收货，订单号 DEMO1002。"
READ_DELAY = 0.02


async def run_case(case, variant, build):
    reads = Counter({"profile": 0, "episodic": 0})
    restored = False

    def configure(memory):
        read_context = memory.get_context
        turn = 0

        async def context(user_id, conv_id, **kwargs):
            nonlocal turn, restored
            turn += 1
            if case["case_id"] == "DIAN-026" and turn == 2:
                restored = await memory._redis.delete(
                    memory._state_key(user_id, conv_id), memory._wm_key(user_id, conv_id),
                ) == 2
                assert restored, "Expected a previously committed Redis turn and state"
            if variant != "recent_only":
                await memory._redis.set(memory._summary_key(user_id, conv_id), STALE_ORDER)
            return await read_context(user_id, conv_id, **kwargs)

        async def query(*_args, **_kwargs):
            reads["episodic"] += 1
            await asyncio.sleep(READ_DELAY)
            return {"documents": [[STALE_ORDER] * memory.HISTORY_TOP_K]}

        async def profile(_user_id):
            reads["profile"] += 1
            await asyncio.sleep(READ_DELAY)
            return {"prior_order": STALE_ORDER}

        async def no_profile(_user_id):
            return {}

        async def no_episodic(_user_id, _conv_id, _query):
            return []

        memory.get_context = context
        memory._query_episodic = query
        memory._get_profile = no_profile if variant in {"without_profile", "recent_only"} else profile
        if variant in {"without_episodic", "recent_only"}:
            memory._search_episodic = no_episodic

    row = await replay(case, "full", False, (), build, intent_label=CASES[case["case_id"]],
                       configure_memory=configure)
    row.update(reads=dict(reads), cleared_redis_before_confirmation=restored)
    return row


def summarize(rows):
    times = sorted(turn["chat_wall_ms"] for row in rows for turn in row["turns"]
                   if turn["chat_wall_ms"] is not None)
    return {
        "passed": sum(row["status"] == "flow_pass_semantic_pending" for row in rows),
        "total": len(rows),
        "profile_reads": sum(row["reads"]["profile"] for row in rows),
        "episodic_reads": sum(row["reads"]["episodic"] for row in rows),
        "ticket_count": sum(row["ticket_count"] for row in rows),
        "unexpected_tickets": sum(turn["unexpected_tickets"] or 0 for row in rows for turn in row["turns"]),
        "chat_wall_p50_ms": median(times) if times else None,
        "chat_wall_p95_ms": times[math.ceil(.95 * len(times)) - 1] if times else None,
        "chat_wall_samples": len(times),
    }


async def compare(phase, rounds):
    cases = [case for case in load_cases("dev") if case["case_id"] in CASES]
    build = build_identity(ROOT)
    variants = EAGER_VARIANTS if phase == "eager" else ("routed",)
    results = {variant: [] for variant in variants}
    shared_identity = None
    for index in range(rounds):
        order = variants[index % len(variants):] + variants[:index % len(variants)]
        for variant in order:
            for case in cases:
                row = await run_case(case, variant, build)
                identity = row.pop("runtime")
                if shared_identity is None:
                    shared_identity = identity
                if identity != shared_identity:
                    raise RuntimeError(f"Replay identity changed in {variant} for {case['case_id']}")
                results[variant].append({"round": index + 1, "case_id": case["case_id"], **row})
    return {
        "phase": phase,
        "runner_commit": git_commit(ROOT),
        "dataset_sha256": sha256((SUITE / "dev.jsonl").read_bytes()),
        "runtime": shared_identity,
        "fixture_read_delay_ms": READ_DELAY * 1000,
        "scenarios": results,
        "summary": {variant: summarize(rows) for variant, rows in results.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("eager", "routed"),
                        help="Use eager on the baseline checkout and routed on the candidate checkout")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    result = asyncio.run(compare(args.phase, args.rounds))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
