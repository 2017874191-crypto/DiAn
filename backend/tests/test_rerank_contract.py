from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.performance import collect_performance, performance_snapshot
from tooling.tool_manager import MCPToolManager


@pytest.mark.asyncio
@pytest.mark.parametrize("text,stop_reason,reason", [
    ("[]", "end_turn", "empty_order"),
    ("```json\n[]\n```", "end_turn", "empty_order"),
    ("", "end_turn", "no_text"),
    ("[0,", "end_turn", "invalid_json"),
    ('{"order": 1}', "end_turn", "invalid_json"),
    ("[true, 1]", "end_turn", "invalid_index"),
    ("[0.0, 1]", "end_turn", "invalid_index"),
    ('["0", 1]', "end_turn", "invalid_index"),
    ("[-1, 1]", "end_turn", "invalid_index"),
    ("[3, 1]", "end_turn", "invalid_index"),
    ("[0, 1, 2]", "max_tokens", "truncated"),
])
async def test_invalid_ranking_preserves_retrieval_order_and_records_safe_reason(text, stop_reason, reason):
    response = SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason=stop_reason)
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    manager = MCPToolManager(api_key="fixture", client=client)
    items = ["first", "second", "third"]
    with collect_performance():
        ranked, accepted = await manager._rerank_with_status("query", items, 2)
        perf = performance_snapshot()
    assert not accepted and ranked == items[:2]
    assert perf["events"] == {f"rag_rerank_fallback_{reason}": 1}
    assert perf["llm_calls"][0]["status"] == "success"
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected,repaired", [
    ("```json\n[2,0,1]\n```", ["third", "first"], False),
    ("[2]", ["third", "first"], True),
    ("[2,2]", ["third", "first"], True),
])
async def test_partial_repairs_remain_visible_separately_from_complete_rankings(text, expected, repaired):
    response = SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason="end_turn")
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    manager = MCPToolManager(api_key="fixture", client=client)
    with collect_performance():
        ranked, accepted = await manager._rerank_with_status("query", ["first", "second", "third"], 2)
        events = performance_snapshot()["events"]
    assert accepted and ranked == expected
    assert events.get("rag_rerank_repaired", 0) == int(repaired)
    assert events["rag_rerank_accepted"] == 1


@pytest.mark.asyncio
async def test_transport_errors_and_small_pools_have_distinct_outcomes_without_extra_requests():
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("sk-private"))))
    manager = MCPToolManager(api_key="fixture", client=client)
    with collect_performance():
        assert await manager._rerank_with_status("q", ["a", "b", "c"], 2) == (["a", "b"], False)
        assert await manager._rerank_with_status("q", ["a"], 2) == (["a"], False)
        perf = performance_snapshot()
    assert perf["events"] == {"rag_rerank_fallback_model_error": 1, "rag_rerank_skipped_small_pool": 1}
    assert "sk-private" not in str(perf)
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_prompt_defines_all_candidate_ranking_even_when_sources_are_unrelated():
    response = SimpleNamespace(content=[{"type": "text", "text": "[1,0,2]"}], stop_reason="end_turn")
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    manager = MCPToolManager(api_key="fixture", client=client)
    await manager._rerank_with_status('query "with quotes"', ["unrelated", "source", "other"], 2)
    prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "即使所有候选都不相关" in prompt and "每个索引各一次" in prompt
    assert "打分（0-10）" not in prompt
    assert "0. " in prompt and "1. " in prompt and "2. " in prompt


@pytest.mark.asyncio
async def test_paired_runner_shares_candidates_and_parser_without_exposing_gold():
    from evaluation.compare_rerank_contract import run_case, summarize
    responses = [SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason="end_turn")
                 for text in ("[]", "[2,0,1,3,4]")]
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=responses)))
    manager = MCPToolManager(api_key="fixture", client=client)
    query = {"id": "q", "query": "public query", "qrels": {"secret-gold": 1}}
    items = [{"document_id": str(i), "title": "source", "content": "public passage"} for i in range(5)]
    rows = [await run_case(manager, query, items, arm) for arm in ("legacy", "current")]
    assert rows[0]["candidate_sha256"] == rows[1]["candidate_sha256"]
    assert not rows[0]["reranked"] and rows[1]["reranked"]
    assert rows[0]["performance"]["events"]["rag_rerank_fallback_empty_order"] == 1
    assert rows[1]["ranked_ids"] == ["2", "0", "1"]
    assert all("secret-gold" not in call.kwargs["messages"][0]["content"]
               for call in client.messages.create.call_args_list)
    summary = summarize(rows)
    assert summary["legacy"]["accepted"] == 0 and summary["current"]["complete_without_repair"] == 1
    assert summary["legacy"]["model_calls"] == summary["current"]["model_calls"] == 1
