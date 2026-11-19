"""Compare frozen vector candidates with character-bigram BM25 on the same public pool."""
import argparse
import json
import re
import time
import unicodedata
from importlib.metadata import version
from pathlib import Path
from statistics import mean

from rank_bm25 import BM25Okapi

from evaluation.compare_rag import metrics
from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import build_identity, git_commit, sha256


def character_bigrams(text):
    runs = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).lower())
    return [run[i:i + 2] for run in runs for i in range(max(1, len(run) - 1))]


def compare(dataset, baseline_path):
    manifest, corpus, queries = validate(dataset)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    digest = sha256((dataset / "manifest.json").read_bytes())
    if not baseline["complete"] or baseline["dataset_manifest_sha256"] != digest:
        raise ValueError("Requires complete baseline on the identical frozen snapshot")
    if set(baseline["selected_query_ids"]) != {q["id"] for q in queries}:
        raise ValueError("Baseline must cover every frozen query")
    vectors = {}
    for row in baseline["rows"]:
        if row["arm"] == "budget_rerank":
            if row["query_id"] in vectors or row["error"] or len(row["searches"]) != 1:
                raise ValueError("Requires one successful original-query retrieval per question")
            search = row["searches"][0]
            if search["top_k"] != 20:
                raise ValueError("Baseline candidate budget must be twenty")
            vectors[row["query_id"]] = search
    if len(vectors) != len(queries):
        raise ValueError("Missing baseline candidate rows")
    started = time.perf_counter()
    index = BM25Okapi([character_bigrams(doc["text"]) for doc in corpus],
                     k1=1.5, b=0.75, epsilon=0.25)
    index_ms = (time.perf_counter() - started) * 1000
    result = {
        "schema": 1, "scope": "Frozen MIRACL zh development judged-pool candidate comparison",
        "code_commit": git_commit(ROOT), "build": build_identity(ROOT),
        "dataset_manifest": manifest, "dataset_manifest_sha256": digest,
        "baseline_file_sha256": sha256(baseline_path.read_bytes()),
        "vector_baseline_commit": baseline["code_commit"],
        "vector_embedding": baseline["embedding"],
        "vector_measurement": "Recorded twenty candidates from budget_rerank, not recomputed",
        "bm25": {"package": "rank-bm25", "version": version("rank-bm25"),
                 "k1": 1.5, "b": 0.75, "epsilon": 0.25,
                 "tokenization": "NFKC lowercase Unicode alphanumeric runs; overlapping bigrams, single-character run kept",
                 "input": "Passage text only, matching vector embedding input",
                 "ties": "Document ID ascending", "index_ms": round(index_ms, 3)},
        "candidate_budget": 20, "model_calls": 0, "rows": [],
    }
    doc_ids = {doc["id"] for doc in corpus}
    for query in queries:
        search = vectors[query["id"]]
        if search["query_sha256"] != sha256(query["query"].encode()):
            raise ValueError("Baseline is not the original query")
        vector = search["returned_ids"]
        started = time.perf_counter()
        scores = index.get_scores(character_bigrams(query["query"]))
        order = sorted(range(len(corpus)), key=lambda i: (-scores[i], corpus[i]["id"]))[:20]
        elapsed = (time.perf_counter() - started) * 1000
        bm25 = [corpus[i]["id"] for i in order]
        if len(set(vector)) != 20 or not set(vector) <= doc_ids:
            raise ValueError("Invalid baseline candidate identities")
        positive = {identity for identity, grade in query["qrels"].items() if grade > 0}
        vector_hits, bm25_hits = positive & set(vector), positive & set(bm25)
        result["rows"].append({
            "query_id": query["id"], "query": query["query"],
            "vector_ids": vector, "bm25_ids": bm25,
            "bm25_scores": [float(scores[i]) for i in order],
            "bm25_positive_score_candidate_count": int(sum(scores[i] > 0 for i in order)),
            "bm25_zero_score_positive_ids": [corpus[i]["id"] for i in order
                                             if scores[i] == 0 and corpus[i]["id"] in positive],
            "bm25_query_ms": round(elapsed, 3),
            "vector_metrics": metrics(vector, query["qrels"], 20),
            "bm25_metrics": metrics(bm25, query["qrels"], 20),
            "vector_metrics_at_3": metrics(vector, query["qrels"], 3),
            "bm25_metrics_at_3": metrics(bm25, query["qrels"], 3),
            "vector_positive_ids": sorted(vector_hits), "bm25_positive_ids": sorted(bm25_hits),
            "gained_positive_ids": sorted(bm25_hits - vector_hits),
            "lost_positive_ids": sorted(vector_hits - bm25_hits),
        })
    # The first eight were exposed in the preceding connection and contract experiments.
    groups = {"all_30": result["rows"], "previous_8": result["rows"][:8], "added_22": result["rows"][8:]}
    result["summary"] = {}
    for name, rows in groups.items():
        summary = {"queries": len(rows)}
        for method in ("vector", "bm25"):
            summary[method] = {
                "hit_queries": sum(bool(row[f"{method}_positive_ids"]) for row in rows),
                "recall_at_20": mean(row[f"{method}_metrics"]["recall"] for row in rows),
                "ndcg_at_20": mean(row[f"{method}_metrics"]["ndcg"] for row in rows),
                "unjudged_fraction": mean(row[f"{method}_metrics"]["unjudged_fraction"] for row in rows),
                "direct_hit_at_3": sum(bool(set(row[f"{method}_ids"][:3]) & set(row[f"{method}_positive_ids"])) for row in rows),
                "direct_recall_at_3": mean(row[f"{method}_metrics_at_3"]["recall"] for row in rows),
                "direct_ndcg_at_3": mean(row[f"{method}_metrics_at_3"]["ndcg"] for row in rows),
            }
        summary["newly_hit_queries"] = [r["query_id"] for r in rows if r["bm25_positive_ids"] and not r["vector_positive_ids"]]
        summary["lost_hit_queries"] = [r["query_id"] for r in rows if r["vector_positive_ids"] and not r["bm25_positive_ids"]]
        summary["both_miss_queries"] = [r["query_id"] for r in rows if not r["vector_positive_ids"] and not r["bm25_positive_ids"]]
        result["summary"][name] = summary
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data/rag-a3/miracl")
    parser.add_argument("--baseline", type=Path, default=ROOT / "evaluation/rag_30q_first.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.dataset, args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
