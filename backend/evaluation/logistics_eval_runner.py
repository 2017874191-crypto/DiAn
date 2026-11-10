"""Replay DiAn case suites against an isolated HTTP deployment.

The runner creates demo cases and tickets. Point --db at the SQLite file used by
the target API, in a disposable environment. It never changes the dataset gold.
"""
import argparse
import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evaluation/datasets/dian_logistics_v1"


def load_cases(split):
    path = SUITE / ("all.jsonl" if split == "all" else f"{split}.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def check_fixtures(case):
    setup = case["setup"]
    for name in ("fixtures", "policy"):
        path = (ROOT / setup[f"{name}_file"]).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError(f"{name} path escapes repository")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != setup[f"{name}_sha256"]:
            raise ValueError(f"{name} hash differs from suite: {case['case_id']}")


def ticket_count(db_path, user_id, conv_id):
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        return db.execute("SELECT COUNT(*) FROM tickets t JOIN cases c ON c.id=t.case_id "
                          "WHERE c.user_id=? AND c.conv_id=?", (user_id, conv_id)).fetchone()[0]


def evaluate_turn(expected, response, snapshot, trace, count, replay_id=None):
    failures = []
    if response.get("status") != "success":
        return ["model_or_service_failure: " + str(response.get("error_code") or response.get("status"))]
    view = response.get("logistics_case")
    if view != snapshot:
        failures.append("case_snapshot_mismatch")
    state = (view or {}).get("state")
    issue = (view or {}).get("issue")
    if state != expected["case_state"]:
        failures.append(f"case_state: {state!r} != {expected['case_state']!r}")
    if issue != expected["case_issue"]:
        failures.append(f"case_issue: {issue!r} != {expected['case_issue']!r}")
    agents = set(response.get("agent_types") or [response.get("agent_type")])
    if not agents.intersection(expected["agent_any_of"]):
        failures.append(f"agent: {sorted(agents)}")
    if count != expected["ticket_count"]:
        failures.append(f"ticket_count: {count} != {expected['ticket_count']}")
    if not trace.get("found") or trace.get("trace", {}).get("request_id") != response.get("request_id"):
        failures.append("tool_trace_missing")
    calls = trace.get("trace", {}).get("tool_calls", [])
    names = [call.get("tool_name") for call in calls if call.get("tool_name")]
    if replay_id is not None:
        if response.get("request_id") != replay_id:
            failures.append("replay_receipt_changed")
    else:
        for name in expected["required_tools"]:
            if name not in names:
                failures.append("required_tool_missing: " + name)
        for name in expected["forbidden_tools"]:
            if name in names:
                failures.append("forbidden_tool_called: " + name)
    return failures


def classify(failures):
    if not failures:
        return "pass"
    if any(x.startswith("model_or_service_failure") for x in failures):
        return "model_or_service_failure"
    if any(x.startswith("environment:") for x in failures):
        return "environment_failure"
    return "flow_failure"


def run_cases(cases, client, db_path, restart=None):
    results = []
    for case in cases:
        conv_id = "eval-" + uuid.uuid4().hex
        user_id = case["setup"]["user_id"]
        prior = []
        case_results = []
        if "controlled_service_restart" in case["requirements"] and restart is None:
            results.append({"case_id": case["case_id"], "family_id": case["family_id"],
                            "status": "unsupported", "reason": "controlled_service_restart unavailable", "turns": []})
            continue
        try:
            check_fixtures(case)
        except (OSError, ValueError) as exc:
            results.append({"case_id": case["case_id"], "family_id": case["family_id"],
                            "status": "environment_failure", "reason": str(exc), "turns": []})
            continue
        for index, turn in enumerate(case["turns"]):
            replay = turn.get("replay_of_turn")
            client_id = prior[replay]["client_id"] if replay is not None else "eval-" + uuid.uuid4().hex
            if turn.get("before_event") == "restart_services":
                try:
                    restart()
                except Exception as exc:
                    case_results.append({"turn": index, "status": "environment_failure", "failures": ["environment: restart failed: " + str(exc)]})
                    break
            try:
                started = time.monotonic()
                response_http = client.post("/chat", json={"message": turn["message"], "user_id": user_id,
                                                "conv_id": conv_id, "client_request_id": client_id})
                if response_http.status_code == 503:
                    error = response_http.json()
                    failures = ["model_or_service_failure: " + str(error.get("error_code") or error.get("status") or "HTTP 503")]
                    performance = error.get("performance") or {}
                    try:
                        failed_ticket_count = ticket_count(db_path, user_id, conv_id)
                    except (OSError, sqlite3.Error):
                        failed_ticket_count = None
                    case_results.append({"turn": index, "status": classify(failures), "failures": failures,
                                         "semantic_status": "pending_human_review",
                                         "observed": {"http_status": 503, "error_code": error.get("error_code"),
                                                      "request_id": error.get("request_id"),
                                                      "agent_type": error.get("agent_type"),
                                                      "intent": error.get("intent"),
                                                      "intent_embedding_source": error.get("intent_embedding_source"),
                                                      "intent_source_scores": error.get("intent_source_scores", {}),
                                                      "ticket_count": failed_ticket_count,
                                                      "unexpected_ticket_count": max(0, failed_ticket_count - turn["expected"]["ticket_count"]) if failed_ticket_count is not None else 0,
                                                      "latency_ms": error.get("latency_ms"),
                                                      "wall_latency_ms": round((time.monotonic() - started) * 1000, 1),
                                                      "llm_usage": performance.get("llm", {}),
                                                      "llm_calls": [{"stage": call.get("stage"), "status": call.get("status"),
                                                                     "model": call.get("model"), "latency_ms": call.get("latency_ms"),
                                                                     "error_type": call.get("error_type")}
                                                                    for call in performance.get("llm_calls", [])]}})
                    break
                if response_http.status_code != 200:
                    raise RuntimeError(f"chat HTTP {response_http.status_code}: {response_http.text[:200]}")
                response = response_http.json()
                snapshot_http = client.get("/logistics/case", params={"user_id": user_id, "conv_id": conv_id})
                snapshot_http.raise_for_status()
                snapshot = snapshot_http.json()["logistics_case"]
                trace_http = client.get("/trace/tool/" + response["request_id"])
                trace_http.raise_for_status()
                trace = trace_http.json()
                count = ticket_count(db_path, user_id, conv_id)
                replay_id = prior[replay]["request_id"] if replay is not None else None
                failures = evaluate_turn(turn["expected"], response, snapshot, trace, count, replay_id)
                previous = prior[replay] if replay is not None else (prior[-1] if prior else None)
                previous_view = (previous or {}).get("snapshot") or {}
                same_case = replay is not None or turn.get("before_event") == "restart_services"
                if not same_case and previous_view and snapshot:
                    same_case = (previous_view.get("issue") == snapshot.get("issue")
                                 and (not previous_view.get("order_id") or previous_view.get("order_id") == snapshot.get("order_id"))
                                 and not re.search(r"(?:换个|新的|另一个)(?:问题|订单)|另一笔订单", turn["message"]))
                if same_case:
                    if (snapshot or {}).get("case_id") != previous_view.get("case_id"):
                        failures.append("case_id_changed")
                    if previous_view.get("ticket_id") and (snapshot or {}).get("ticket_id") != previous_view["ticket_id"]:
                        failures.append("ticket_id_changed")
                observed = {"response": response.get("response", ""),
                            "agent_type": response.get("agent_type"),
                            "intent": response.get("intent"),
                            "intent_embedding_source": response.get("intent_embedding_source"),
                            "intent_source_scores": response.get("intent_source_scores", {}),
                            "case_state": (snapshot or {}).get("state"),
                            "case_issue": (snapshot or {}).get("issue"),
                            "case_id": (snapshot or {}).get("case_id"),
                            "ticket_id": (snapshot or {}).get("ticket_id"),
                            "ticket_count": count,
                            "unexpected_ticket_count": max(0, count - turn["expected"]["ticket_count"]),
                            "latency_ms": response.get("latency_ms"),
                            "wall_latency_ms": round((time.monotonic() - started) * 1000, 1),
                            "llm_usage": (response.get("performance") or {}).get("llm", {}),
                            "tools": [call.get("tool_name") for call in trace.get("trace", {}).get("tool_calls", []) if call.get("tool_name")],
                            "request_id": response.get("request_id")}
                prior.append({"client_id": client_id, "request_id": response["request_id"], "snapshot": snapshot})
            except (httpx.HTTPError, OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
                failures = ["environment: " + str(exc)]
                observed = None
                prior.append({"client_id": client_id, "request_id": None, "snapshot": None})
            case_results.append({"turn": index, "status": classify(failures), "failures": failures,
                                 "semantic_status": "pending_human_review", "observed": observed})
            if failures:
                break
        status = next((item["status"] for item in case_results if item["status"] != "pass"), "flow_pass_semantic_pending")
        results.append({"case_id": case["case_id"], "family_id": case["family_id"], "status": status, "turns": case_results})
    return results


def report(results, split, variant=None):
    by_family = defaultdict(Counter)
    observations = [turn["observed"] for item in results for turn in item["turns"] if turn.get("observed")]
    latencies = sorted(float(o["wall_latency_ms"]) for o in observations if isinstance(o.get("wall_latency_ms"), (int, float)))
    usage = {field: sum((o.get("llm_usage") or {}).get(field, 0) for o in observations)
             for field in ("calls", "errors", "usage_reported_calls", "input_tokens", "output_tokens")}
    def percentile(p):
        return latencies[min(len(latencies) - 1, int((len(latencies) - 1) * p))] if latencies else None
    for item in results:
        by_family[item["family_id"]][item["status"]] += 1
    return {"suite": "dian_logistics_v1", "split": split, "generated_at": datetime.now(timezone.utc).isoformat(),
            "variant": variant,
            "summary": dict(Counter(item["status"] for item in results)),
            "metrics": {"cases_total": len(results),
                        "flow_pass_cases": sum(item["status"] == "flow_pass_semantic_pending" for item in results),
                        "unexpected_tickets": sum(max(((turn.get("observed") or {}).get("unexpected_ticket_count", 0)
                                                        for turn in item["turns"]), default=0) for item in results),
                        "unexpected_ticket_turns": sum(o.get("unexpected_ticket_count", 0) > 0 for o in observations),
                        "observed_turns": len(observations), "wall_latency_p50_ms": percentile(0.5),
                        "wall_latency_p95_ms": percentile(0.95),
                        "llm_usage": usage,
                        "monetary_cost": "unavailable: provider prices and unreported usage are not estimated"},
            "by_family": {key: dict(value) for key, value in sorted(by_family.items())},
            "semantic_evaluation": "pending_human_review", "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--case-id", action="append", help="run only this case ID (repeatable)")
    parser.add_argument("--base-url", required=True, help="isolated DiAn API URL")
    parser.add_argument("--db", required=True, type=Path, help="SQLite path used by the isolated API")
    parser.add_argument("--token", help="test user Bearer token, if authentication is enabled")
    parser.add_argument("--restart-command", help="controlled restart command for an isolated test deployment")
    parser.add_argument("--output", type=Path, help="JSON report path")
    parser.add_argument("--variant", choices=("rules_only", "llm_rules", "full"), help="intent recognition branch for this deployment")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("--db must point to the target API's existing SQLite file")
    headers = {"Authorization": "Bearer " + args.token} if args.token else {}
    cases = load_cases(args.split)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in selected]
        if len(cases) != len(selected):
            parser.error("some --case-id values are not present in the selected split")
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=60) as client:
        def restart():
            subprocess.run(shlex.split(args.restart_command), check=True, timeout=180)
            for _ in range(30):
                try:
                    if client.get("/health", timeout=3).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            raise RuntimeError("API did not become healthy after restart")
        results = run_cases(cases, client, args.db,
                            restart=restart if args.restart_command else None)
    data = report(results, args.split, args.variant)
    output = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if data["summary"] == {"flow_pass_semantic_pending": len(results)} else 1


if __name__ == "__main__":
    sys.exit(main())
