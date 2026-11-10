"""Export an independent two-reviewer sheet for a frozen DiAn case split."""
import argparse
import csv
import json
from pathlib import Path

from evaluation.logistics_eval_runner import load_cases


FIELDS = [
    "case_id", "family_id", "title", "origin_kind", "source_record", "turn",
    "message", "expected_state", "expected_issue", "expected_agents",
    "expected_ticket_count", "required_tools", "forbidden_tools", "semantic_checks",
    "observed_response", "observed_state", "observed_agent", "observed_ticket_count",
    "observed_tools", "reviewer_1", "reviewer_1_verdict", "reviewer_1_reason",
    "reviewer_2", "reviewer_2_verdict", "reviewer_2_reason",
    "adjudicator", "final_verdict", "final_reason",
]


def fields_for_reviewer(slot=None):
    if slot is None:
        return FIELDS
    prefix = f"reviewer_{slot}"
    return FIELDS[:FIELDS.index("reviewer_1")] + [prefix, f"{prefix}_verdict", f"{prefix}_reason"]


def rows(cases, report=None):
    observed = {item["case_id"]: item for item in (report or {}).get("cases", [])}
    for case in cases:
        for index, turn in enumerate(case["turns"]):
            expected = turn["expected"]
            actual = next((item.get("observed") for item in observed.get(case["case_id"], {}).get("turns", [])
                           if item["turn"] == index), None) or {}
            yield {
                "case_id": case["case_id"], "family_id": case["family_id"], "title": case.get("title", ""),
                "origin_kind": case["origin_kind"],
                "source_record": (case.get("provenance") or {}).get("record_id", ""),
                "turn": index, "message": turn["message"],
                "expected_state": expected["case_state"], "expected_issue": expected["case_issue"],
                "expected_agents": ", ".join(expected["agent_any_of"]),
                "expected_ticket_count": expected["ticket_count"],
                "required_tools": ", ".join(expected["required_tools"]),
                "forbidden_tools": ", ".join(expected["forbidden_tools"]),
                "semantic_checks": " | ".join(expected["semantic_checks"]),
                "observed_response": actual.get("response", ""),
                "observed_state": actual.get("case_state", ""),
                "observed_agent": actual.get("agent_type", ""),
                "observed_ticket_count": actual.get("ticket_count", ""),
                "observed_tools": ", ".join(actual.get("tools", [])),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument("--report", type=Path, help="optional HTTP replay JSON with observed replies")
    parser.add_argument("--reviewer-slot", type=int, choices=(1, 2), help="write an independent sheet for one reviewer")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else None
    if report and report.get("split") != args.split:
        parser.error("report split does not match requested split")
    if args.output.exists():
        parser.error("output already exists; preserve reviewer decisions and choose a new path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields_for_reviewer(args.reviewer_slot), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows(load_cases(args.split), report))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
