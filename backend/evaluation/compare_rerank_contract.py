"""Compare only the ranking prompt on identical public, frozen five-passage pools."""
import argparse
import asyncio
import json
import logging
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import chromadb
import httpx
from anthropic import AsyncAnthropic
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

from core.llm_utils import extract_text_content, model_scope
from core.performance import collect_performance, performance_snapshot
from evaluation.compare_rag import attach_transport_trace, metrics, model_settings
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256, value_digest
from tooling import tool_manager
from tooling.knowledge_base import KnowledgeBase


def legacy_prompt(query, items):
    """Exact pre-fix prompt from cc107f2; candidate presentation stays identical."""
    items_text = "\n".join(f"{i}. {json.dumps(item, ensure_ascii=False)[:200]}"
                           for i, item in enumerate(items))
    return f"""根据用户查询，对以下检索结果按相关性打分（0-10），返回 JSON 数组。
用户查询: "{query}"
检索结果:
{items_text}

返回格式（按相关性降序排列的索引列表）: [最相关的索引, ..., 最不相关的索引]
只返回 JSON 数组，不要其他文字。"""


async def run_case(manager, query, items, arm):
    response_info = {}
    original = tool_manager.create_message

    async def capture(*args, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        response_info["prompt_sha256"] = sha256(prompt.encode())
        response = await original(*args, **kwargs)
        # This runner accepts only public reviewed passages; never used on live business text.
        response_info["response_text"] = extract_text_content(response.content, separator="")
        response_info["stop_reason"] = response.stop_reason
        return response

    prompt_fn = legacy_prompt if arm == "legacy" else manager._rerank_prompt
    with (
        patch.object(manager, "_rerank_prompt", prompt_fn),
        patch.object(tool_manager, "create_message", capture),
        collect_performance(), model_scope("evaluation", max_calls=1),
    ):
        ranked, accepted = await manager._rerank_with_status(query["query"], items, 3)
        perf = performance_snapshot()
    ids = [item["document_id"] for item in ranked]
    return {
        "query_id": query["id"], "arm": arm, "reranked": accepted,
        "candidate_ids": [item["document_id"] for item in items],
        "candidate_sha256": value_digest(items), "ranked_ids": ids,
        "metrics": metrics(ids, query["qrels"], 3),
        "retrieval_metrics": metrics([item["document_id"] for item in items], query["qrels"], 3),
        **response_info, "performance": perf,
    }


def summarize(rows):
    result = {}
    for arm in ("legacy", "current"):
        group = [row for row in rows if row["arm"] == arm]
        calls = [call for row in group for call in row["performance"]["llm_calls"]]
        events = Counter()
        for row in group:
            events.update(row["performance"]["events"])
        result[arm] = {
            "cases": len(group), "model_calls": len(calls),
            "model_errors": sum(call["status"] != "success" for call in calls),
            "accepted": sum(row["reranked"] for row in group),
            "complete_without_repair": sum(row["reranked"] and not row["performance"]["events"].get("rag_rerank_repaired") for row in group),
            "events": dict(events),
            "mean_recall_at_3": sum(row["metrics"]["recall"] for row in group) / len(group),
            "mean_ndcg_at_3": sum(row["metrics"]["ndcg"] for row in group) / len(group),
        }
    return result


async def compare(args):
    manifest, corpus, queries = validate(args.dataset)
    if any(q["review_status"] != "upstream_human_judgments" for q in queries):
        raise ValueError("Only upstream reviewed public queries are allowed")
    if args.limit > len(queries):
        raise ValueError("--limit exceeds frozen query count")
    queries = queries[:args.limit]
    settings, source = model_settings(args.env_file)
    embedding = ONNXMiniLM_L6_V2()
    db = chromadb.EphemeralClient(settings=chromadb.Settings(anonymized_telemetry=False))
    collection = db.create_collection("rerank-contract-" + uuid.uuid4().hex, embedding_function=embedding)
    try:
        for offset in range(0, len(corpus), 32):
            batch = corpus[offset:offset + 32]
            await asyncio.to_thread(collection.add,
                ids=[doc["id"] for doc in batch], documents=[doc["text"] for doc in batch],
                metadatas=[{"document_id": doc["id"], "title": doc["title"],
                            "version": doc["content_sha256"], "chunk_index": 0} for doc in batch])
        kb = KnowledgeBase.__new__(KnowledgeBase)
        kb._collection = collection
        pools = {query["id"]: await kb.search_async(query["query"], 5) for query in queries}
        result = {
            "schema": 1, "scope": "public development subset, paired ranking-output contract experiment",
            "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
            "dataset_manifest": manifest,
            "dataset_manifest_sha256": sha256((args.dataset / "manifest.json").read_bytes()),
            "model": settings["model"], "model_endpoint_sha256": value_digest(settings["base_url"]),
            "configuration_source": source, "rounds": args.rounds,
            "selected_query_ids": [q["id"] for q in queries],
            "embedding": {"name": "Chroma default all-MiniLM-L6-v2",
                          "model_files": {p.name: sha256(p.read_bytes()) for p in Path(embedding.DOWNLOAD_PATH).rglob("*") if p.is_file()}},
            "model_transport": {"network": "direct", "shared_client": True, "sdk_retries": 0},
            "controls": {"candidate_pool": 5, "top_k": 3, "temperature": 0, "max_tokens": 2048,
                         "prompt_only_change": True, "parser_shared": True, "alternating_order": True,
                         "rewrite": False, "per_arm_model_budget": 1},
            "complete": False, "rows": [],
        }
        async with AsyncAnthropic(api_key=settings["api_key"], base_url=settings["base_url"], max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=5),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=30),
                event_hooks={"request": [attach_transport_trace]})) as client:
            manager = tool_manager.MCPToolManager(**settings, client=client)
            for round_index in range(args.rounds):
                for query_index, query in enumerate(queries):
                    arms = ("legacy", "current") if (round_index + query_index) % 2 == 0 else ("current", "legacy")
                    for arm in arms:
                        row = await run_case(manager, query, pools[query["id"]], arm)
                        result["rows"].append({"round": round_index + 1, **row})
                        write_json(args.output, result)
                    print(f"round {round_index + 1}, query {query_index + 1}/{len(queries)}", flush=True)
        result["summary"] = summarize(result["rows"])
        result["complete"] = True
        write_json(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    finally:
        db.delete_collection(collection.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/rag-a3/miracl")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if min(args.rounds, args.limit) < 1:
        parser.error("--rounds and --limit must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
