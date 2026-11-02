"""结构、分组、来源和固定工具世界验证。不会请求模型或运行中的服务。"""
import argparse, collections, csv, gzip, hashlib, json
from pathlib import Path

def digest(b): return hashlib.sha256(b).hexdigest()
def load_lines(p): return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
def validate(root, cache=None):
    cases=load_lines(root/"all.jsonl")
    sources=json.loads((root/"sources.json").read_text(encoding="utf-8"))
    assert len(cases)==50
    assert len({c["case_id"] for c in cases})==50
    groups={}; source_groups={}; fingerprints=set()
    valid_states={None,"awaiting_info","awaiting_evidence","awaiting_confirmation","manual_review","ticket_created"}
    for c in cases:
        assert c["split"] in {"dev","test"}
        assert groups.setdefault(c["family_id"],c["split"])==c["split"], "family leaked"
        key=digest(json.dumps([c["setup"],c["turns"]],ensure_ascii=False,sort_keys=True).encode())
        assert key not in fingerprints, "duplicate case"
        fingerprints.add(key)
        assert c["setup"]["fixtures_sha256"]==digest((root/"fixtures.json").read_bytes())
        assert c["setup"]["policy_sha256"]==digest((root/"policies.json").read_bytes())
        ref=c["provenance"]
        assert (ref is not None)==(c["origin_kind"]=="open_source_adapted")
        if ref:
            assert ref["file_sha256"]==sources[ref["dataset"]]["sha256"]
            k=(ref["dataset"],ref["record_id"])
            assert source_groups.setdefault(k,c["split"])==c["split"], "source record leaked"
        for i,t in enumerate(c["turns"]):
            e=t["expected"]
            assert e["case_state"] in valid_states
            assert bool(e["case_issue"])==(e["case_state"] is not None)
            assert isinstance(e["ticket_count"],int) and e["ticket_count"]>=0
            assert not set(e["required_tools"]) & set(e["forbidden_tools"])
            if "replay_of_turn" in t:
                j=t["replay_of_turn"]
                assert 0<=j<i and t["message"]==c["turns"][j]["message"]
            if t.get("before_event")=="restart_services":assert "controlled_service_restart" in c["requirements"]
    for split in ["dev","test"]:
        assert load_lines(root/(split+".jsonl"))==[c for c in cases if c["split"]==split]
    assert len(load_lines(root/"source_records.jsonl"))==40
    if cache:
        raw={}
        for name,src in sources.items():
            b=(cache/src["local_cache_name"]).read_bytes()
            assert digest(b)==src["sha256"],name+" content changed"
            raw[name]=json.loads(gzip.decompress(b)) if name=="abcd" else list(csv.DictReader(b.decode("utf-8-sig").splitlines()))
        for c in cases:
            r=c["provenance"]
            if not r:continue
            if r["dataset"]=="abcd":
                record=next(x for x in raw["abcd"][r["source_split"]] if f"convo:{x['convo_id']}"==r["record_id"])
                assert record["scenario"]["flow"]+"/"+record["scenario"]["subflow"]==r["source_intent"]
                text=json.dumps([t for t in record["original"] if t[0]=="customer"],ensure_ascii=False,separators=(",",":"))
                assert digest(text.encode())==r["customer_turns_sha256"]
            else:
                record=raw[r["dataset"]][r["row_index_zero_based"]]
                assert record["intent"]==r["source_intent"]
                assert digest(record["instruction"].encode())==r["instruction_sha256"]
    print(json.dumps({"valid":True,"cases":len(cases),"turns":sum(len(c["turns"]) for c in cases),"splits":dict(collections.Counter(c["split"] for c in cases)),"families":len(groups),"source_record_check":bool(cache)},ensure_ascii=False))

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--source-cache",type=Path);args=parser.parse_args()
    validate(Path(__file__).resolve().parent,args.source_cache)
