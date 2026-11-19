"""Replay the existing GeneralAgent entry point with an evaluation-only BM25 tool adapter."""
import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from rank_bm25 import BM25Okapi

from agents.agent_orchestrator import GeneralAgent, Request
from agents.tools import build_shared_rag_tools
from core.llm_utils import model_scope
from core.performance import collect_performance, performance_snapshot
from evaluation.compare_rag import attach_transport_trace, model_settings
from evaluation.compare_rag_candidates import character_bigrams
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256, value_digest
from tooling.tool_manager import ToolResult


class EvaluationBM25:
    def __init__(self, corpus):
        self.corpus = corpus
        self.index = BM25Okapi([character_bigrams(doc["text"]) for doc in corpus],
                              k1=1.5, b=0.75, epsilon=0.25)
        self.calls = []

    async def search_with_rewrite(self, tool_name, query, top_k=5):
        scores = self.index.get_scores(character_bigrams(query))
        order = sorted(range(len(self.corpus)), key=lambda i: (-scores[i], self.corpus[i]["id"]))[:top_k]
        results = [{"title": self.corpus[i]["title"], "content": self.corpus[i]["text"],
                    "document_id": self.corpus[i]["id"], "score": float(scores[i]),
                    "version": self.corpus[i]["version"], "chunk": self.corpus[i]["chunk"]} for i in order]
        self.calls.append({"query": query, "requested_top_k": top_k, "results": results})
        return ToolResult(success=True, data=results, tool_name=tool_name)


async def replay(args):
    manifest, corpus, queries = validate(args.dataset)
    settings, source = model_settings()
    search = EvaluationBM25(corpus)
    result = {
        "schema": 1, "scope": "Existing GeneralAgent.handle and shared RAG tool bridge, evaluation BM25 adapter",
        "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
        "dataset_manifest": manifest,
        "dataset_manifest_sha256": sha256((args.dataset / "manifest.json").read_bytes()),
        "review_file_sha256": sha256((ROOT / "evaluation/reviews/dian_rag_v1/codex-initial-review.csv").read_bytes()),
        "model": settings["model"], "model_endpoint_sha256": value_digest(settings["base_url"]),
        "configuration_source": source,
        "controls": {"agent": "GeneralAgent", "agent_profile": "Production default",
                     "tool_choice": "Model autonomous", "retrieval": "Evaluation-only BM25 direct, respects requested top_k",
                     "rewrite": False, "rerank": False, "sdk_retries": 0, "model_call_budget": 3,
                     "user_context": "Fresh request with no supplied history or background"},
        "complete": False, "rows": [],
    }
    async with AsyncAnthropic(
        api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(30, connect=5),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
            event_hooks={"request": [attach_transport_trace]}),
    ) as client:
        for index, query in enumerate(queries):
            agent = GeneralAgent(client, settings["model"])
            agent.set_shared_tools(build_shared_rag_tools(search))
            request = Request(message=query["query"], user_id="rag-evaluation",
                              conv_id=query["id"], request_id=query["id"])
            start = len(search.calls)
            started = time.perf_counter()
            with collect_performance(), model_scope("evaluation", max_calls=3):
                response = await agent.handle(request)
                perf = performance_snapshot()
            result["rows"].append({
                "query_id": query["id"], "query": query["query"],
                "response": response.content, "success": response.success, "status": response.status,
                "error_code": response.error_code, "tools_used": response.tools_used,
                "tool_traces": response.tool_traces, "retrievals": search.calls[start:],
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "performance": perf,
            })
            write_json(args.output, result)
            print(f"query {index + 1}/{len(queries)}", flush=True)
    result["complete"] = True
    write_json(args.output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/dian_rag_v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(replay(args))


if __name__ == "__main__":
    main()
