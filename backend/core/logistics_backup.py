"""SQLite snapshots for logistics data; restore only into a new, stopped target."""
import argparse
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path


TABLES = ("cases", "tickets", "ticket_operations", "operation_reviews",
          "ticket_reconcile_schedule", "ticket_reconcile_status", "logistics_runs",
          "logistics_steps", "active_cases", "logistics_conversation_revisions")


def inspect_database(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES).issubset(tables):
        raise ValueError("Not a current DiAn logistics database")
    return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in TABLES}


def copy_database(source, target, *, timeout_seconds=30):
    """Use SQLite's backup API, including committed WAL pages. Never overwrite."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("Timeout must be positive and finite")
    source = Path(source).resolve(strict=True)
    target = Path(target).resolve()
    if source == target:
        raise ValueError("Source and target must differ")
    started = time.monotonic()

    def progress(_status, _remaining, _total):
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError("Backup deadline exceeded; target was not published as complete")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        # Reject unrelated databases without creating a target.
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES).issubset(tables):
            raise ValueError("Not a current DiAn logistics database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=.05)
                counts = inspect_database(dst)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"source": str(source), "target": str(target), "sha256": digest.hexdigest(),
            "integrity_check": "ok", "rows": counts,
            "elapsed_seconds": round(time.monotonic() - started, 4)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path, help="new file; existing targets are never overwritten")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        result = copy_database(args.source, args.target, timeout_seconds=args.timeout)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")
    print(json.dumps({"action": args.action, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
