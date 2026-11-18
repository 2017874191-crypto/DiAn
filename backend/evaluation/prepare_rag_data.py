"""Freeze a MIRACL Chinese judged-pool subset and DiAn evidence review drafts."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx

from evaluation.provenance import sha256
from tooling.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[1]
REVISION = "ad2acf33f265b8fba92e5096c9d2cf569a82b067"
SOURCE_SHA256 = "56798d5cbc601a6f1fa8df72f37ef37c4f26911e0cbc42d5b26d04cc43731f1e"
SOURCE_URL = f"https://huggingface.co/datasets/miracl/miracl/resolve/{REVISION}/zh/train/0000.parquet"
SEED = "dian-a3-v1"


def write_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def freeze(folder, corpus, queries, source):
    folder.mkdir(parents=True, exist_ok=True)
    payloads = {"corpus.json": corpus, "queries.json": queries}
    for name, data in payloads.items():
        path = folder / name
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != data:
            raise FileExistsError(f"Snapshot differs at {path}; use a new output directory")
    for name, data in payloads.items():
        write_json(folder / name, data)
    manifest = {
        "schema": 1, "source": source, "documents": len(corpus), "queries": len(queries),
        "files": {name: sha256((folder / name).read_bytes()) for name in payloads},
    }
    write_json(folder / "manifest.json", manifest)
    validate(folder)
    return manifest


def validate(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for name in ("corpus.json", "queries.json"):
        if sha256((folder / name).read_bytes()) != manifest["files"][name]:
            raise ValueError(f"Snapshot hash mismatch: {name}")
    corpus = json.loads((folder / "corpus.json").read_text(encoding="utf-8"))
    queries = json.loads((folder / "queries.json").read_text(encoding="utf-8"))
    docs = {doc["id"]: doc for doc in corpus}
    if len(docs) != len(corpus) or len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Duplicate document or query identity")
    if len(corpus) != manifest["documents"] or len(queries) != manifest["queries"]:
        raise ValueError("Snapshot count mismatch")
    for doc in corpus:
        if sha256(doc["text"].encode()) != doc["content_sha256"]:
            raise ValueError("Document content hash mismatch")
    for query in queries:
        if not query["query"].strip():
            raise ValueError("Empty query")
        for doc_id, grade in query["qrels"].items():
            if doc_id not in docs or type(grade) is not int or grade not in (0, 1, 2):
                raise ValueError("Invalid relevance judgment")
        if query["answerable"] != any(grade > 0 for grade in query["qrels"].values()):
            raise ValueError("Answerability differs from judgments")
        for evidence in query.get("evidence", []):
            if (query["qrels"].get(evidence["doc_id"], 0) <= 0
                    or not evidence["quote"] or evidence["quote"] not in docs[evidence["doc_id"]]["text"]):
                raise ValueError("Evidence does not match its source")
    return manifest, corpus, queries


def public_subset(rows, count):
    selected = sorted(rows, key=lambda row: sha256((SEED + row["query_id"]).encode()))[:count]
    if len(selected) != count:
        raise ValueError("Insufficient source queries")
    docs, queries = {}, []
    for row in selected:
        qrels = {}
        for key, grade in (("negative_passages", 0), ("positive_passages", 1)):
            for passage in row[key]:
                identity = passage["docid"]
                doc = {"id": identity, "title": passage["title"], "text": passage["text"],
                       "content_sha256": sha256(passage["text"].encode())}
                if identity in docs and docs[identity] != doc:
                    raise ValueError("Conflicting source passage")
                if identity in qrels and qrels[identity] != grade:
                    raise ValueError("Conflicting source relevance")
                docs[identity] = doc
                qrels[identity] = grade
        queries.append({"id": row["query_id"], "query": row["query"], "qrels": qrels,
                        "answerable": True, "review_status": "upstream_human_judgments",
                        "split": "development", "family_id": row["query_id"]})
    return sorted(docs.values(), key=lambda doc: doc["id"]), queries


def business_snapshot():
    documents = []
    class Collector:
        def add_documents(self, rows):
            documents.extend(rows)
    KnowledgeBase._load_default_docs(Collector())
    documents.extend(json.loads((ROOT / "config/logistics/knowledge.json").read_text(encoding="utf-8")))
    corpus, by_title = [], {}
    for doc in documents:
        identity = ["explicit", doc["document_id"]] if doc.get("document_id") else ["title", doc["title"]]
        document_id = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
        version = sha256(doc["content"].encode())
        chunks = KnowledgeBase._chunk_text(None, doc["content"], chunk_size=500)
        for index, text in enumerate(chunks):
            item = {"id": f"{document_id}:{index}", "title": doc["title"], "text": text,
                    "document_id": document_id, "version": version, "chunk": index,
                    "content_sha256": sha256(text.encode())}
            corpus.append(item)
            by_title.setdefault(doc["title"], []).append(item)
    specs = [
        ("退款政策", "购买几天内可以无理由退款？", "购买后 7 天内可以申请无理由退款"),
        ("退款政策", "退款申请要审核多久？", "1-3 个工作日内审核"),
        ("退款政策", "已经发货想退款，下一步是什么？", "需要先完成退货流程才能退款"),
        ("订单查询", "有哪些订单状态？", "待支付、已支付、已发货、运输中、已签收、已完成"),
        ("订单查询", "发货超过七天没收到，该找谁？", "可以联系客服申请查件"),
        ("订单查询", "刚发货，轨迹通常多久更新？", "发货后 24 小时内更新"),
        ("账户安全", "忘记密码怎么重置？", "绑定的手机号或邮箱重置"),
        ("账户安全", "客服让我把密码发过去，可以给吗？", "客服人员不会索要用户密码"),
        ("账户安全", "怎样开启额外的登录保护？", "安全设置中开启两步验证"),
        ("技术故障排查", "登录出现401，该检查什么？", "表示认证失败，请检查用户名密码"),
        ("技术故障排查", "应用一直崩溃，先怎么排查？", "清除缓存后重启应用"),
        ("技术故障排查", "遇到500错误该怎么办？", "这是服务端问题，请稍后重试"),
        ("会员与积分", "一百积分能抵多少钱？", "100 积分 = 1 元"),
        ("会员与积分", "金卡会员的消费门槛是多少？", "金卡会员（累计消费 5000 元）"),
        ("会员与积分", "积分多久会过期？", "积分有效期为 1 年"),
        ("配送说明", "加急配送需要多久、多少钱？", "加急配送：1-2 个工作日送达，运费 15 元"),
        ("配送说明", "标准配送满足什么条件免运费？", "免运费（订单满 99 元）"),
        ("配送说明", "订单已经发货，还能改地址吗？", "请在发货前联系客服"),
        ("疑似丢件核查演示规则", "轨迹停滞就能判定丢件吗？", "轨迹停滞仅作为疑似异常，不直接认定丢件"),
        ("疑似丢件核查演示规则", "疑似丢件还能做什么核查？", "可提交人工核查"),
        ("疑似丢件核查演示规则", "这份丢件规则是真实承运商政策吗？", "仅为演示政策，不代表真实承运商规则"),
        ("签收争议演示规则", "显示签收就能证明本人拿到了吗？", "签收记录不能证明实际收件"),
        ("签收争议演示规则", "没有收到但显示签收，可以申请什么核查？", "可提交签收争议核查"),
        ("签收争议演示规则", "签收争议规则属于什么版本？", "版本 demo-2026-11-v1"),
        ("破损错发举证演示规则", "破损错发需要提供哪些照片？", "外包装照片、面单照片、商品问题照片"),
        ("破损错发举证演示规则", "上传照片后会自动赔偿吗？", "不自动判责赔偿"),
        ("破损错发举证演示规则", "用户说已经提供材料，接下来能提交什么？", "可提交人工审核"),
        (None, "某承运商丢件按保价金额具体赔多少？", ""),
        (None, "订单DEMO1001目前的实时位置在哪里？", ""),
        (None, "我刚提交的退款申请现在审核通过了吗？", ""),
    ]
    queries = []
    for index, (title, query, quote) in enumerate(specs, 1):
        evidence = [{"doc_id": doc["id"], "quote": quote} for doc in by_title.get(title, []) if quote in doc["text"]]
        if title and not evidence:
            raise ValueError(f"Missing source evidence for draft {index}")
        queries.append({
            "id": f"DIAN-RAG-{index:03d}", "query": query, "family_id": title or f"unanswerable-{index}",
            "split": "development", "answerable": bool(title),
            "qrels": {item["doc_id"]: 2 for item in evidence}, "evidence": evidence,
            "review_status": "pending_human_review", "reviewer_1": None, "reviewer_2": None,
            "required_facts_draft": [quote] if quote else [],
        })
    return corpus, queries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-output", type=Path, default=ROOT / "data/rag-a3/miracl")
    parser.add_argument("--business-output", type=Path, default=ROOT / "evaluation/datasets/dian_rag_v1")
    parser.add_argument("--source", type=Path, default=ROOT / "data/rag-a3/miracl-zh-train.parquet")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--queries", type=int, default=30)
    args = parser.parse_args()
    if args.queries < 1:
        parser.error("--queries must be positive")
    if args.download:
        response = httpx.get(SOURCE_URL, follow_redirects=True, timeout=60)
        response.raise_for_status()
        if sha256(response.content) != SOURCE_SHA256:
            raise ValueError("Upstream parquet hash mismatch")
        args.source.parent.mkdir(parents=True, exist_ok=True)
        args.source.write_bytes(response.content)
    if sha256(args.source.read_bytes()) != SOURCE_SHA256:
        raise ValueError("Source parquet hash mismatch")
    import pyarrow.parquet as parquet
    corpus, queries = public_subset(parquet.read_table(args.source).to_pylist(), args.queries)
    public = freeze(args.public_output, corpus, queries, {
        "dataset": "miracl/miracl", "language": "zh", "upstream_split": "train",
        "revision": REVISION, "url": SOURCE_URL, "sha256": SOURCE_SHA256, "seed": SEED,
        "selection": "first N by sha256(seed + query_id); union of their positive and negative passages",
        "scope": "shared judged-pool subset; unjudged cross-query passages remain unjudged",
        "dataset_card_license": "Apache-2.0", "text_origin": "Chinese Wikipedia passages",
    })
    corpus, queries = business_snapshot()
    business = freeze(args.business_output, corpus, queries, {
        "dataset": "DiAn local demonstration knowledge", "review_status": "pending_human_review",
        "source_files": {
            name: sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n"))
            for name in ("tooling/knowledge_base.py", "config/logistics/knowledge.json")
        },
    })
    print(json.dumps({"public": public, "business": business}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
