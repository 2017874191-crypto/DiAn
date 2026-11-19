"""Measure the marginal value of production reranking on frozen BM25 candidates."""
import argparse
import asyncio
import json
import logging
import math
import time
from pathlib import Path
from statistics import mean

import httpx
from anthropic import AsyncAnthropic

from evaluation.compare_rag import attach_transport_trace, metrics, model_settings
from evaluation.compare_rerank_contract import run_case
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256, value_digest
from tooling.tool_manager import MCPToolManager


def summarize(rows, query_ids):
    selected = [row for row in rows if row["query_id"] in query_ids]
    result = {}
    for arm in ("bm25_direct", "bm25_rerank"):
        group = [row for row in selected if row["arm"] == arm]
        calls = [call for row in group for call in row["performance"]["llm_calls"]]
        times = sorted(row["ranking_latency_ms"] for row in group)
        result[arm] = {
            "queries": len(group), "hit_queries": sum(row["hit"] for row in group),
            "recall_at_3": mean(row["metrics"]["recall"] for row in group),
            "ndcg_at_3": mean(row["metrics"]["ndcg"] for row in group),
            "ranking_p95_ms": times[math.ceil(.95 * len(times)) - 1],
            "model_calls": len(calls), "model_errors": sum(c["status"] != "success" for c in calls),
            "accepted": sum(row.get("reranked", False) for row in group),
            "repaired": sum(row["performance"]["events"].get("rag_rerank_repaired", 0) for row in group),
            "usage": {key: sum(c.get(key) or 0 for c in calls) for key in (
                "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
        }
    direct = {r["query_id"]: r for r in selected if r["arm"] == "bm25_direct"}
    rerank = {r["query_id"]: r for r in selected if r["arm"] == "bm25_rerank"}
    result["gained_hit_queries"] = [q for q in direct if rerank[q]["hit"] and not direct[q]["hit"]]
    result["lost_hit_queries"] = [q for q in direct if direct[q]["hit"] and not rerank[q]["hit"]]
    deltas = [rerank[q]["metrics"]["ndcg"] - direct[q]["metrics"]["ndcg"] for q in direct]
    result["ndcg_win_tie_loss"] = [sum(d > 0 for d in deltas), sum(d == 0 for d in deltas), sum(d < 0 for d in deltas)]
    return result


async def compare(args):
    manifest, corpus, queries = validate(args.dataset)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    digest = sha256((args.dataset / "manifest.json").read_bytes())
    if candidates["dataset_manifest_sha256"] != digest:
        raise ValueError("Candidate corpus must match the frozen snapshot")
    pools = {r["query_id"]: r for r in candidates["rows"]}
    if set(pools) != {q["id"] for q in queries}:
        raise ValueError("Candidate pools must cover the frozen questions")
    docs = {doc["id"]: doc for doc in corpus}
    settings, source = model_settings()
    result = {
        "schema": 1, "scope": "One paired development pass over frozen BM25 candidates",
        "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
        "dataset_manifest": manifest, "dataset_manifest_sha256": digest,
        "candidate_file_sha256": sha256(args.candidates.read_bytes()),
        "configuration_source": source, "model": settings["model"],
        "model_endpoint_sha256": value_digest(settings["base_url"]),
        "controls": {"candidate_budget": 20, "top_k": 3, "temperature": 0, "max_tokens": 2048,
                     "sdk_retries": 0, "shared_direct_client": True, "per_question_model_calls": 1,
                     "candidate_preview": "Production serialized item prefix, 200 characters",
                     "latency_scope": "Ranking only; frozen retrieval and materialization excluded"},
        "complete": False, "rows": [],
    }
    async with AsyncAnthropic(
        api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(30, connect=5),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
            event_hooks={"request": [attach_transport_trace]}),
    ) as client:
        manager = MCPToolManager(**settings, client=client)
        for index, query in enumerate(queries):
            pool = pools[query["id"]]
            items = [{"title": docs[i]["title"], "content": docs[i]["text"], "score": score,
                      "chunk": 0, "document_id": i, "version": docs[i]["content_sha256"]}
                     for i, score in zip(pool["bm25_ids"], pool["bm25_scores"])]
            arms = ("bm25_direct", "bm25_rerank") if index % 2 == 0 else ("bm25_rerank", "bm25_direct")
            for arm in arms:
                started = time.perf_counter()
                if arm == "bm25_rerank":
                    row = await run_case(manager, query, items, "current")
                    row["arm"] = arm
                else:
                    ids = [item["document_id"] for item in items[:3]]
                    row = {"query_id": query["id"], "arm": arm, "ranked_ids": ids,
                           "candidate_ids": pool["bm25_ids"], "candidate_sha256": value_digest(items),
                           "metrics": metrics(ids, query["qrels"], 3),
                           "performance": {"llm_calls": [], "events": {}}}
                row["ranking_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                row["hit"] = any(query["qrels"].get(i, 0) > 0 for i in row["ranked_ids"])
                result["rows"].append(row)
                write_json(args.output, result)
            print(f"query {index + 1}/{len(queries)}", flush=True)
    result["summary"] = {
        "all_30": summarize(result["rows"], pools.keys()),
        "previous_8": summarize(result["rows"], [q["id"] for q in queries[:8]]),
        "added_22": summarize(result["rows"], [q["id"] for q in queries[8:]]),
    }
    result["complete"] = True
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/rag-a3/miracl")
    parser.add_argument("--candidates", type=Path, default=ROOT / "evaluation/rag_candidate_bm25.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
