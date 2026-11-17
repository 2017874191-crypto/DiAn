"""Summarize three isolated development-set replays without changing raw reports."""
import argparse
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from evaluation.logistics_eval_runner import report
from evaluation.provenance import BOOL_CONFIG, MODES, NUMERIC_CONFIG, SCHEMA, TEXT_CONFIG

VARIANTS = MODES


def run_identity(raw, variant, case_ids, path):
    provenance = deepcopy(raw.get("provenance"))
    try:
        if provenance["schema"] != SCHEMA or provenance["verification_errors"] != []:
            raise ValueError("unsupported or failed runtime verification")
        runtime = provenance["target"]["runtime"]
        settings = runtime["configuration"]
        if settings.pop("intent_mode") != variant:
            raise ValueError("runtime intent mode does not match group")
        if provenance["case_ids"] != sorted(case_ids):
            raise ValueError("case IDs differ from the run manifest")
        if type(provenance["controlled_restart"]) is not bool:
            raise ValueError("invalid restart setting")
        image_id = provenance["target"]["image_id"]
        if image_id is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("invalid image ID")
        fields = [(provenance, ("dataset_sha256",)),
                  (runtime, ("fixtures_sha256", "policy_sha256", "skills_sha256", "tools_sha256")),
                  (provenance["runner"], ("source_sha256", "dependencies_sha256")),
                  (runtime["build"], ("source_sha256", "dependencies_sha256")),
                  (settings, ("model_endpoint_sha256",))]
        for section, keys in fields:
            for key in keys:
                if not re.fullmatch(r"[0-9a-f]{64}", section[key]):
                    raise ValueError(f"invalid {key}")
        for build in (runtime["build"], provenance["runner"]):
            if not all(isinstance(build[key], str) and build[key] for key in ("python", "system", "architecture", "sqlite")):
                raise ValueError("missing runtime environment")
        for key in BOOL_CONFIG:
            if type(settings[key]) is not bool:
                raise ValueError(f"invalid configuration {key}")
        for key in TEXT_CONFIG:
            if key != "intent_mode" and (not isinstance(settings[key], str) or not settings[key]):
                raise ValueError(f"invalid configuration {key}")
        for key, (_, default) in NUMERIC_CONFIG.items():
            if type(settings[key]) is not type(default):
                raise ValueError(f"invalid configuration {key}")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid or missing run provenance: {path}: {exc}") from exc
    return provenance


def compare(root):
    runs = {}
    case_ids = None
    common = None
    for variant in VARIANTS:
        path = root / variant / "eval" / "report.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("suite") != "dian_logistics_v1" or raw.get("split") != "dev" or raw.get("variant") != variant:
            raise ValueError(f"report identity mismatch: {path}")
        items = {item["case_id"]: item for item in raw["cases"]}
        if len(raw["cases"]) != 30 or len(items) != 30 or (case_ids is not None and set(items) != case_ids):
            raise ValueError(f"case set mismatch: {path}")
        case_ids = set(items)
        identity = run_identity(raw, variant, case_ids, path)
        if common is None:
            common = identity
        elif identity != common:
            differing = [key for key in sorted(identity.keys() | common.keys()) if identity.get(key) != common.get(key)]
            raise ValueError(f"experiment conditions differ ({', '.join(differing)}): {path}")
        aggregate = report(raw["cases"], "dev", variant)
        runs[variant] = {"summary": aggregate["summary"],
                         "metrics": aggregate["metrics"],
                         "cases": items, "raw_report": path.relative_to(root).as_posix()}
    def status(variant, case_id):
        return runs[variant]["cases"][case_id]["status"]
    comparison = {
        "suite": "dian_logistics_v1", "split": "dev",
        "dataset_sha256": common["dataset_sha256"],
        "image_id": common["target"]["image_id"],
        "provenance": common,
        "varied_configuration": "target.runtime.configuration.intent_mode",
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(args.root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
