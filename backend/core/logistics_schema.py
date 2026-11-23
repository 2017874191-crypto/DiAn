"""Version the existing logistics schema with one atomic legacy adoption migration."""
import json
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def ensure_schema(db):
    # Serialize first starts and reread the version under the same write lock.
    # Explicit BEGIN also makes DDL, data backfill and user_version roll back together.
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(f"Unsupported logistics schema version {version}; supported: {SCHEMA_VERSION}")
        _migrate_0_to_1(db)
        db.execute("PRAGMA user_version=1")


def _migrate_0_to_1(db):
    db.execute("CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conv_id TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS tickets (case_id TEXT PRIMARY KEY, ticket_id TEXT UNIQUE NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS ticket_operations (op_key TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL, conv_id TEXT NOT NULL, params_hash TEXT NOT NULL, params_json TEXT NOT NULL, confirmation_id TEXT NOT NULL, case_version TEXT NOT NULL, state TEXT NOT NULL, external_id TEXT, updated_at TEXT NOT NULL, created_at TEXT)")
    if not any(col[1] == "created_at" for col in db.execute("PRAGMA table_info(ticket_operations)")):
        db.execute("ALTER TABLE ticket_operations ADD COLUMN created_at TEXT")
        db.execute("UPDATE ticket_operations SET created_at=updated_at")
    db.execute("CREATE TABLE IF NOT EXISTS operation_reviews (op_key TEXT PRIMARY KEY, reason TEXT NOT NULL, actor_id TEXT NOT NULL, reviewed_at TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS ticket_reconcile_schedule (
        op_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_attempt_at TEXT NOT NULL,
        next_attempt_at TEXT, last_result TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS ticket_reconcile_status (
        id INTEGER PRIMARY KEY CHECK (id=1), last_completed_timestamp_seconds REAL NOT NULL
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS logistics_runs (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conv_id TEXT NOT NULL, request_id TEXT NOT NULL, input_hash TEXT NOT NULL, case_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS logistics_steps (run_id TEXT NOT NULL, step_name TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(run_id,step_name))")
    db.execute("CREATE TABLE IF NOT EXISTS active_cases (user_id TEXT NOT NULL, conv_id TEXT NOT NULL, case_id TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, conv_id))")
    db.execute("""CREATE TABLE IF NOT EXISTS logistics_conversation_revisions (
        user_id TEXT NOT NULL, conv_id TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY (user_id, conv_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS cases_order_issue ON cases "
               "(user_id, json_extract(payload, '$.order_id'), json_extract(payload, '$.issue')) "
               "WHERE json_valid(payload)")
    # Older databases have cases but no active pointer. Choose the last
    # updated case per owned conversation once; all future writes are explicit.
    latest = {}
    for rowid, case_id, user_id, conv_id, payload in db.execute(
            "SELECT c.rowid,c.id,c.user_id,c.conv_id,c.payload FROM cases c "
            "LEFT JOIN active_cases a ON a.user_id=c.user_id AND a.conv_id=c.conv_id "
            "WHERE a.case_id IS NULL"):
        try:
            case = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(case, dict):
            continue
        timestamp = case.get("updated_at") or ""
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        rank = (parsed, rowid)
        key = (user_id, conv_id)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, case_id, timestamp)
    for (user_id, conv_id), (_, case_id, timestamp) in latest.items():
        db.execute("INSERT OR IGNORE INTO active_cases VALUES (?,?,?,?)",
                   (user_id, conv_id, case_id, timestamp))
