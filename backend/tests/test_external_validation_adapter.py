from evaluation.run_external_validation import FixturePorts, chat_payload, next_turn_allowed
from evaluation.run_external_validation import LogicalFixturePorts
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


def test_logical_schema_keeps_ownership_material_and_script_guards():
    task = {"available_capabilities": {"readonly_order_ownership_check": "enabled; fixture on call",
            "record_material_declaration": "enabled; declarations only"}}
    fixture = {"initial_world_state": {"order_id": "ORDER", "order_owner_id": "owner"},
               "authoritative_tool_results": {"entries": [
                   {"logical_capability": "order_ownership_check", "on_call_result": {"order_id": "ORDER", "is_owner": True}},
                   {"logical_capability": "record_material_declaration", "on_call_result": {"recorded": True,
                    "material_names": ["outer", "label", "item"]}}]}}
    ports = LogicalFixturePorts(task, fixture, "owner")
    assert not ports.enabled("mock_ticket_create")
    assert ports.query_order("other", "ORDER")["success"] is False
    assert ports.declared("没有提供照片", False) is False
    ports.query_order("owner", "ORDER")
    reply = {"response": "请确认模拟审核", "logistics_case": {"state": "awaiting_confirmation"}}
    trigger = {"event": "after_agent_has_verified_order_ownership_and_recorded_three_material_declarations_then_explains_proposed_ticket_and_asks_separate_confirmation"}
    assert not next_turn_allowed(0, reply, 0, trigger, ports.events)
    assert ports.declared("已提供照片", False)
    assert next_turn_allowed(0, reply, 0, trigger, ports.events)
    assert not next_turn_allowed(0, reply, 0, {"event": "unknown"}, ports.events)
