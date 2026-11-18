"""Replay retrieval ablations on a frozen shared corpus without exposing qrels to models."""
import argparse
import asyncio
import json
import logging
import math
import os
import time
from pathlib import Path
from statistics import mean, median
from unittest.mock import patch

import chromadb
import httpx
from anthropic import AsyncAnthropic
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
from dotenv import dotenv_values, load_dotenv

from core.llm_utils import model_scope
from core.performance import collect_performance, performance_snapshot, record_event
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256, value_digest
from tooling.knowledge_base import KnowledgeBase
from tooling.tool_manager import MCPToolManager, Tool

ARMS = ("single", "rewrite", "rerank", "full", "budget_single", "budget_rerank")


def model_settings(env_file=None):
    path = env_file or ROOT / ".env"
    if env_file is not None and not path.is_file():
        raise FileNotFoundError(path)
    values = dotenv_values(path) if path.is_file() else os.environ
    if not values.get("ANTHROPIC_API_KEY"):
        raise ValueError("Selected model configuration requires ANTHROPIC_API_KEY")
    settings = {
        "api_key": values["ANTHROPIC_API_KEY"],
        "base_url": values.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        "model": values.get("ANTHROPIC_MODEL") or "claude-3-5-sonnet-20241022",
    }
    source = {
        "kind": "explicit_env_file" if env_file else "project_dotenv" if path.is_file() else "environment",
        "shadowed_environment_fields": [
            field for field in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
            if path.is_file() and os.getenv(field) and os.getenv(field) != values.get(field)
        ],
    }
    return settings, source


async def trace_model_transport(event, _info):
    names = {
        "connection.connect_tcp.complete": "model_tcp_connected",
        "connection.connect_tcp.failed": "model_tcp_failed",
        "connection.start_tls.complete": "model_direct_tls_connected",
        "connection.start_tls.failed": "model_direct_tls_failed",
        "proxy.start_tls.complete": "model_proxy_tls_connected",
        "proxy.start_tls.failed": "model_proxy_tls_failed",
    }
    if event in names:
        record_event(names[event])


async def attach_transport_trace(request):
    request.extensions["trace"] = trace_model_transport


def metrics(ranked, qrels, k):
    ids = list(dict.fromkeys(ranked))[:k]
    positive = {identity for identity, relevance in qrels.items() if relevance > 0}
    if not positive:
        return {"recall": None, "ndcg": None, "unjudged_fraction": None}
    gains = [(2 ** qrels.get(identity, 0) - 1) for identity in ids]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sorted((2 ** relevance - 1 for relevance in qrels.values()), reverse=True)[:k]
    idcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(ideal))
    return {
        "recall": len(set(ids) & positive) / len(positive),
        "ndcg": dcg / idcg,
        "unjudged_fraction": sum(identity not in qrels for identity in ids) / len(ids) if ids else 0,
    }


async def run_arm(kb, query, arm, settings, top_k=3, *, template=None):
    manager = template.fork() if template is not None else MCPToolManager(**settings)
    searches = []

    async def search(params, _context):
        items = await kb.search_async(params["query"], params["top_k"])
        searches.append({"query_sha256": sha256(params["query"].encode()), "top_k": params["top_k"],
                         "returned_ids": [item["document_id"] for item in items]})
        return items

    async def original_only(text, n=3):
        return [text]

    async def without_rerank(_query, items, count):
        return items[:count], False

    manager.register(Tool("knowledge_search", "Frozen evaluation corpus", search, {
        "type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query", "top_k"],
    }, timeout_s=30))
    error, data, reranked = None, [], False
    started = time.monotonic()
    try:
        with collect_performance(), model_scope("evaluation", max_calls=2):
            try:
                if arm in ("single", "budget_single", "budget_rerank"):
                    recall_k = 20 if arm.startswith("budget") else top_k
                    result = await manager.call("knowledge_search", {"query": query["query"], "top_k": recall_k},
                                                use_cache=False)
                    data = result.data or []
                    if not result.success:
                        error = "retrieval_failed"
                    if arm == "budget_rerank" and result.success:
                        data, reranked = await manager._rerank_with_status(query["query"], data, top_k)
                    data = data[:top_k]
                else:
                    # Evaluation-only removal of one mechanism, using the production pipeline.
                    with (
                        patch.object(manager, "rewrite_query", original_only if arm == "rerank" else manager.rewrite_query),
                        patch.object(manager, "_rerank_with_status",
                                     without_rerank if arm == "rewrite" else manager._rerank_with_status),
                    ):
                        result = await manager.search_with_rewrite("knowledge_search", query["query"], top_k=top_k)
                        data, reranked = result.data or [], result.reranked
                        if not result.success:
                            error = "retrieval_failed"
            except Exception as exc:
                error = type(exc).__name__
            perf = performance_snapshot()
    finally:
        if template is None:
            await manager._client.close()
    ids = [item["document_id"] for item in data]
    return {
        "query_id": query["id"], "arm": arm, "error": error,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "ranked_ids": ids, "reranked": reranked, "searches": searches,
        "metrics": {str(k): metrics(ids, query["qrels"], k) for k in (1, 3)},
        "model_calls": perf.get("llm_calls", []),
        "transport_events": perf.get("events", {}),
    }


def summarize(rows):
    summary = {}
    for arm in ARMS:
        group = [row for row in rows if row["arm"] == arm]
        times = sorted(row["latency_ms"] for row in group)
        calls = [call for row in group for call in row["model_calls"]]
        summary[arm] = {
            "queries": len(group), "errors": sum(row["error"] is not None for row in group),
            "model_errors": sum(call["status"] != "success" for call in calls),
            "reranked_queries": sum(row["reranked"] for row in group),
            "search_calls": sum(len(row["searches"]) for row in group), "model_calls": len(calls),
            "latency_p50_ms": median(times),
            "latency_p95_ms": times[math.ceil(.95 * len(times)) - 1],
            "metrics": {
                k: {name: mean(row["metrics"][k][name] for row in group)
                    for name in ("recall", "ndcg", "unjudged_fraction")} for k in ("1", "3")
            },
        }
    return summary


async def compare(args):
    manifest, corpus, queries = validate(args.dataset)
    if any(query["review_status"] != "upstream_human_judgments" for query in queries):
        raise ValueError("This runner requires upstream reviewed public queries")
    if args.limit > len(queries):
        raise ValueError("--limit exceeds frozen query count")
    queries = queries[:args.limit]
    settings, config_source = model_settings(args.env_file)
    load_dotenv(args.env_file or ROOT / ".env", override=args.env_file is not None)
    embedding = ONNXMiniLM_L6_V2()
    client = chromadb.EphemeralClient(settings=chromadb.Settings(anonymized_telemetry=False))
    collection = client.create_collection("rag-a3-" + os.urandom(8).hex(), embedding_function=embedding)
    try:
        for offset in range(0, len(corpus), 32):
            batch = corpus[offset:offset + 32]
            await asyncio.to_thread(
                collection.add, ids=[doc["id"] for doc in batch], documents=[doc["text"] for doc in batch],
                metadatas=[{"document_id": doc["id"], "title": doc["title"],
                            "version": doc["content_sha256"], "chunk_index": 0} for doc in batch],
            )
        kb = KnowledgeBase.__new__(KnowledgeBase)
        kb._collection = collection
        # Exclude first-use ONNX/collection initialization from per-arm latency.
        await kb.search_async(queries[0]["query"], 3)
        result = {
            "schema": 1, "scope": "MIRACL zh train, shared judged-pool development subset",
            "dataset_manifest": manifest, "dataset_manifest_sha256": sha256((args.dataset / "manifest.json").read_bytes()),
            "code_commit": git_commit(ROOT), "build": build_identity(ROOT), "rounds": args.rounds,
            "selected_query_ids": [query["id"] for query in queries],
            "model": settings["model"], "model_endpoint_sha256": value_digest(settings.get("base_url", "default")),
            "configuration_source": config_source,
            "model_transport": {"network": args.network, "shared_client": True, "sdk_retries": 0,
                                "connect_timeout_s": 5, "read_timeout_s": 30,
                                "keepalive_expiry_s": 30, "max_connections": 4},
            "embedding": {"name": "Chroma default all-MiniLM-L6-v2",
                          "model_files": {p.name: sha256(p.read_bytes()) for p in Path(embedding.DOWNLOAD_PATH).rglob("*") if p.is_file()}},
            "index_distance": "Chroma default l2",
            "retrieval_unit": "original MIRACL passage, no rechunking", "final_top_k": 3,
            "cold_cache": True, "unjudged_policy": "score zero for metrics; report fraction separately",
            "complete": False, "rows": [],
        }
        async with AsyncAnthropic(
            api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
            http_client=httpx.AsyncClient(
                trust_env=args.network == "system", timeout=httpx.Timeout(30, connect=5),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
                event_hooks={"request": [attach_transport_trace]},
            ),
        ) as model_client:
            template = MCPToolManager(**settings, client=model_client)
            for round_index in range(args.rounds):
                for query_index, query in enumerate(queries):
                    shift = (round_index + query_index) % len(ARMS)
                    for arm in ARMS[shift:] + ARMS[:shift]:
                        row = await run_arm(kb, query, arm, settings, template=template)
                        result["rows"].append({"round": round_index + 1, **row})
                        write_json(args.output, result)
                    print(f"round {round_index + 1}, query {query_index + 1}/{len(queries)}", flush=True)
        result["summary"] = summarize(result["rows"])
        result["complete"] = True
        write_json(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    finally:
        client.delete_collection(collection.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/rag-a3/miracl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="Explicit evaluation-only settings, overriding inherited variables")
    parser.add_argument("--network", choices=("direct", "system"), default="direct",
                        help="Evaluation model transport, direct by default; system uses environment and OS proxies")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    if args.rounds < 1 or args.limit < 1:
        parser.error("--rounds and --limit must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
