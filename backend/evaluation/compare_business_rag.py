"""Development retrieval replay using explicitly model-assisted business review labels."""
import argparse
import asyncio
import csv
import json
import logging
import math
import time
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from rank_bm25 import BM25Okapi

from evaluation.compare_bm25_rerank import summarize
from evaluation.compare_rag import attach_transport_trace, metrics, model_settings
from evaluation.compare_rag_candidates import character_bigrams
from evaluation.compare_rerank_contract import run_case
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256, value_digest
from tooling.tool_manager import MCPToolManager


async def compare(args):
    manifest, corpus, queries = validate(args.dataset)
    review_dir = args.review.parent
    source = json.loads((review_dir / "source.json").read_text(encoding="utf-8"))
    digest = sha256((args.dataset / "manifest.json").read_bytes())
    if digest != source["source_manifest_sha256"]:
        raise ValueError("Review and dataset must use the same source snapshot")
    if sha256(args.review.read_bytes()) != source["files"][args.review.name]:
        raise ValueError("Review file differs from its recorded identity")
    with args.review.open(encoding="utf-8-sig", newline="") as handle:
        reviews = {row["样本ID"]: row for row in csv.DictReader(handle)}
    if set(reviews) != {q["id"] for q in queries}:
        raise ValueError("Review must cover every business question")
    aliases = source["document_aliases"]
    for query in queries:
        review = reviews[query["id"]]
        if query["query"] != review["问题"]:
            raise ValueError("Reviewed question text differs")
        query["qrels"] = {doc["id"]: 0 for doc in corpus}
        for column, grade in (("部分证据", 1), ("充分证据", 2)):
            for alias in filter(None, review[column].split(",")):
                query["qrels"][aliases[alias]] = grade
    index = BM25Okapi([character_bigrams(doc["text"]) for doc in corpus],
                     k1=1.5, b=0.75, epsilon=0.25)
    settings, config_source = model_settings()
    result = {
        "schema": 1, "scope": "Business development replay with Codex-assisted labels",
        "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
        "dataset_manifest": manifest, "dataset_manifest_sha256": digest,
        "review_file_sha256": sha256(args.review.read_bytes()),
        "label_source": source["assisted_review"],
        "model": settings["model"], "model_endpoint_sha256": value_digest(settings["base_url"]),
        "configuration_source": config_source,
        "controls": {"candidate_budget": min(20, len(corpus)), "top_k": 3,
                     "bm25": {"k1": 1.5, "b": 0.75, "epsilon": 0.25},
                     "input": "Passage text only, same bigram tokenizer as public experiment",
                     "model_retries": 0, "per_query_model_calls": 1,
                     "preview": "Production 200-character serialized item prefix",
                     "quality_population": "Review decision 是 only",
                     "boundary_population": "Review decision 不确定 or 否, no quality aggregate",
                     "latency_scope": "Local retrieval and ranking only, excludes chat and answer generation"},
        "rows": [], "complete": False,
    }
    async with AsyncAnthropic(
        api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
        http_client=httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(30, connect=5),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
            event_hooks={"request": [attach_transport_trace]}),
    ) as client:
        manager = MCPToolManager(**settings, client=client)
        for offset, query in enumerate(queries):
            review = reviews[query["id"]]
            started = time.perf_counter()
            scores = index.get_scores(character_bigrams(query["query"]))
            order = sorted(range(len(corpus)), key=lambda i: (-scores[i], corpus[i]["id"]))[:20]
            items = [{"title": corpus[i]["title"], "content": corpus[i]["text"],
                      "score": float(scores[i]), "chunk": corpus[i]["chunk"],
                      "document_id": corpus[i]["id"], "version": corpus[i]["version"]} for i in order]
            retrieval_ms = (time.perf_counter() - started) * 1000
            arms = ("bm25_direct", "bm25_rerank") if offset % 2 == 0 else ("bm25_rerank", "bm25_direct")
            for arm in arms:
                started = time.perf_counter()
                if arm == "bm25_rerank":
                    row = await run_case(manager, query, items, "current")
                    row["arm"] = arm
                else:
                    ids = [item["document_id"] for item in items[:3]]
                    row = {"query_id": query["id"], "arm": arm, "ranked_ids": ids,
                           "candidate_ids": [i["document_id"] for i in items],
                           "candidate_sha256": value_digest(items),
                           "metrics": metrics(ids, query["qrels"], 3),
                           "performance": {"llm_calls": [], "events": {}}}
                row["ranking_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                row["retrieval_latency_ms"] = round(retrieval_ms, 3)
                row["review_decision"] = review["可回答"]
                row["hit"] = any(query["qrels"].get(i, 0) > 0 for i in row["ranked_ids"])
                row["sufficient_hit"] = any(query["qrels"].get(i, 0) == 2 for i in row["ranked_ids"])
                if review["可回答"] != "是":
                    row["metrics"] = None
                    row.pop("retrieval_metrics", None)
                result["rows"].append(row)
                write_json(args.output, result)
            print(f"query {offset + 1}/{len(queries)}", flush=True)
    answerable = {q for q, review in reviews.items() if review["可回答"] == "是"}
    summary = summarize(result["rows"], answerable)
    for arm in ("bm25_direct", "bm25_rerank"):
        group = [r for r in result["rows"] if r["query_id"] in answerable and r["arm"] == arm]
        total_times = sorted(r["retrieval_latency_ms"] + r["ranking_latency_ms"] for r in group)
        summary[arm]["sufficient_hit_queries"] = sum(r["sufficient_hit"] for r in group)
        summary[arm]["retrieval_plus_ranking_p95_ms"] = total_times[math.ceil(.95 * len(group)) - 1]
    calls = [c for r in result["rows"] for c in r["performance"]["llm_calls"]]
    result["summary"] = {
        "answerable": summary,
        "boundary_query_ids": [q for q in reviews if q not in answerable],
        "total_model_calls": len(calls), "total_model_errors": sum(c["status"] != "success" for c in calls),
        "total_usage": {k: sum(c.get(k) or 0 for c in calls) for k in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
    }
    result["complete"] = True
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/dian_rag_v1")
    parser.add_argument("--review", type=Path, default=ROOT / "evaluation/reviews/dian_rag_v1/codex-initial-review.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
