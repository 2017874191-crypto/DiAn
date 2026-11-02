"""物流案件领域服务。SQLite 原子事务保存案件和模拟工单；不执行真实赔付。"""
import copy
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

ISSUES = {"logistics_lost", "logistics_dispute", "logistics_damaged"}
RULE_VERSION = "demo-2026-11-v1"
STATES = {"awaiting_info", "verifying", "awaiting_evidence", "awaiting_confirmation", "manual_review", "ticket_created"}

def detect_issue(message):
    if re.search(r"签收.{0,10}(没收到|未收到|没拿到)|没收到.{0,10}签收|被代签", message):
        return "logistics_dispute"
    if any(x in message for x in ("破损", "错发", "发错货", "包裹损坏")):
        return "logistics_damaged"
    if any(x in message for x in ("丢件", "丢失", "物流一直不更新", "物流不更新", "快递不更新")):
        return "logistics_lost"
    return None

def is_confirmed(message):
    return bool(re.fullmatch(r"\s*确认(?:建单|创建工单|提交工单)[。！!]?\s*", message))

class LogisticsService:
    def __init__(self, db_path=None, fixtures=None):
        self.path = Path(db_path or os.getenv("DIAN_LOGISTICS_DB", "data/logistics/cases.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        source = Path(fixtures or os.getenv("DIAN_LOGISTICS_FIXTURES", str(Path(__file__).resolve().parents[1] / "config/logistics/fixtures.json")))
        self.orders = json.loads(source.read_text(encoding="utf-8"))["orders"]
        self.rules = json.loads((Path(__file__).resolve().parents[1] / "config/logistics/policies.json").read_text(encoding="utf-8"))
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conv_id TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS tickets (case_id TEXT PRIMARY KEY, ticket_id TEXT UNIQUE NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def query_order(self, user_id, order_id=None, tracking_no=None):
        matches = [o for o in self.orders if (o["order_id"] == order_id if order_id else o["tracking_no"] == tracking_no)]
        if not matches or matches[0]["user_id"] != user_id:
            return {"success": False, "error": "订单不存在或当前用户无权查询", "source": "mock"}
        order = matches[0]
        if tracking_no and order["tracking_no"] != tracking_no:
            return {"success": False, "error": "订单与运单不匹配，请重新核对", "source": "mock"}
        return {"success": True, "source": "mock", "order": {k:v for k,v in order.items() if k not in ("user_id", "fault")}}

    def query_logistics(self, user_id, order_id):
        found = self.query_order(user_id, order_id)
        if not found["success"]:
            return found
        order = next(o for o in self.orders if o["order_id"] == order_id)
        if order.get("fault") == "query_timeout":
            return {"success": False, "error": "模拟物流查询超时", "source": "mock", "retryable": True}
        return {"success": True, "source": "mock", "tracking": copy.deepcopy(found["order"])}

    def get_case(self, user_id, case_id, conv_id=None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=?" + (" AND conv_id=?" if conv_id is not None else ""),
                (case_id, user_id, conv_id) if conv_id is not None else (case_id, user_id)).fetchone()
        return json.loads(row[0]) if row else None

    def request_manual_review(self, req):
        case_id = (req.conversation_state or {}).get("logistics_case_id")
        if not case_id:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=? AND conv_id=?", (case_id, req.user_id, req.conv_id)).fetchone()
            if not row:
                return
            case = json.loads(row[0])
            if case["state"] == "ticket_created":
                return
            before = case["state"]
            case["state"] = "manual_review"
            case["updated_at"] = datetime.now(timezone.utc).isoformat()
            case["events"].append({"request_id": req.request_id, "from": before, "to": "manual_review", "at": case["updated_at"], "reason": "user_requested_handoff"})
            case["events"] = case["events"][-100:]
            db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case, ensure_ascii=False), case_id))
        req.conversation_state["logistics_case_state"] = "manual_review"

    def process(self, req):
        """协调层每轮执行一次，LLM 不能指定案件状态或伪造核验结果。"""
        state = req.conversation_state or {}
        old_id = state.get("logistics_case_id")
        old = self.get_case(req.user_id, old_id) if old_id else None
        issue = req.intent.value if req.intent and req.intent.value in ISSUES else (old or {}).get("issue")
        if issue not in ISSUES:
            raise ValueError("不支持的物流案件类型")
        entities = req.entities or {}
        order_id = next(iter(entities.get("order_id", [])), None)
        tracking_no = next(iter(entities.get("tracking_no", [])), None)
        new_task = bool(re.search(r"(?:换个|新的|另一个)(?:问题|订单)|另一笔订单", req.message))
        if old and (new_task or old["issue"] != issue or (order_id and old.get("order_id") and old["order_id"] != order_id) or (tracking_no and old.get("tracking_no") and old["tracking_no"] != tracking_no)):
            old = None
        # 无单号时也按会话和问题类型持久化；新订单使用独立案件身份。
        identity = json.dumps([req.user_id, req.conv_id, order_id or tracking_no or (old or {}).get("identity", "pending"), issue, req.request_id if new_task else ""], ensure_ascii=True)
        case_id = old["case_id"] if old else "LC-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        calls = []
        def call(name, args, result):
            calls.append({"agent_type": "logistics", "tool_name": name, "input": args, "source": "mock", "success": True, "result_success": result.get("success", True), "error": result.get("error", ""), "result": result})
            return result
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=?", (case_id, req.user_id)).fetchone()
            case = json.loads(row[0]) if row else {"case_id": case_id, "issue": issue, "order_id": None, "tracking_no": None, "identity": order_id or tracking_no or "pending", "state": "awaiting_info", "events": []}
            before = case["state"]
            case["order_id"] = order_id or case.get("order_id")
            case["tracking_no"] = tracking_no or case.get("tracking_no")
            policy = self.rules[issue]
            case["policy_version"] = RULE_VERSION
            case["source"] = "mock"
            # 已建单保留终态。每次返回相同工单，不重复操作。
            if case["state"] == "ticket_created":
                reply = "模拟工单已创建，编号：" + case["ticket_id"] + "。这不是承运商真实工单，未执行退款或赔偿。"
            elif case["state"] == "manual_review" and is_confirmed(req.message):
                reply = "案件待人工核查，不能直接确认建单。当前尚未通知真实客服。"
            elif not case["order_id"] and not case["tracking_no"]:
                case["state"] = "awaiting_info"
                reply = "请提供订单号或运单号，以便核验物流异常。当前使用模拟物流服务。"
            else:
                case["state"] = "verifying"
                found = call("query_order", {"order_id": case["order_id"], "tracking_no": case["tracking_no"]}, self.query_order(req.user_id, case["order_id"], case["tracking_no"]))
                if not found["success"]:
                    case["state"] = "awaiting_info"
                    reply = found["error"]
                else:
                    case["order_id"] = found["order"]["order_id"]
                    case["tracking_no"] = found["order"]["tracking_no"]
                    facts = call("query_logistics", {"order_id": case["order_id"]}, self.query_logistics(req.user_id, case["order_id"]))
                    case["facts"] = facts
                    case["policy"] = policy
                    call("lookup_logistics_policy", {"issue": issue}, {"success": True, "version": RULE_VERSION, "policy": policy})
                    if not facts["success"]:
                        case["state"] = "manual_review"
                        reply = "模拟物流查询失败，已保存案件，尚未创建工单。可稍后重试核验或转人工。"
                    else:
                        tracking = facts["tracking"]
                        conflict = issue == "logistics_dispute" and tracking["status"] != "delivered"
                        evidence = bool(re.search(r"(?:已上传|已提供|已提交).{0,12}(?:照片|图片|视频)", req.message)) and not bool(re.search(r"未|没有|没上传|没提供", req.message))
                        case["evidence_declared"] = case.get("evidence_declared", False) or evidence
                        if conflict:
                            case["state"] = "manual_review"
                            reply = "模拟轨迹尚未签收，与签收争议描述存在冲突，需要人工核查；未认定任何一方责任。"
                        elif issue == "logistics_damaged" and not case["evidence_declared"]:
                            case["state"] = "awaiting_evidence"
                            reply = "请提供外包装、面单和商品破损或错发情况的照片。演示版仅记录您声明已提交材料，不验证图片。"
                        elif not is_confirmed(req.message):
                            case["state"] = "awaiting_confirmation"
                            reply = "已按模拟轨迹核验。" + policy["action"] + "如同意创建模拟核查工单，请回复“确认建单”。"
                        else:
                            ticket_id = "MOCK-" + hashlib.sha256(case_id.encode()).hexdigest()[:16]
                            db.execute("INSERT OR IGNORE INTO tickets VALUES (?, ?)", (case_id, ticket_id))
                            case["ticket_id"] = db.execute("SELECT ticket_id FROM tickets WHERE case_id=?", (case_id,)).fetchone()[0]
                            call("create_logistics_case", {"case_id": case_id, "idempotency_key": case_id}, {"success": True, "ticket_id": case["ticket_id"]})
                            case["state"] = "ticket_created"
                            reply = "已创建模拟核查工单：" + case["ticket_id"] + "。未联系真实承运商，未执行退款或赔偿。"
            if case["state"] not in STATES:
                raise ValueError("非法案件状态")
            case["updated_at"] = datetime.now(timezone.utc).isoformat()
            case["events"].append({"request_id": req.request_id, "from": before, "to": case["state"], "at": case["updated_at"], "tools": [c["tool_name"] for c in calls]})
            case["events"] = case["events"][-100:]
            db.execute("INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?)", (case_id, req.user_id, req.conv_id, json.dumps(case, ensure_ascii=False)))
        req.conversation_state = dict(state, logistics_case_id=case_id, logistics_case_state=case["state"], logistics_issue=issue)
        return {"success": True, "source": "mock", "case": case, "reply": reply, "calls": calls, "escalate": case["state"] == "manual_review"}


def case_view(case):
    """给页面的最小只读快照，不暴露用户标识、完整轨迹或内部事件。"""
    if not case:
        return None
    state = case["state"]
    actions = {
        "awaiting_info": "请核对并补充订单号或运单号。",
        "verifying": "正在核验订单和物流信息。",
        "awaiting_evidence": "请提供外包装、面单和商品问题照片；当前仅记录提交声明。",
        "awaiting_confirmation": "资料已记录，可确认创建模拟核查工单。",
        "manual_review": "需要人工进一步核查，尚未通知真实客服。",
        "ticket_created": "模拟工单已创建；未联系承运商，未执行赔偿或退款。",
    }
    return {"case_id": case["case_id"], "issue": case["issue"], "state": state,
        "order_id": case.get("order_id"), "tracking_no": case.get("tracking_no"),
        "ticket_id": case.get("ticket_id"), "updated_at": case.get("updated_at"),
        "source": "mock", "next_step": actions[state],
        "required_evidence": case.get("policy", {}).get("required_evidence", []),
        "evidence_declared": case.get("evidence_declared", False),
        "can_confirm": state == "awaiting_confirmation"}
