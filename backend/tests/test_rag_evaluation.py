import json
import math

import httpx
import pytest
from anthropic import AsyncAnthropic

from evaluation.compare_rag import ARMS, metrics, model_settings, run_arm, trace_model_transport
from core.performance import collect_performance, performance_snapshot
from evaluation.prepare_rag_data import ROOT, business_snapshot, freeze, public_subset, validate


def test_graded_metrics_deduplicate_and_account_for_unjudged_documents():
    qrels = {"a": 2, "b": 1, "irrelevant": 0}
    result = metrics(["b", "b", "unknown", "a"], qrels, 3)
    assert result["recall"] == 1
    assert result["unjudged_fraction"] == pytest.approx(1 / 3)
    assert result["ndcg"] == pytest.approx((1 + 3 / math.log2(4)) / (3 + 1 / math.log2(3)))
    assert metrics([], qrels, 3)["recall"] == metrics([], qrels, 3)["ndcg"] == 0
    assert metrics(["irrelevant"], {"irrelevant": 0}, 3)["recall"] is None


def test_snapshot_rejects_tampering_and_invented_evidence(tmp_path):
    corpus, queries = business_snapshot()
    freeze(tmp_path, corpus, queries, {})
    assert b"\r\n" not in (tmp_path / "corpus.json").read_bytes()
    queries[0]["evidence"][0]["quote"] = "this text is absent"
    with pytest.raises(ValueError, match="Evidence"):
        freeze(tmp_path / "invalid", corpus, queries, {})
    with pytest.raises(FileExistsError, match="new output directory"):
        freeze(tmp_path, corpus, queries, {})
    corpus, queries = business_snapshot()
    freeze(tmp_path, corpus, queries, {})
    (tmp_path / "queries.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        validate(tmp_path)


def test_business_drafts_match_source_chunks_and_have_no_fabricated_reviewers():
    _, corpus, queries = validate(ROOT / "evaluation/datasets/dian_rag_v1")
    current_corpus, current_queries = business_snapshot()
    assert (corpus, queries) == (current_corpus, current_queries)
    assert len(queries) == 30 and sum(query["answerable"] for query in queries) == 27
    assert all(query["review_status"] == "pending_human_review"
               and query["reviewer_1"] is None and query["reviewer_2"] is None for query in queries)


def test_public_sampling_is_order_independent_and_keeps_negative_judgments():
    rows = [{"query_id": str(index), "query": f"query {index}",
             "positive_passages": [{"docid": f"{index}#0", "title": "title", "text": "positive"}],
             "negative_passages": [{"docid": "negative#0", "title": "title", "text": "negative"}]}
            for index in range(6)]
    corpus, queries = public_subset(rows, 3)
    assert (corpus, queries) == public_subset(list(reversed(rows)), 3)
    assert len(corpus) == 4 and all(query["qrels"]["negative#0"] == 0 for query in queries)


@pytest.mark.asyncio
async def test_ablation_uses_production_model_methods_without_sending_gold(monkeypatch):
    from tooling import tool_manager
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        text = payload["messages"][0]["content"]
        sent.append(text)
        assert "secret-gold" not in text
        output = '["rewritten query"]' if "搜索子查询" in text else "[1,0,2,3,4]"
        return httpx.Response(200, json={
            "id": "msg_local", "type": "message", "role": "assistant", "model": "fixture",
            "content": [{"type": "text", "text": output}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    def model_client(**_kwargs):
        return AsyncAnthropic(api_key="local-only", base_url="http://model.fixture", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(tool_manager, "AsyncAnthropic", model_client)

    class Search:
        async def search_async(self, _query, count):
            return [{"document_id": str(index), "title": "title", "content": "source text",
                     "chunk": 0} for index in range(min(count, 5))]

    query = {"id": "q", "query": "a user question", "qrels": {"secret-gold": 1}}
    for arm in ARMS:
        row = await run_arm(Search(), query, arm, {"api_key": "local-only", "model": "fixture"})
        assert row["error"] is None
        assert len(row["ranked_ids"]) == 3
        assert row["metrics"]["3"]["recall"] == 0
        assert all(call["status"] == "success" for call in row["model_calls"])
    assert len(sent) == 5


def test_project_model_configuration_is_atomic_and_does_not_use_stale_inherited_values(tmp_path, monkeypatch):
    import evaluation.compare_rag as evaluation
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    settings_file = tmp_path / ".env"
    settings_file.write_text(
        "ANTHROPIC_API_KEY=file-key\nANTHROPIC_BASE_URL=http://model.fixture\nANTHROPIC_MODEL=file-model\n",
        encoding="utf-8",
    )
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
        monkeypatch.setenv(name, "stale-value")
    settings, source = model_settings()
    assert settings == {"api_key": "file-key", "base_url": "http://model.fixture", "model": "file-model"}
    assert source["kind"] == "project_dotenv" and len(source["shadowed_environment_fields"]) == 3
    assert model_settings(settings_file)[1]["kind"] == "explicit_env_file"
    # A partial file must not silently mix another provider's inherited credential.
    settings_file.write_text("ANTHROPIC_MODEL=partial\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Selected model configuration"):
        model_settings()
    settings_file.unlink()
    settings, source = model_settings()
    assert settings["api_key"] == "stale-value" and source["kind"] == "environment"
    with pytest.raises(FileNotFoundError):
        model_settings(settings_file)


@pytest.mark.asyncio
async def test_shared_evaluation_client_survives_arms_and_keeps_tool_statistics_isolated():
    from tooling.tool_manager import MCPToolManager
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "msg_local", "type": "message", "role": "assistant", "model": "fixture",
            "content": [{"type": "text", "text": "[1,0,2,3,4]"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    class Search:
        async def search_async(self, _query, count):
            return [{"document_id": str(index), "title": "source", "content": "text", "chunk": 0}
                    for index in range(min(count, 5))]

    settings = {"api_key": "local-only", "base_url": "http://model.fixture", "model": "fixture"}
    async with AsyncAnthropic(api_key="local-only", base_url=settings["base_url"], max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        template = MCPToolManager(**settings, client=client)
        for arm in ("rerank", "budget_rerank"):
            row = await run_arm(Search(), {"id": "q", "query": "question", "qrels": {"1": 1}},
                                arm, settings, template=template)
            assert row["error"] is None and row["reranked"]
            assert row["model_calls"][0]["status"] == "success"
            assert not client.is_closed()
            assert template._tools == {} and template._cache == {}
        assert len(requests) == 2
    assert client.is_closed()


@pytest.mark.asyncio
async def test_transport_diagnostics_record_failure_phase_without_exception_contents():
    with collect_performance():
        await trace_model_transport("proxy.start_tls.failed", {"exception": RuntimeError("sk-sensitive")})
        await trace_model_transport("connection.start_tls.complete", {})
        diagnostic = performance_snapshot()
    assert diagnostic["events"] == {"model_proxy_tls_failed": 1, "model_direct_tls_connected": 1}
    assert "sk-sensitive" not in json.dumps(diagnostic)
