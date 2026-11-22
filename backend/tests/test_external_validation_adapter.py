from evaluation.run_external_validation import FixturePorts, chat_payload, next_turn_allowed
from evaluation.grade_external_validation import validate_replay
import pytest


def test_runtime_payload_keeps_authored_confirmation_and_excludes_author_metadata():
    text = "我确认申请这个订单的核查。"
    payload = chat_payload(text, "actor", "opaque-random-conversation")
    assert payload["message"] == text
    assert set(payload) == {"message", "user_id", "conv_id", "client_request_id"}


def test_disabled_capability_and_wrong_owner_never_disclose_a_record():
    task = {"available_capabilities": {"order_ownership_verify": {"enabled": True}}}
    fixture = {"initial_world_state": {"order_id": "ORDER", "actual_owner": "owner"},
               "authoritative_tool_results": []}
    denied = FixturePorts(task, fixture, "other").query_order("other", "ORDER")
    assert denied["success"] is False and "order" not in denied
    blocked = FixturePorts(task, fixture, "owner").query_logistics("owner", "ORDER")
    assert blocked["success"] is False and "tracking" not in blocked


def test_conditional_turns_require_observed_confirmation_or_ticket_state():
    assert not next_turn_allowed(0, {"response": "请确认模拟核查"}, 0)
    assert next_turn_allowed(0, {"response": "如同意模拟核查，请确认", "logistics_case": {
        "state": "awaiting_confirmation"}}, 0)
    assert not next_turn_allowed(1, {"logistics_case": {"state": "ticket_created"}}, 0)
    assert next_turn_allowed(1, {"logistics_case": {"state": "ticket_created"}}, 1)


def test_checkpoint_grading_preserves_interruption_and_rejects_unfinished_cases():
    case = {"case_id": "opaque", "turns": [{}], "ticket_count": 0,
            "rule_checks": {}, "fixture_events": [], "skipped_script_turns": []}
    replay = {"complete": False, "cases": [case]}
    with pytest.raises(ValueError):
        validate_replay(replay, {"opaque": {}}, False)
    validate_replay(replay, {"opaque": {}}, True)
    assert replay["complete"] is False
    del case["ticket_count"]
    with pytest.raises(ValueError):
        validate_replay(replay, {"opaque": {}}, True)
