import pytest

from core.intent_recognizer import IntentCategory, IntentRecognizer


@pytest.mark.asyncio
async def test_task_goal_uses_same_intent_call_and_legacy_outputs_remain_valid():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    r = make_recognizer()
    r.model = "fixture"
    r.client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=[
        SimpleNamespace(content=[{"type": "text", "text": '{"intent":"refund","confidence":0.9,"task_goal":"status_lookup"}'}]),
        SimpleNamespace(content=[{"type": "text", "text": '{"intent":"refund","confidence":0.9}'}]),
    ])))
    assert (await r._llm_recognize("退款通过了吗", None))["task_goal"] == "status_lookup"
    assert (await r._llm_recognize("旧响应", None))["task_goal"] is None
    assert r.client.messages.create.await_count == 2


def make_recognizer(threshold=0.5):
    recognizer = IntentRecognizer.__new__(IntentRecognizer)
    recognizer._embedding_enabled = True
    recognizer.threshold = threshold
    return recognizer


def vote(recognizer, llm, emb, pat):
    return recognizer._vote(
        {"intent": llm[0], "confidence": llm[1]},
        {"intent": emb[0], "confidence": emb[1]},
        {"intent": pat[0], "confidence": pat[1]},
    )


def test_pattern_cannot_refine_a_different_intent_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.BILLING, 0.9),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.TECHNICAL_LOGIN, 0.5),
    )

    assert intent is IntentCategory.BILLING
    assert confidence == pytest.approx(0.63)
    assert "refined_by_pattern" not in sources


def test_coarse_and_fine_scores_choose_the_same_group_together():
    intent, confidence, sources = vote(
        make_recognizer(threshold=0.2),
        (IntentCategory.BILLING, 0.3),       # weighted score 0.21
        (IntentCategory.TECHNICAL_LOGIN, 0.9),  # weighted score 0.18
        (IntentCategory.TECHNICAL, 0.4),     # weighted score 0.04
    )

    assert intent is IntentCategory.TECHNICAL_LOGIN
    assert confidence == pytest.approx(0.22)
    assert "refined_by_pattern" not in sources


def test_pattern_can_still_refine_within_the_winning_group():
    intent, confidence, sources = vote(
        make_recognizer(),
        (IntentCategory.TECHNICAL, 0.8),
        (IntentCategory.OTHER, 0.0),
        (IntentCategory.TECHNICAL_LOGIN, 0.5),
    )

    assert intent is IntentCategory.TECHNICAL_LOGIN
    assert confidence == pytest.approx(0.61)
    assert sources["refined_by_pattern"] == pytest.approx(0.5)
