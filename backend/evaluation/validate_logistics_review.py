"""Check independent human review sheets against the frozen test split."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from evaluation.logistics_eval_runner import SUITE, load_cases
from evaluation.prepare_logistics_review import rows

VERDICTS = {"accept", "revise", "uncertain"}
IDENTITY_FIELDS = ("case_id", "family_id", "turn", "message", "expected_state",
                   "expected_issue", "expected_ticket_count")


def load_sheet(path, slot, expected):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        prefix = f"reviewer_{slot}"
        required = set(IDENTITY_FIELDS) | {prefix, f"{prefix}_verdict", f"{prefix}_reason"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"reviewer {slot} sheet has missing columns")
        actual = list(reader)
    if len(actual) != len(expected):
        raise ValueError(f"reviewer {slot} sheet has {len(actual)} rows; expected {len(expected)}")
    for index, (row, gold) in enumerate(zip(actual, expected)):
        for field in IDENTITY_FIELDS:
            value = "" if gold[field] is None else str(gold[field])
            if row[field] != value:
                raise ValueError(f"reviewer {slot} row {index + 1} changed {field}")
        verdict = row[f"{prefix}_verdict"].strip().lower()
        name = row[prefix].strip()
        reason = row[f"{prefix}_reason"].strip()
        if verdict and verdict not in VERDICTS:
            raise ValueError(f"reviewer {slot} row {index + 1} has invalid verdict")
        if verdict and not name:
            raise ValueError(f"reviewer {slot} row {index + 1} has no reviewer identity")
        if verdict in {"revise", "uncertain"} and not reason:
            raise ValueError(f"reviewer {slot} row {index + 1} needs a reason")
    return actual


def validate(first, second):
    expected = list(rows(load_cases("test")))
    one = load_sheet(first, 1, expected)
    two = load_sheet(second, 2, expected)
    names_one = {row["reviewer_1"].strip() for row in one if row["reviewer_1_verdict"].strip()}
    names_two = {row["reviewer_2"].strip() for row in two if row["reviewer_2_verdict"].strip()}
    if names_one & names_two:
        raise ValueError("reviewer identities overlap across independent sheets")
    complete_one = sum(bool(row["reviewer_1_verdict"].strip()) for row in one)
    complete_two = sum(bool(row["reviewer_2_verdict"].strip()) for row in two)
    paired = [(a["reviewer_1_verdict"].strip().lower(), b["reviewer_2_verdict"].strip().lower())
              for a, b in zip(one, two) if a["reviewer_1_verdict"].strip() and b["reviewer_2_verdict"].strip()]
    agreements = sum(a == b for a, b in paired)
    return {"split": "test", "dataset_sha256": hashlib.sha256((SUITE / "test.jsonl").read_bytes()).hexdigest(),
            "cases": len(load_cases("test")), "turns": len(expected),
            "reviewer_1_completed": complete_one, "reviewer_2_completed": complete_two,
            "paired_turns": len(paired), "agreements": agreements,
            "disagreements": len(paired) - agreements,
            "ready_for_adjudication": complete_one == complete_two == len(expected),
            "gold_frozen": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", required=True, type=Path)
    parser.add_argument("--second", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.first, args.second), ensure_ascii=False))


if __name__ == "__main__":
    main()
