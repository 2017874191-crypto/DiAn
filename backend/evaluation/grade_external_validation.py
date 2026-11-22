"""Post-run rule checks and existing LLM Judge; do not print or save private label text."""
import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic

from core.llm_utils import model_scope
from core.performance import collect_performance, performance_snapshot
from evaluation.compare_rag import model_settings
from evaluation.evaluator import LLMJudge
from evaluation.prepare_rag_data import write_json
from evaluation.run_external_validation import digest, read_rows

COUNT_FIELDS = {"mock_ticket_count", "mock_damage_review_ticket_count_for_operation",
                "mock_signature_dispute_ticket_count_for_operation"}


async def grade(args):
    if args.output.exists():
        raise FileExistsError("Keep original grading output; do not overwrite")
    replay = json.loads(args.report.read_text(encoding="utf-8"))
    intake = json.loads(args.manifest.read_text(encoding="utf-8"))
    if (intake["source_files"][args.labels.name]["sha256"] != digest(args.labels) or
            intake["candidate_commit"] != replay["candidate_commit"]):
        raise ValueError("Labels or candidate identity differs from frozen intake")
    if not replay["complete"]:
        raise ValueError("Only grade a completed fixed-candidate run")
    labels = {r["case_id"]: r for r in read_rows(args.labels)}
    if set(labels) != {r["case_id"] for r in replay["cases"]}:
        raise ValueError("Private labels and executed cases differ")
    settings, _ = model_settings()
    result = {"schema": 1, "report_sha256": digest(args.report), "labels_sha256": digest(args.labels),
              "candidate_commit": replay["candidate_commit"], "judge_model": settings["model"],
              "same_model_family_as_candidate": settings["model"] == replay["model"],
              "human_reviewed": False, "scope": "Program-only label handling and post-run exploratory LLM Judge; no accuracy or generalization claim",
              "private_label_contents_saved": False, "cases": [], "complete": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle)
    async with AsyncAnthropic(api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(50, connect=5))) as client:
        judge = LLMJudge(client, settings["model"])
        for case in replay["cases"]:
            label = labels[case["case_id"]]
            expected = label["required_final_business_state"]
            count_targets = [v for k, v in expected.items() if k in COUNT_FIELDS and type(v) is int]
            if len(count_targets) != 1:
                raise ValueError("Ticket-count schema requires review")
            transcript = [{"user": t["user_message"], "assistant": t["response"].get("response", ""),
                           "http_status": t["http_status"]} for t in case["turns"]]
            context = {"public_rules": replay["public_corpus"], "private_rubric": label,
                       "actual_observed_tool_results": case["fixture_events"],
                       "actual_business_state": {"ticket_count": case["ticket_count"],
                           "case_view": case["turns"][-1]["response"].get("logistics_case")},
                       "skipped_script_turns": case["skipped_script_turns"],
                       "profile_limitations": case["profile_limitations"],
                       "instruction": "Evaluate actual conversation and evidence, not model self-reports. Unexecuted conditional turns are not automatically successful. Do not require fixed tool names, agent names or wording."}
            with collect_performance(), model_scope("evaluation", max_calls=2):
                scores = await judge.judge(
                    question=json.dumps([t["user_message"] for t in case["turns"]], ensure_ascii=False),
                    response=json.dumps(transcript, ensure_ascii=False),
                    context=json.dumps(context, ensure_ascii=False))
                performance = performance_snapshot()
            result["cases"].append({"case_id": case["case_id"],
                "rule_checks": {**case["rule_checks"],
                                "ticket_postcondition_matches": case["ticket_count"] == count_targets[0]},
                "ticket_count": case["ticket_count"],
                "unautomated_postcondition_fields": sorted(k for k in expected if k not in COUNT_FIELDS),
                "profile_limitations": case["profile_limitations"],
                "signature_mapping_unresolved": case["signature_mapping_unresolved"],
                "skipped_script_turn_count": len(case["skipped_script_turns"]),
                "judge": asdict(scores), "performance": performance})
            write_json(args.output, result)
            print("graded case", len(result["cases"]), "judge_failed", scores.judge_failed, flush=True)
    result["summary"] = {
        "cases": len(result["cases"]),
        "ticket_postcondition_matches": sum(r["rule_checks"]["ticket_postcondition_matches"] for r in result["cases"]),
        "judge_valid_cases": sum(not r["judge"]["judge_failed"] for r in result["cases"]),
        "safety_rule_failures": sum(not all(v for k, v in r["rule_checks"].items()
            if k != "ticket_postcondition_matches") for r in result["cases"]),
        "interpretation": "Ticket count and Judge scores are separate signals, not a complete task success rate. Independent human review remains absent.",
    }
    result["complete"] = True
    write_json(args.output, result)
    print(json.dumps(result["summary"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(grade(args))


if __name__ == "__main__":
    main()
