"""Export two blank review sheets and complete source passages for business RAG drafts."""
import argparse
import csv
from pathlib import Path

from evaluation.prepare_rag_data import ROOT, validate, write_json
from evaluation.provenance import sha256


FIELDS = [
    "样本ID", "问题", "可回答", "充分证据", "部分证据", "证据原文",
    "必要事实", "已核对全部语料", "复核者", "备注",
]


def export(dataset, output):
    manifest, corpus, queries = validate(dataset)
    if output.exists():
        raise FileExistsError("Choose a new output directory to preserve existing review decisions")
    output.mkdir(parents=True)
    aliases = {f"D{i:02}": doc["id"] for i, doc in enumerate(corpus, 1)}
    for slot in (1, 2):
        with (output / f"reviewer-{slot}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({"样本ID": query["id"], "问题": query["query"]} for query in queries)
    text = [
        "# DiAn 业务检索人工复核材料", "",
        f"本材料含 {len(queries)} 个开发问题与 {len(corpus)} 个完整证据片段。两份复核表的判定栏均为空。资料来自项目演示知识，复核结论适用于这份固定语料。", "",
        "## 填写方法", "",
        "每位复核者使用自己的 CSV。Excel 可直接打开 UTF-8 BOM 编码文件。先阅读全部语料，再逐题独立填写。", "",
        "- 可回答填写“是”“否”或“不确定”，以这份语料能否支持回答为依据。",
        "- 充分证据填写能直接支持回答的片段别名，例如 D01。多条用英文逗号隔开。",
        "- 部分证据填写有关联但条件不足的别名。与充分证据分别判断，其余片段在确认核对完整语料后记为不相关。",
        "- 证据原文复制关键句，并注明片段别名。必要事实填写回答必须保留的条件、时间范围或操作边界。",
        "- 已核对全部语料填写“是”后，空白的两类证据才表示确实没有找到证据。填写“不确定”时在备注写明疑点。",
        "- 复核者填写可追溯姓名或代号。两人独立填写后，再比较分歧并保留裁决依据。", "",
        "充分证据对应相关性等级 2，部分证据对应等级 1，核对后其余片段对应等级 0。把等级与可回答性一起判断，需要组合多个片段才能回答时，在必要事实和备注中写明。", "",
        "这轮先核对问题和证据。实际回答产生后还需核对引用、事实和越权承诺。现有业务草稿的候选标签保留在原数据集中，提交各自判定后可对照检查。", "",
        "## 完整语料", "",
    ]
    for alias, doc in zip(aliases, corpus):
        text.extend([f"### {alias} {doc['title']}", "", f"片段 ID `{doc['id']}`", "",
                     f"内容 SHA-256 `{doc['content_sha256']}`", "", doc["text"], ""])
    text.extend(["## 待复核问题", "", "| 样本 ID | 问题 |", "|---|---|"])
    for query in queries:
        text.append(f"| {query['id']} | {query['query'].replace('|', '／')} |")
    text.extend(["", "## 当前状态", "",
                 "全部判定待人工填写。两份表中的意见收集后，再整理相关性标签、可回答性和分歧。本目录保留原片段身份与源文件哈希，修订标签时另存新的版本。", ""])
    (output / "README.md").write_text("\n".join(text), encoding="utf-8")
    metadata = {
        "schema": 1, "status": "pending_human_review",
        "source_manifest_sha256": sha256((dataset / "manifest.json").read_bytes()),
        "source_files": manifest["files"], "queries": len(queries), "passages": len(corpus),
        "document_aliases": aliases,
        "files": {name: sha256((output / name).read_bytes())
                  for name in ("reviewer-1.csv", "reviewer-2.csv", "README.md")},
    }
    write_json(output / "source.json", metadata)
    print(f"Prepared {len(queries)} questions and {len(corpus)} passages in {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/dian_rag_v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.dataset, args.output)


if __name__ == "__main__":
    main()
