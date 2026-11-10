import json
import sqlite3
from pathlib import Path

from evaluation.logistics_eval_runner import evaluate_turn, load_cases, report, run_cases, ticket_count
from evaluation.prepare_logistics_review import rows, fields_for_reviewer


EXPECTED = {"case_state": "ticket_created", "case_issue": "logistics_lost",
            "agent_any_of": ["logistics"], "ticket_count": 1,
            "required_tools": ["create_logistics_case"], "forbidden_tools": ["refund"],
            "semantic_checks": ["人工复核"]}


def test_turn_checks_trace_side_effect_and_replay():
    view = {"case_id": "LC-1", "state": "ticket_created", "issue": "logistics_lost"}
    response = {"status": "success", "request_id": "r1", "agent_type": "logistics", "logistics_case": view}
    trace = {"found": True, "trace": {"request_id": "r1", "tool_calls": [{"tool_name": "create_logistics_case"}]}}
    assert evaluate_turn(EXPECTED, response, view, trace, 1) == []
    assert "ticket_count: 0 != 1" in evaluate_turn(EXPECTED, response, view, trace, 0)
    assert "replay_receipt_changed" in evaluate_turn(EXPECTED, response, view, trace, 1, "other")
    assert "forbidden_tool_called: refund" in evaluate_turn(
        EXPECTED, response, view,
        {"found": True, "trace": {"request_id": "r1", "tool_calls": trace["trace"]["tool_calls"] + [{"tool_name": "refund"}]}}, 1)
    assert evaluate_turn(EXPECTED, {**response, "status": "failed"}, view, trace, 1)[0].startswith("model_or_service_failure")


def test_ticket_count_is_conversation_scoped(tmp_path):
    db = tmp_path / "cases.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE cases(id TEXT, user_id TEXT, conv_id TEXT);"
                           "CREATE TABLE tickets(case_id TEXT);")
        conn.executemany("INSERT INTO cases VALUES (?,?,?)", [("a", "u", "one"), ("b", "u", "two")])
        conn.executemany("INSERT INTO tickets VALUES (?)", [("a",), ("b",)])
    assert ticket_count(db, "u", "one") == 1
    assert ticket_count(db, "u", "two") == 1
    assert ticket_count(db, "other", "one") == 0


def test_restart_without_hook_is_reported_unsupported():
    case = next(item for item in load_cases("all") if item["case_id"] == "DIAN-046")
    result = run_cases([case], None, Path("unused"))
    assert result[0]["status"] == "unsupported"
    assert report(result, "dev")["summary"] == {"unsupported": 1}


def test_restart_detects_changed_ticket_identity(monkeypatch):
    from types import SimpleNamespace
    import evaluation.logistics_eval_runner as runner
    case = next(item for item in load_cases("dev") if item["case_id"] == "DIAN-046")
    monkeypatch.setattr(runner, "check_fixtures", lambda _case: None)
    class Client:
        index = -1
        def post(self, path, json):
            self.index += 1
            return SimpleNamespace(status_code=200, json=lambda: {
                "status": "success", "request_id": f"r{self.index}", "agent_type": "logistics",
                "logistics_case": self.view(), "latency_ms": 1})
        def view(self):
            return {"case_id": "LC-1", "issue": "logistics_lost",
                    "state": "awaiting_confirmation" if self.index == 0 else "ticket_created",
                    "ticket_id": None if self.index == 0 else ("T-1" if self.index == 1 else "T-2")}
        def get(self, path, params=None):
            if path == "/logistics/case":
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"logistics_case": self.view()})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"found": True,
                "trace": {"request_id": f"r{self.index}", "tool_calls": []}})
    client = Client()
    monkeypatch.setattr(runner, "ticket_count", lambda *_args: 0 if client.index == 0 else 1)
    result = run_cases([case], client, Path("unused"), restart=lambda: None)
    assert result[0]["status"] == "flow_failure"
    assert "ticket_id_changed" in result[0]["turns"][-1]["failures"]


def test_failed_http_turn_retains_safe_diagnostics():
    from types import SimpleNamespace
    case = next(item for item in load_cases("dev") if item["case_id"] == "DIAN-029")
    class Client:
        def post(self, path, json):
            return SimpleNamespace(status_code=503, json=lambda: {
                "error_code": "agent_failed", "request_id": "r-failed", "agent_type": "general",
                "performance": {"llm": {"calls": 1, "errors": 1, "input_tokens": 0, "output_tokens": 0},
                                "llm_calls": [{"stage": "agent.general", "status": "error", "model": "test-model",
                                               "latency_ms": 25, "error_type": "TimeoutError", "prompt": "never copy"}]}})
    results = run_cases([case], Client(), Path("unused"))
    observed = results[0]["turns"][0]["observed"]
    assert results[0]["status"] == "model_or_service_failure"
    assert observed["request_id"] == "r-failed"
    assert observed["llm_calls"] == [{"stage": "agent.general", "status": "error", "model": "test-model", "latency_ms": 25, "error_type": "TimeoutError"}]
    assert report(results, "dev")["metrics"]["llm_usage"]["errors"] == 1


def test_review_sheet_links_observation_without_fabricating_verdict():
    case = load_cases("test")[0]
    observation = {"cases": [{"case_id": case["case_id"], "turns": [
        {"turn": 0, "observed": {"response": "模拟核验后请补充材料", "case_state": "awaiting_evidence",
                                  "agent_type": "logistics", "ticket_count": 0, "tools": ["query_order"]}}
    ]}]}
    row = next(rows([case], observation))
    assert row["observed_response"] == "模拟核验后请补充材料"
    assert row["expected_state"] == case["turns"][0]["expected"]["case_state"]
    assert "reviewer_1_verdict" not in row


def test_independent_review_sheets_expose_only_assigned_reviewer():
    first = fields_for_reviewer(1)
    second = fields_for_reviewer(2)
    assert "reviewer_1_verdict" in first and "reviewer_2_verdict" not in first
    assert "reviewer_2_verdict" in second and "reviewer_1_verdict" not in second
    assert "final_verdict" not in first + second


def test_review_validation_requires_independent_complete_unchanged_sheets(tmp_path):
    import csv
    import pytest
    from evaluation.validate_logistics_review import validate
    expected = list(rows(load_cases("test")))
    def write(slot, reviewer, change_gold=False):
        path = tmp_path / f"reviewer-{slot}.csv"
        prefix = f"reviewer_{slot}"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields_for_reviewer(slot), extrasaction="ignore")
            writer.writeheader()
            for index, source in enumerate(expected):
                row = dict(source)
                row[prefix] = reviewer
                row[f"{prefix}_verdict"] = "revise" if slot == 2 and index == 0 else "accept"
                row[f"{prefix}_reason"] = "金标需复核" if slot == 2 and index == 0 else ""
                if change_gold and index == 0:
                    row["expected_state"] = "ticket_created"
                writer.writerow(row)
        return path
    first = write(1, "human-a")
    second = write(2, "human-b")
    summary = validate(first, second)
    assert summary["ready_for_adjudication"] and summary["disagreements"] == 1
    assert not summary["gold_frozen"]
    write(2, "human-a")
    with pytest.raises(ValueError, match="overlap"):
        validate(first, second)
    write(2, "human-b", change_gold=True)
    with pytest.raises(ValueError, match="changed expected_state"):
        validate(first, second)
