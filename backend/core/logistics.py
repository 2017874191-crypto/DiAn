"""物流案件领域服务；默认同库建单，可选独立 HTTP 模拟工单。"""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from core.logistics_providers import (DeclarationEvidenceProvider, MockOrderProvider,
    MockTrackingProvider, SQLiteTicketProvider, HttpTicketProvider)

ISSUES = {"logistics_lost", "logistics_dispute", "logistics_damaged"}
RULE_VERSION = "demo-2026-11-v1"
STATES = {"awaiting_info", "verifying", "awaiting_evidence", "awaiting_confirmation", "manual_review", "ticket_pending", "ticket_unknown", "ticket_created", "ticket_already_open"}

_NOT_RECEIVED = re.compile(r"(?:没(?:有)?|未)收到(?:货|包裹|快递|商品)")
_LOSS_CHECK = re.compile(r"(?:核实|核查|确认|查一下)\s*(?:一下\s*)?(?:是否|是不是|会不会)(?:已经|已)?(?:遗失|丢失|丢件)")
_CLARIFICATION_EXCLUSIONS = re.compile(
    r"运费|邮费|配送费用|寄送费|物流费用|(?:只想|只是|仅想|就是想)(?:查|查询|看看)(?:一下)?(?:物流|运单|快递|包裹)"
    r"|(?:不是|并非|没有说|没说|没有要求|未要求|不需要|不必|不用|无需|不要|不想|不打算|暂不|不|别)"
    r"(?:要|想|说|表示|认为)?\s*(?:帮我|我|包裹|快递)?\s*(?:没(?:有)?收到|未收到|核实|核查|确认|查)"
    r"|我(?:已|已经|确实)收到|我收到了"
)


def needs_missing_delivery_clarification(message, order_ids):
    """An explicit but weak loss inquiry can invite clarification only."""
    return (len(order_ids) == 1 and not _CLARIFICATION_EXCLUSIONS.search(message)
            and bool(_NOT_RECEIVED.search(message) and _LOSS_CHECK.search(message)))


class StaleLogisticsWrite(RuntimeError):
    pass


def detect_issue(message):
    # 用户明确否认的异常不能作为建案依据；保留同句中其他肯定异常。
    message = re.sub(r"(?:不是|并非|没有|没发生)(?:要查询|包裹|快递|物流|发生|出现|的)?(?:丢件|丢失)", "", message)
    message = re.sub(r"(?:不是|并非|没有|没发生)(?:包裹|快递|商品|发生|出现|的)?(?:破损|损坏|错发|发错货)", "", message)
    if re.search(r"签收.{0,10}(没收到|未收到|没拿到)|没收到.{0,10}签收|被代签", message):
        return "logistics_dispute"
    if any(x in message for x in ("破损", "错发", "发错货", "包裹损坏")):
        return "logistics_damaged"
    if any(x in message for x in ("丢件", "丢失", "物流一直不更新", "物流不更新", "快递不更新")):
        return "logistics_lost"
    return None

_CONFIRMATION_TARGETS = {
    "dispute": "logistics_dispute", "lost": "logistics_lost", "damage": "logistics_damaged",
}
_CONFIRMATION_ACTION = re.compile(
    r"建单|(?:创建|提交|申请)(?:(?:刚才|上述|前面)(?:说的|说明的)?)?"
    r"(?:(?:这|该|当前)(?:一)?(?:个|张|笔|单)?(?:订单)?的?)?(?:模拟)?"
    r"(?:(?:人工审核|核查)?工单|(?:(?P<dispute>签收争议)|(?P<lost>丢件)|(?P<damage>破损错发|破损|错发))"
    r"(?:模拟)?(?:人工)?(?:核查|审核))"
)


def is_confirmed(message, issue=None):
    """Recognize a bounded explicit assent; the persisted offer still authorizes the action."""
    text = re.sub(r"\s+", "", message).rstrip("。！!")
    assent = re.match(r"(?:我|本人)?(?:确认|同意)[，,]?", text)
    if assent:
        text = text[assent.end():]
        if not text:
            return True  # Only usable with the existing prior-offer and target checks.
    elif text.startswith("请"):
        text = text[1:]
    else:
        return False
    reference = re.match(r"(?:就)?按(?:你)?(?:刚才|上述|前面)(?:说明的)?(?:事项|方案)", text)
    if reference:
        text = text[reference.end():]
    text = text.removeprefix("请")
    action = _CONFIRMATION_ACTION.fullmatch(text)
    target = next((value for group, value in _CONFIRMATION_TARGETS.items() if action and action.group(group)), None)
    return bool(action and (not issue or not target or target == issue))


def is_confirmation_reply(message):
    """Route assent, refusal and questions to the current case without authorizing them."""
    return (is_confirmed(message)
            or bool(re.fullmatch(r"\s*(?:不同意|不确认|暂不确认|暂缓|取消)[。！!]?\s*", message))
            or bool(re.search(r"确认建单|创建工单|提交工单|(?:确认|同意).*(?:建单|工单|核查|审核|申请)", message)))

class LogisticsService:
    def __init__(self, db_path=None, fixtures=None, *, order_provider=None,
                 tracking_provider=None, ticket_provider=None, evidence_provider=None):
        self.path = Path(db_path or os.getenv("DIAN_LOGISTICS_DB", "data/logistics/cases.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        source = Path(fixtures or os.getenv("DIAN_LOGISTICS_FIXTURES", str(Path(__file__).resolve().parents[1] / "config/logistics/fixtures.json")))
        self.orders = json.loads(source.read_text(encoding="utf-8"))["orders"]
        if order_provider is not None and tracking_provider is None:
            raise ValueError("自定义订单提供者必须同时指定物流提供者")
        self.order_provider = order_provider or MockOrderProvider(self.orders)
        self.tracking_provider = tracking_provider or MockTrackingProvider(self.orders, self.order_provider)
        self.ticket_provider = ticket_provider or SQLiteTicketProvider()
        self.http_ticket_provider = (HttpTicketProvider(os.environ["DIAN_TICKET_URL"])
            if ticket_provider is None and os.getenv("DIAN_TICKET_PROVIDER") == "http_mock" else None)
        self.evidence_provider = evidence_provider or DeclarationEvidenceProvider()
        self.rules = json.loads((Path(__file__).resolve().parents[1] / "config/logistics/policies.json").read_text(encoding="utf-8"))
        from core.logistics_schema import ensure_schema
        with self.connect() as db:
            ensure_schema(db)

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
        return self.order_provider.query_order(user_id, order_id, tracking_no)

    def query_logistics(self, user_id, order_id):
        return self.tracking_provider.query_logistics(user_id, order_id)

    def get_case(self, user_id, case_id, conv_id=None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=?" + (" AND conv_id=?" if conv_id is not None else ""),
                (case_id, user_id, conv_id) if conv_id is not None else (case_id, user_id)).fetchone()
        return json.loads(row[0]) if row else None

    def get_active_case(self, user_id, conv_id):
        """Read the authoritative conversation pointer and recheck case ownership."""
        with self.connect() as db:
            row = db.execute("SELECT c.payload FROM active_cases a JOIN cases c "
                             "ON c.id=a.case_id AND c.user_id=a.user_id AND c.conv_id=a.conv_id "
                             "WHERE a.user_id=? AND a.conv_id=?", (user_id, conv_id)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _check_revision(db, user_id, conv_id, expected):
        row = db.execute("SELECT revision FROM logistics_conversation_revisions WHERE user_id=? AND conv_id=?",
                         (user_id, conv_id)).fetchone()
        current = row[0] if row else 0
        if expected is not None and expected != current:
            raise StaleLogisticsWrite("物流案件已由更新的请求推进，请查询当前案件状态")
        return current

    def get_revision(self, user_id, conv_id):
        with self.connect() as db:
            return self._check_revision(db, user_id, conv_id, None)

    @classmethod
    def _advance_revision(cls, db, req):
        # The caller holds the SQLite write transaction through the domain commit.
        current = cls._check_revision(db, req.user_id, req.conv_id, getattr(req, "logistics_revision", None))
        revision = current + 1
        db.execute("""INSERT INTO logistics_conversation_revisions (user_id,conv_id,revision) VALUES (?,?,?)
                      ON CONFLICT(user_id,conv_id) DO UPDATE SET revision=excluded.revision""",
                   (req.user_id, req.conv_id, revision))
        return revision

    @staticmethod
    def run_id(req):
        identity = req.client_request_id or [req.request_id, hashlib.sha256(req.message.encode()).hexdigest()]
        raw = json.dumps([req.user_id, req.conv_id, identity], ensure_ascii=True)
        return "LR-" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    @staticmethod
    def _run_state(case_state):
        return {"awaiting_info": "waiting_input", "awaiting_evidence": "waiting_input",
                "awaiting_confirmation": "waiting_confirmation", "ticket_pending": "reconciling",
                "ticket_unknown": "outcome_unknown", "ticket_created": "completed",
                "ticket_already_open": "waiting_input", "manual_review": "manual_review"}.get(case_state, "running")

    @staticmethod
    def _write_run(db, req, run_id, case):
        now = datetime.now(timezone.utc).isoformat()
        input_hash = hashlib.sha256(req.message.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO logistics_runs VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, req.user_id, req.conv_id, req.request_id, input_hash,
                    case["case_id"], "running", now, now))
        prior = db.execute("SELECT user_id,conv_id,input_hash FROM logistics_runs WHERE run_id=?", (run_id,)).fetchone()
        if prior != (req.user_id, req.conv_id, input_hash):
            raise ValueError("同一运行标识不能用于不同请求")
        LogisticsService._sync_existing_run(db, run_id, case, now)
        db.execute("UPDATE logistics_steps SET state='succeeded',updated_at=? WHERE run_id=? AND step_name='dispatch'",
                   (now, run_id))

    @staticmethod
    def reserve_run(db, req, request_id=None):
        """Called in the same transaction as Temporal's accepted dispatch row."""
        run_id = LogisticsService.run_id(req)
        now = datetime.now(timezone.utc).isoformat()
        input_hash = hashlib.sha256(req.message.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO logistics_runs VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, req.user_id, req.conv_id, request_id or req.request_id, input_hash,
                    None, "accepted", now, now))
        prior = db.execute("SELECT user_id,conv_id,input_hash FROM logistics_runs WHERE run_id=?", (run_id,)).fetchone()
        if prior != (req.user_id, req.conv_id, input_hash):
            raise ValueError("同一运行标识不能用于不同请求")
        db.execute("INSERT OR IGNORE INTO logistics_steps VALUES (?,?,?,?)",
                   (run_id, "dispatch", "pending", now))
        return run_id

    @staticmethod
    def _sync_existing_run(db, run_id, case, now=None):
        now = now or datetime.now(timezone.utc).isoformat()
        state = LogisticsService._run_state(case["state"])
        db.execute("UPDATE logistics_runs SET case_id=?,state=?,updated_at=? WHERE run_id=?",
                   (case["case_id"], state, now, run_id))
        db.execute("INSERT INTO logistics_steps VALUES (?,?,?,?) ON CONFLICT(run_id,step_name) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                   (run_id, "case_decision", "succeeded", now))
        if case["state"] in ("ticket_pending", "ticket_unknown", "ticket_created"):
            step_state = {"ticket_pending": "pending", "ticket_unknown": "unknown", "ticket_created": "succeeded"}[case["state"]]
            db.execute("INSERT INTO logistics_steps VALUES (?,?,?,?) ON CONFLICT(run_id,step_name) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                       (run_id, "ticket_result", step_state, now))

    def get_run(self, user_id, conv_id, run_id):
        with self.connect() as db:
            row = db.execute("SELECT run_id,request_id,case_id,state,created_at,updated_at FROM logistics_runs WHERE run_id=? AND user_id=? AND conv_id=?",
                             (run_id, user_id, conv_id)).fetchone()
            if not row:
                return None
            steps = db.execute("SELECT step_name,state,updated_at FROM logistics_steps WHERE run_id=? ORDER BY step_name", (run_id,)).fetchall()
        return {"run_id": row[0], "request_id": row[1], "case_id": row[2], "state": row[3],
                "created_at": row[4], "updated_at": row[5],
                "steps": [{"name": name, "state": state, "updated_at": at} for name, state, at in steps]}

    def hold_operation(self, op_key, reason, actor_id="local-admin"):
        """Admin decision: stop retries and require external/manual result verification."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT case_id,user_id,conv_id,state FROM ticket_operations WHERE op_key=?", (op_key,)).fetchone()
            if not row:
                return None
            case_id, user_id, conv_id, state = row
            if state not in ("prepared", "unknown", "held"):
                raise ValueError("操作已结束，不能挂起")
            payload = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=? AND conv_id=?",
                                 (case_id, user_id, conv_id)).fetchone()
            if not payload:
                raise RuntimeError("操作对应案件缺失")
            case = json.loads(payload[0])
            if case["state"] not in ("ticket_pending", "ticket_unknown", "manual_review"):
                raise ValueError("案件状态不允许挂起")
            now = datetime.now(timezone.utc).isoformat()
            db.execute("UPDATE ticket_operations SET state='held',updated_at=? WHERE op_key=?", (now, op_key))
            db.execute("INSERT INTO operation_reviews VALUES (?,?,?,?) ON CONFLICT(op_key) DO UPDATE SET reason=excluded.reason,actor_id=excluded.actor_id,reviewed_at=excluded.reviewed_at",
                       (op_key, reason, actor_id, now))
            before = case["state"]
            case["state"] = "manual_review"
            case["updated_at"] = now
            case["events"].append({"request_id": "admin-hold", "from": before,
                                   "to": "manual_review", "at": now, "reason": "operation_held"})
            case["events"] = case["events"][-100:]
            db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case, ensure_ascii=False), case_id))
            for (run_id,) in db.execute("SELECT run_id FROM logistics_runs WHERE case_id=? AND state IN ('reconciling','outcome_unknown')", (case_id,)):
                self._sync_existing_run(db, run_id, case)
                db.execute("UPDATE logistics_steps SET state='held',updated_at=? WHERE run_id=? AND step_name='ticket_result'", (now, run_id))
        return {"op_key": op_key, "state": "held", "case_id": case_id, "reviewed_at": now}

    @staticmethod
    def _has_other_open_ticket(db, user_id, case):
        # Both providers check under BEGIN IMMEDIATE. HTTP persists its intent
        # before releasing that transaction, so even an unknown result reserves the scope.
        return db.execute(
            "SELECT 1 FROM cases c WHERE c.user_id=? AND c.id<>? AND json_valid(c.payload) "
            "AND json_extract(c.payload, '$.order_id')=? AND json_extract(c.payload, '$.issue')=? "
            "AND json_extract(c.payload, '$.closed_at') IS NULL AND ("
            "json_extract(c.payload, '$.ticket_id') IS NOT NULL OR "
            "EXISTS (SELECT 1 FROM tickets WHERE case_id=c.id) OR "
            "EXISTS (SELECT 1 FROM ticket_operations WHERE case_id=c.id)) LIMIT 1",
            (user_id, case["case_id"], case["order_id"], case["issue"]),
        ).fetchone() is not None

    def close_ticket_case(self, case_id, reason, actor_id, *, user_id, conv_id):
        reason = reason.strip()
        if not 10 <= len(reason) <= 500:
            raise ValueError("结案原因长度须为 10 至 500 个字符")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT user_id,conv_id,payload FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row or row[:2] != (user_id, conv_id):
                return None
            case = json.loads(row[2])
            if case["state"] != "ticket_created" or not case.get("ticket_id"):
                raise ValueError("只有已确认建单结果的案件可以结案")
            if not case.get("closed_at"):
                now = datetime.now(timezone.utc).isoformat()
                case.update(closed_at=now, closed_by=actor_id, close_reason=reason, updated_at=now)
                db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case, ensure_ascii=False), case_id))
        return {"case_id": case_id, "closed_at": case["closed_at"]}

    @staticmethod
    def _ticket_params(case):
        return {"case_id": case["case_id"], "issue": case["issue"],
                "order_id": case["order_id"], "tracking_no": case["tracking_no"],
                "policy_version": case["policy_version"]}

    def _prepare_ticket_operation(self, db, req, case):
        params = self._ticket_params(case)
        encoded = json.dumps(params, sort_keys=True, ensure_ascii=True)
        params_hash = hashlib.sha256(encoded.encode()).hexdigest()
        key = hashlib.sha256(json.dumps([req.user_id, case["case_id"], "create_ticket"],
                                         ensure_ascii=True).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        confirmation_version = case.get("updated_at") or now
        db.execute("INSERT OR IGNORE INTO ticket_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key, case["case_id"], req.user_id, req.conv_id, params_hash, encoded,
                    req.request_id, confirmation_version, "prepared", None, now, now))
        row = db.execute("SELECT user_id,conv_id,case_id,params_hash FROM ticket_operations WHERE op_key=?", (key,)).fetchone()
        if row != (req.user_id, req.conv_id, case["case_id"], params_hash):
            raise ValueError("工单操作键与已确认的案件参数冲突")
        case["ticket_confirmation_version"] = confirmation_version
        case["ticket_confirmation_id"] = req.request_id
        return key

    def _complete_ticket_operation(self, req, case_id, *, expected_revision=None):
        with self.connect() as db:
            row = db.execute("SELECT op_key,params_hash,params_json,state,external_id,case_version,confirmation_id FROM ticket_operations "
                             "WHERE case_id=? AND user_id=? AND conv_id=?", (case_id, req.user_id, req.conv_id)).fetchone()
        if not row:
            raise RuntimeError("工单操作记录缺失，不能执行外部调用")
        key, params_hash, params_json, state, external_id, case_version, confirmation_id = row
        if state == "held":
            return self.get_case(req.user_id, case_id, req.conv_id), None
        if state == "succeeded":
            return self.get_case(req.user_id, case_id, req.conv_id), external_id
        current = self.get_case(req.user_id, case_id, req.conv_id)
        if (not current or current.get("ticket_confirmation_version") != case_version
                or current.get("ticket_confirmation_id") != confirmation_id
                or hashlib.sha256(json.dumps(self._ticket_params(current), sort_keys=True,
                                             ensure_ascii=True).encode()).hexdigest() != params_hash):
            return self._finish_ticket_operation(req, case_id, key, None, expected_revision=expected_revision), None
        try:
            found = self.http_ticket_provider.lookup(key)
            if found is None:
                found = self.http_ticket_provider.create(key, json.loads(params_json), params_hash)
            if found.get("params_hash") != params_hash or not found.get("ticket_id"):
                raise RuntimeError("外部工单结果与已确认参数不一致")
        except Exception:
            # Timeout, disconnect and provider errors are ambiguous. Never blind-retry here.
            return self._finish_ticket_operation(req, case_id, key, None, expected_revision=expected_revision), None
        finished = self._finish_ticket_operation(req, case_id, key, found["ticket_id"],
                                                 expected_revision=expected_revision)
        return finished, finished.get("ticket_id") if finished["state"] == "ticket_created" else None

    def _finish_ticket_operation(self, req, case_id, key, ticket_id, *, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=? AND conv_id=?",
                             (case_id, req.user_id, req.conv_id)).fetchone()
            op = db.execute("SELECT state,external_id,params_hash,case_version,confirmation_id FROM ticket_operations WHERE op_key=? AND user_id=? AND conv_id=?",
                            (key, req.user_id, req.conv_id)).fetchone()
            if not row or not op:
                raise RuntimeError("案件或操作归属已变化")
            case = json.loads(row[0])
            if op[0] == "succeeded":
                return case
            if op[0] == "held":
                if ticket_id and not op[1]:
                    db.execute("UPDATE ticket_operations SET external_id=? WHERE op_key=?", (ticket_id, key))
                return case
            if expected_revision is not None:
                self._check_revision(db, req.user_id, req.conv_id, expected_revision)
            now = datetime.now(timezone.utc).isoformat()
            observed_ticket_id = ticket_id or op[1]
            if ticket_id and (case.get("ticket_confirmation_version") != op[3]
                    or case.get("ticket_confirmation_id") != op[4]
                    or hashlib.sha256(json.dumps(self._ticket_params(case), sort_keys=True,
                                                 ensure_ascii=True).encode()).hexdigest() != op[2]):
                ticket_id = None
            target = "ticket_created" if ticket_id else "ticket_unknown"
            if ticket_id:
                case["ticket_id"] = ticket_id
            before = case["state"]
            case["state"] = target
            case["updated_at"] = now
            case["events"].append({"request_id": req.request_id, "from": before, "to": target,
                                   "at": now, "reason": "http_ticket_result"})
            case["events"] = case["events"][-100:]
            db.execute("UPDATE ticket_operations SET state=?,external_id=?,updated_at=? WHERE op_key=?",
                       ("succeeded" if ticket_id else "unknown", observed_ticket_id, now, key))
            db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case, ensure_ascii=False), case_id))
            run_ids = {case.get("last_run_id")}
            if ticket_id:
                run_ids.update(row[0] for row in db.execute(
                    "SELECT run_id FROM logistics_runs WHERE case_id=? AND user_id=? AND conv_id=? "
                    "AND state IN ('reconciling','outcome_unknown')",
                    (case_id, req.user_id, req.conv_id)))
            for run_id in run_ids - {None}:
                self._sync_existing_run(db, run_id, case)
        return case

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
            self._advance_revision(db, req)
            before = case["state"]
            case["state"] = "manual_review"
            case["updated_at"] = datetime.now(timezone.utc).isoformat()
            case["events"].append({"request_id": req.request_id, "from": before, "to": "manual_review", "at": case["updated_at"], "reason": "user_requested_handoff"})
            case["events"] = case["events"][-100:]
            db.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps(case, ensure_ascii=False), case_id))
            db.execute("INSERT INTO active_cases VALUES (?,?,?,?) ON CONFLICT(user_id,conv_id) DO UPDATE SET case_id=excluded.case_id, updated_at=excluded.updated_at",
                       (req.user_id, req.conv_id, case_id, case["updated_at"]))
        req.conversation_state["logistics_case_state"] = "manual_review"

    def process(self, req):
        """协调层每轮执行一次，LLM 不能指定案件状态或伪造核验结果。"""
        state = req.conversation_state or {}
        old_id = state.get("logistics_case_id")
        old = self.get_active_case(req.user_id, req.conv_id)
        if old is None and old_id:
            old = self.get_case(req.user_id, old_id, req.conv_id)
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
        run_id = self.run_id(req)
        calls = []
        needs_http_ticket = False
        def call(name, args, result):
            calls.append({"agent_type": "logistics", "tool_name": name, "input": args, "source": result.get("source", "mock"), "success": True, "result_success": result.get("success", True), "error": result.get("error", ""), "result": result})
            return result
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = self._advance_revision(db, req)
            row = db.execute("SELECT payload FROM cases WHERE id=? AND user_id=?", (case_id, req.user_id)).fetchone()
            case = json.loads(row[0]) if row else {"case_id": case_id, "issue": issue, "order_id": None, "tracking_no": None, "identity": order_id or tracking_no or "pending", "state": "awaiting_info", "events": []}
            case["last_run_id"] = run_id
            before = case["state"]
            previously_confirmable = before == "awaiting_confirmation" and case.get("policy_version") == RULE_VERSION
            prior_target = (case.get("order_id"), case.get("tracking_no"))
            case["order_id"] = order_id or case.get("order_id")
            case["tracking_no"] = tracking_no or case.get("tracking_no")
            policy = self.rules[issue]
            if issue == "logistics_damaged":
                # Old pending cases may have passed the former boolean-only gate.
                previously_confirmable = previously_confirmable and bool(case.get("evidence_declared")) and set(
                    policy["required_evidence"]).issubset(case.get("declared_materials", []))
            if case["state"] not in ("ticket_pending", "ticket_unknown"):
                case["policy_version"] = RULE_VERSION
            case["source"] = "mock"
            held = db.execute("SELECT 1 FROM ticket_operations WHERE case_id=? AND state='held'", (case_id,)).fetchone() is not None
            # 已建单保留终态。每次返回相同工单，不重复操作。
            if case["state"] == "ticket_created":
                if case.get("closed_at"):
                    reply = "本案件已结案，模拟工单编号：" + case["ticket_id"] + "。如需再次申请，请说明是新的问题并提供订单号。"
                else:
                    reply = "模拟工单已创建，编号：" + case["ticket_id"] + "。这不是承运商真实工单，未执行退款或赔偿。"
            elif held:
                case["state"] = "manual_review"
                reply = "工单操作已挂起，需人工核对远端结果；不会自动重试建单。"
            elif case["state"] in ("ticket_pending", "ticket_unknown"):
                needs_http_ticket = self.http_ticket_provider is not None
                reply = "模拟工单结果待核对，请稍后查询；不要重复提交新的建单操作。"
            elif case["state"] == "manual_review" and is_confirmed(req.message, issue):
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
                    case["tracking_no"] = found["order"].get("tracking_no")
                    # Submitting damage evidence for manual review does not establish transport liability.
                    # Keep carrier verification mandatory for loss and signature-dispute cases.
                    if issue == "logistics_damaged":
                        facts = {"query_performed": False, "reason": "not_required_for_damage_review"}
                    else:
                        facts = call("query_logistics", {"order_id": case["order_id"]}, self.query_logistics(req.user_id, case["order_id"]))
                    case["facts"] = facts
                    case["policy"] = policy
                    call("lookup_logistics_policy", {"issue": issue}, {"success": True, "version": RULE_VERSION, "policy": policy})
                    if issue != "logistics_damaged" and not facts["success"]:
                        case["state"] = "manual_review"
                        reply = "模拟物流查询失败，已保存案件，尚未创建工单。可稍后重试核验或转人工。"
                    else:
                        conflict = issue == "logistics_dispute" and facts["tracking"]["status"] != "delivered"
                        case["evidence_declared"] = self.evidence_provider.declared(req.message, case.get("evidence_declared", False))
                        if issue == "logistics_damaged":
                            names = DeclarationEvidenceProvider.material_names(req.message)
                            case["declared_materials"] = (list(dict.fromkeys(case.get("declared_materials", []) + names))
                                                          if case["evidence_declared"] else [])
                        if conflict:
                            case["state"] = "manual_review"
                            reply = "模拟轨迹尚未签收，与签收争议描述存在冲突，需要人工核查；未认定任何一方责任。"
                        elif issue == "logistics_damaged" and (not case["evidence_declared"] or not set(
                                policy["required_evidence"]).issubset(case["declared_materials"])):
                            case["state"] = "awaiting_evidence"
                            missing = [name for name in policy["required_evidence"] if name not in case["declared_materials"]]
                            reply = "尚缺材料提交声明：" + "、".join(missing) + "。请补充。演示版仅记录您声明已提交材料，不验证图片。"
                        elif self._has_other_open_ticket(db, req.user_id, case):
                            case["state"] = "ticket_already_open"
                            reply = "这笔订单的同类问题已有工单或正在核对建单结果。请回原会话查看进展，结案后可重新申请。"
                        elif (not is_confirmed(req.message, issue) or not previously_confirmable
                              or prior_target != (case["order_id"], case["tracking_no"])):
                            case["state"] = "awaiting_confirmation"
                            verification = ("已核验订单归属并记录材料提交声明，未验证图片真实性、未查询物流。"
                                            if issue == "logistics_damaged" else "已按模拟轨迹核验。")
                            reply = verification + policy["action"] + "如同意提交，可回复“我同意提交工单”或“确认建单”。"
                        else:
                            if self.http_ticket_provider is not None:
                                self._prepare_ticket_operation(db, req, case)
                                case["state"] = "ticket_pending"
                                needs_http_ticket = True
                                reply = "模拟工单正在创建；结果尚待外部服务核对。"
                            else:
                                created = self.ticket_provider.create_ticket(db, case_id, case_id)
                                if not created.get("success") or not created.get("ticket_id"):
                                    raise RuntimeError("工单提供者未确认建单结果")
                                case["ticket_id"] = created["ticket_id"]
                                call("create_logistics_case", {"case_id": case_id, "idempotency_key": case_id}, created)
                                case["state"] = "ticket_created"
                                reply = "已创建模拟核查工单：" + case["ticket_id"] + "。未联系真实承运商，未执行退款或赔偿。"
            if case["state"] not in STATES:
                raise ValueError("非法案件状态")
            case["updated_at"] = datetime.now(timezone.utc).isoformat()
            case["events"].append({"request_id": req.request_id, "from": before, "to": case["state"], "at": case["updated_at"], "tools": [c["tool_name"] for c in calls]})
            case["events"] = case["events"][-100:]
            db.execute("INSERT OR REPLACE INTO cases VALUES (?, ?, ?, ?)", (case_id, req.user_id, req.conv_id, json.dumps(case, ensure_ascii=False)))
            db.execute("INSERT INTO active_cases VALUES (?,?,?,?) ON CONFLICT(user_id,conv_id) DO UPDATE SET case_id=excluded.case_id, updated_at=excluded.updated_at",
                       (req.user_id, req.conv_id, case_id, case["updated_at"]))
            self._write_run(db, req, run_id, case)
        if needs_http_ticket:
            expected = revision if getattr(req, "logistics_revision", None) is not None else None
            case, ticket_id = self._complete_ticket_operation(req, case_id, expected_revision=expected)
            if ticket_id:
                tool = "reconcile_logistics_ticket" if before in ("ticket_pending", "ticket_unknown") else "create_logistics_case"
                call(tool, {"case_id": case_id},
                     {"success": True, "source": "http_mock", "ticket_id": ticket_id})
                reply = "已创建模拟核查工单：" + ticket_id + "。未联系真实承运商，未执行退款或赔偿。"
            else:
                reply = "模拟工单结果暂未确定，已记录待核对；请稍后查询，不要另建工单。"
        if issue == "logistics_damaged" and case["state"] == "ticket_created":
            reply += "当前仅记录材料提交声明，未验证图片真实性、未判定责任。"
        req.conversation_state = dict(state, logistics_case_id=case_id, logistics_case_state=case["state"], logistics_issue=issue)
        return {"success": True, "source": "mock", "case": case, "run_id": run_id, "reply": reply, "calls": calls, "escalate": case["state"] == "manual_review"}


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
        "ticket_pending": "模拟工单处理中；尚未确认外部结果。",
        "ticket_unknown": "模拟工单结果待核对；不要重复提交新建单操作。",
        "ticket_created": "模拟工单已创建；未联系承运商，未执行赔偿或退款。",
        "ticket_already_open": "请回原会话查看工单进展，结案后可重新申请。",
    }
    if case.get("closed_at"):
        actions["ticket_created"] = "本案件已结案。如需再次申请，请说明是新的问题并提供订单号。"
    if case["issue"] == "logistics_damaged":
        missing = [name for name in case.get("policy", {}).get("required_evidence", [])
                   if name not in case.get("declared_materials", [])]
        actions["awaiting_evidence"] = "尚缺材料提交声明：" + "、".join(missing) + "；当前不验证图片。"
        actions["verifying"] = "正在核验订单归属和材料提交声明。"
        actions["awaiting_confirmation"] = "仅记录材料提交声明，未验证图片真实性；可确认创建模拟人工审核工单。"
        actions["ticket_created"] += "仅记录材料提交声明，未验证图片真实性、未判定责任。"
    return {"case_id": case["case_id"], "issue": case["issue"], "state": state,
        "run_id": case.get("last_run_id"),
        "order_id": case.get("order_id"), "tracking_no": case.get("tracking_no"),
        "ticket_id": case.get("ticket_id"), "updated_at": case.get("updated_at"),
        "source": "mock", "next_step": actions[state],
        "required_evidence": case.get("policy", {}).get("required_evidence", []),
        "evidence_declared": case.get("evidence_declared", False),
        "declared_materials": case.get("declared_materials", []),
            "can_confirm": state == "awaiting_confirmation",
            **({"closed_at": case["closed_at"]} if case.get("closed_at") else {})}
