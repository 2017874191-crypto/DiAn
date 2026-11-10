"""Summarize three isolated development-set replays without changing raw reports."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from evaluation.logistics_eval_runner import SUITE, report

VARIANTS = ("rules_only", "llm_rules", "full")


def compare(root, image_id):
    runs = {}
    case_ids = None
    for variant in VARIANTS:
        path = root / variant / "eval" / "report.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("suite") != "dian_logistics_v1" or raw.get("split") != "dev" or raw.get("variant") != variant:
            raise ValueError(f"report identity mismatch: {path}")
        items = {item["case_id"]: item for item in raw["cases"]}
        if len(items) != 30 or (case_ids is not None and set(items) != case_ids):
            raise ValueError(f"case set mismatch: {path}")
        case_ids = set(items)
        aggregate = report(raw["cases"], "dev", variant)
        runs[variant] = {"summary": aggregate["summary"],
                         "metrics": aggregate["metrics"],
                         "cases": items, "raw_report": str(path)}
    def status(variant, case_id):
        return runs[variant]["cases"][case_id]["status"]
    comparison = {
        "suite": "dian_logistics_v1", "split": "dev",
        "dataset_sha256": hashlib.sha256((SUITE / "dev.jsonl").read_bytes()).hexdigest(),
        "image_id": image_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variants": {name: {key: value for key, value in run.items() if key != "cases"}
                     for name, run in runs.items()},
        "case_differences": {
            "rules_to_llm_improved": sorted(cid for cid in case_ids if status("rules_only", cid) != "flow_pass_semantic_pending" and status("llm_rules", cid) == "flow_pass_semantic_pending"),
            "llm_to_full_regressed": sorted(cid for cid in case_ids if status("llm_rules", cid) == "flow_pass_semantic_pending" and status("full", cid) != "flow_pass_semantic_pending"),
            "llm_to_full_improved": sorted(cid for cid in case_ids if status("llm_rules", cid) != "flow_pass_semantic_pending" and status("full", cid) == "flow_pass_semantic_pending"),
        },
        "interpretation_limit": "Flow assertions only; semantic review and frozen test evaluation pending.",
    }
    return comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.root, args.image_id)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
