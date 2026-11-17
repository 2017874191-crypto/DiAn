import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from core import logistics
from core.intent_recognizer import IntentCategory, IntentRecognizer
from evaluation.compare_intent_faults import VARIANTS, replay
from evaluation.logistics_eval_runner import ROOT, load_cases
from evaluation.provenance import build_identity

CLARIFICATION = "意图模型不可用，先澄清是否需要丢件核查"


def dev_case(case_id):
    return deepcopy(next(case for case in load_cases("dev") if case["case_id"] == case_id))


def input_case(*, message=None):
    case = dev_case("DIAN-025")
    no_case = dev_case("DIAN-019")["turns"][0]["expected"]
    case["turns"][0]["expected"] = no_case
    if message is not None:
        case["turns"][0]["message"] = message
    case["turns"] = case["turns"][:1]
    return case


@pytest.fixture(scope="module")
def build():
    return build_identity(ROOT)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", VARIANTS)
async def test_missing_delivery_request_clarifies_when_both_models_disconnect(build, variant, monkeypatch):
    with monkeypatch.context() as control:
        control.setattr(logistics, "needs_missing_delivery_clarification", lambda *_: False)
        old = await replay(input_case(), *VARIANTS[variant], ("intent", "reply"), build)
    row = await replay(input_case(), *VARIANTS[variant], ("intent", "reply"), build)
    assert old["turns"][0]["http_status"] == 503
    assert any(attempt["stage"] == "reply" for attempt in old["model_attempts"])
    assert row["turns"][0]["http_status"] == 200
    assert row["turns"][0]["routing_reason"] == CLARIFICATION
    assert row["cases_created"] == row["ticket_count"] == 0
    assert row["model_attempts"] == [{"turn": 0, "stage": "intent", "outcome": "connect_error", "count": 1}]
    keys = ("turns", "cases_created", "ticket_count", "model_attempts")
    print("clarification-proof: " + json.dumps({
        "variant": variant, "without_clarification": {key: old[key] for key in keys},
        "with_clarification": {key: row[key] for key in keys},
    }))


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "订单号 DEMO1001，我只是查物流进度。",
    "订单号 DEMO1001，没收到货，能核实是否遗失吗？另问配送费用。",
    "不是没收到货，能核实是否遗失吗？订单号 DEMO1001。",
    "没收到货，但不需要帮我核实是否遗失。订单号 DEMO1001。",
    "订单号 DEMO1001，没收到货，但先别核实是否遗失。",
    "之前没收到货，我已经收到。能核实是否遗失吗？订单号 DEMO1001。",
    "没收到货，能核实是否遗失吗？",
    "没收到货，能核实是否遗失吗？订单号 DEMO1001 或订单号 DEMO1002。",
])
async def test_near_matches_do_not_enter_targeted_clarification(build, message):
    row = await replay(input_case(message=message), "full", True, ("intent",), build)
    assert row["turns"][0]["routing_reason"] != CLARIFICATION
    assert row["cases_created"] == row["ticket_count"] == 0


@pytest.mark.asyncio
async def test_clarification_does_not_grant_confirmation(build):
    case = input_case()
    premature = deepcopy(dev_case("DIAN-025")["turns"][1])
    premature["expected"] = {
        **premature["expected"], "case_state": "awaiting_info", "ticket_count": 0,
        "required_tools": [], "forbidden_tools": ["create_logistics_case"],
    }
    case["turns"].append(premature)
    # A healthy model after the outage deliberately classifies the bare confirmation as logistics_lost.
    row = await replay(case, "full", True, ("intent", "reply"), build)
    assert row["status"] == "flow_pass_semantic_pending"
    assert row["turns"][0]["case_state"] is None
    assert row["turns"][1]["case_state"] == "awaiting_info"
    assert row["ticket_count"] == 0


@pytest.mark.asyncio
async def test_explicit_restatement_then_confirmation_works_during_outage(build):
    case = input_case()
    verify, confirm = dev_case("DIAN-025")["turns"]
    verify["message"] = "订单号 DEMO1001，我怀疑包裹丢件了。"
    case["turns"].extend([verify, confirm])
    row = await replay(case, "full", True, ("intent", "reply"), build, fault_turns=(0, 1, 2))
    assert row["status"] == "flow_pass_semantic_pending"
    assert [turn["case_state"] for turn in row["turns"]] == [None, "awaiting_confirmation", "ticket_created"]
    assert [turn["ticket_count"] for turn in row["turns"]] == [0, 0, 1]
    assert {attempt["stage"] for attempt in row["model_attempts"]} == {"intent"}
    print("clarification-sequence: " + json.dumps(row["turns"]))


@pytest.mark.asyncio
async def test_existing_case_keeps_its_state_when_model_disconnects(build):
    case = dev_case("DIAN-025")
    verify = case["turns"][0]
    ambiguous = input_case()["turns"][0]
    verify["message"] = "订单号 DEMO1001，我怀疑包裹丢件了。"
    ambiguous["expected"].update(case_state="awaiting_confirmation", case_issue="logistics_lost")
    case["turns"] = [verify, ambiguous]
    row = await replay(case, "full", True, ("intent",), build, fault_turns=(1,))
    assert row["turns"][1]["routing_reason"] != CLARIFICATION
    assert row["turns"][1]["case_state"] == "awaiting_confirmation"
    assert row["cases_created"] == 1 and row["ticket_count"] == 0


@pytest.mark.asyncio
async def test_healthy_model_and_disabled_model_keep_their_existing_paths(build):
    healthy = await replay(dev_case("DIAN-025"), "full", True, (), build)
    assert healthy["status"] == "flow_pass_semantic_pending"
    assert healthy["turns"][0]["case_state"] == "awaiting_confirmation"
    assert healthy["turns"][0]["routing_reason"] != CLARIFICATION

    disabled = await replay(input_case(), "rules_only", True, ("intent", "reply"), build)
    assert disabled["turns"][0]["routing_reason"] != CLARIFICATION
    assert disabled["model_attempts"] == []


@pytest.mark.asyncio
async def test_intent_does_not_cache_degraded_result(monkeypatch):
    monkeypatch.setenv("INTENT_RECOGNITION_MODE", "llm_rules")
    recognizer = IntentRecognizer(api_key="local-test-only", model="test")
    recognize = AsyncMock(side_effect=[
        {"intent": IntentCategory.OTHER, "confidence": 0, "failed": True},
        {"intent": IntentCategory.LOGISTICS_LOST, "confidence": .97},
    ])
    monkeypatch.setattr(recognizer, "_llm_recognize", recognize)
    message = input_case()["turns"][0]["message"]
    try:
        assert (await recognizer.recognize(message)).llm_failed
        recovered = await recognizer.recognize(message)
        assert recovered.intent is IntentCategory.LOGISTICS_LOST and not recovered.llm_failed
        assert await recognizer.recognize(message) is recovered
        assert recognize.await_count == 2
    finally:
        await recognizer.client.close()
