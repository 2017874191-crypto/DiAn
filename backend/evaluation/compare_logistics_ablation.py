"""Summarize three isolated development-set replays without changing raw reports."""
import argparse
import json
import random
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from evaluation.logistics_eval_runner import report
from evaluation.provenance import BOOL_CONFIG, MODES, NUMERIC_CONFIG, SCHEMA, TEXT_CONFIG

VARIANTS = MODES
CONTRASTS = {"intent_mode": VARIANTS, "logistics_embedding": ("full", "full_off")}


def run_identity(raw, variant, case_ids, path, contrast="intent_mode"):
    provenance = deepcopy(raw.get("provenance"))
    try:
        if provenance["schema"] != SCHEMA or provenance["verification_errors"] != []:
            raise ValueError("unsupported or failed runtime verification")
        runtime = provenance["target"]["runtime"]
        settings = runtime["configuration"]
        expected_mode = variant if contrast == "intent_mode" else "full"
        if settings["intent_mode"] != expected_mode:
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
        if contrast == "logistics_embedding" and settings["logistics_embedding"] != (variant == "full"):
            raise ValueError("runtime logistics embedding switch does not match group")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid or missing run provenance: {path}: {exc}") from exc
    del settings[contrast]
    return provenance


def load_comparison(root, contrast):
    runs = {}
    case_ids = None
    common = None
    for variant in CONTRASTS[contrast]:
        path = root / variant / "eval" / "report.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        expected_mode = variant if contrast == "intent_mode" else "full"
        if raw.get("suite") != "dian_logistics_v1" or raw.get("split") != "dev" or raw.get("variant") != expected_mode:
            raise ValueError(f"report identity mismatch: {path}")
        items = {item["case_id"]: item for item in raw["cases"]}
        if len(raw["cases"]) != 30 or len(items) != 30 or (case_ids is not None and set(items) != case_ids):
            raise ValueError(f"case set mismatch: {path}")
        case_ids = set(items)
        identity = run_identity(raw, variant, case_ids, path, contrast)
        if common is None:
            common = identity
        elif identity != common:
            differing = [key for key in sorted(identity.keys() | common.keys()) if identity.get(key) != common.get(key)]
            raise ValueError(f"experiment conditions differ ({', '.join(differing)}): {path}")
        aggregate = report(raw["cases"], "dev", variant)
        turns = [turn for case in raw["cases"] for turn in case["turns"]]
        observed = [turn["observed"] for turn in turns if turn.get("observed")]
        passed_latencies = sorted(
            turn["observed"]["wall_latency_ms"] for turn in turns
            if turn["status"] == "pass" and turn.get("observed")
            and isinstance(turn["observed"].get("wall_latency_ms"), (int, float))
        )
        aggregate["metrics"].update({
            "passed_turn_p95_ms": passed_latencies[int((len(passed_latencies) - 1) * .95)] if passed_latencies else None,
            "missing_turn_observations": len(turns) - len(observed),
            "intent_embedding_sources": dict(Counter(o["intent_embedding_source"] for o in observed
                                                     if o.get("intent_embedding_source"))),
        })
        runs[variant] = {"summary": aggregate["summary"],
                         "metrics": aggregate["metrics"],
                         "cases": items, "raw_report": path.relative_to(root).as_posix()}
    for case_id in case_ids:
        if len({run["cases"][case_id]["family_id"] for run in runs.values()}) != 1:
            raise ValueError(f"case family mismatch: {case_id}")
    return common, runs, case_ids


def compare(root, contrast="intent_mode"):
    common, runs, case_ids = load_comparison(root, contrast)

    def status(variant, case_id):
        return runs[variant]["cases"][case_id]["status"]

    differences = {}
    pairs = (("rules_only", "llm_rules"), ("llm_rules", "full")) if contrast == "intent_mode" else (("full", "full_off"),)
    for before, after in pairs:
        prefix = {("rules_only", "llm_rules"): "rules_to_llm", ("llm_rules", "full"): "llm_to_full",
                  ("full", "full_off"): "on_to_off"}[before, after]
        differences[prefix + "_improved"] = sorted(cid for cid in case_ids if status(before, cid) != "flow_pass_semantic_pending" and status(after, cid) == "flow_pass_semantic_pending")
        differences[prefix + "_regressed"] = sorted(cid for cid in case_ids if status(before, cid) == "flow_pass_semantic_pending" and status(after, cid) != "flow_pass_semantic_pending")
    comparison = {
        "suite": "dian_logistics_v1", "split": "dev",
        "dataset_sha256": common["dataset_sha256"],
        "image_id": common["target"]["image_id"],
        "provenance": common,
        "varied_configuration": "target.runtime.configuration." + contrast,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variants": {name: {key: value for key, value in run.items() if key != "cases"}
                     for name, run in runs.items()},
        "case_differences": differences,
        "interpretation_limit": "Flow assertions only; semantic review and frozen test evaluation pending.",
    }
    return comparison


def paired_difference(rounds, baseline, candidate):
    """Sample whole scenario families, keeping related cases and repeated runs together."""
    effects = defaultdict(list)
    passed = "flow_pass_semantic_pending"
    for runs in rounds:
        for case_id, before in sorted(runs[baseline]["cases"].items()):
            after = runs[candidate]["cases"][case_id]
            effects[before["family_id"]].append(int(after["status"] == passed) - int(before["status"] == passed))
    clusters = [(sum(effects[key]), len(effects[key])) for key in sorted(effects)]
    rng = random.Random(20261117)
    samples = []
    for _ in range(4000):
        draw = rng.choices(clusters, k=len(clusters))
        samples.append(sum(delta for delta, _ in draw) / sum(count for _, count in draw))
    samples.sort()
    return {
        "paired_cases": sum(count for _, count in clusters),
        "delta": sum(delta for delta, _ in clusters) / sum(count for _, count in clusters),
        "descriptive_95pct_family_interval": [samples[100], samples[3899]],
        "families": len(clusters),
    }


def compare_rounds(root, count, contrast):
    paired = []
    common = None
    families = None
    for number in range(1, count + 1):
        identity, runs, case_ids = load_comparison(root / f"r{number:02d}", contrast)
        current_families = {cid: runs[CONTRASTS[contrast][0]]["cases"][cid]["family_id"] for cid in case_ids}
        if common is not None and (identity != common or current_families != families):
            raise ValueError(f"experiment conditions differ across rounds at r{number:02d}")
        common, families = identity, current_families
        paired.append(runs)
    names = CONTRASTS[contrast]
    return {
        "varied_configuration": "target.runtime.configuration." + contrast,
        "provenance": common,
        "rounds": [{name: {k: v for k, v in run.items() if k != "cases"} for name, run in runs.items()} for runs in paired],
        "paired_flow_pass": {f"{candidate}_minus_{baseline}": paired_difference(paired, baseline, candidate)
                             for baseline, candidate in zip(names, names[1:])},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contrast", choices=CONTRASTS, default="intent_mode")
    parser.add_argument("--rounds", type=int, help="compare r01 through rNN under the root")
    args = parser.parse_args()
    try:
        if args.rounds is not None and args.rounds < 1:
            raise ValueError("--rounds must be positive")
        result = compare(args.root, args.contrast) if args.rounds is None else compare_rounds(args.root, args.rounds, args.contrast)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
