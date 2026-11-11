"""Isolated, durable HTTP ticket simulator for integration/fault experiments only."""
import hashlib
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(host="127.0.0.1", port=18191, path=None, drop_once=False):
    db_path = Path(path or os.getenv("DIAN_TICKET_MOCK_DB", "data/logistics/mock-http-tickets.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS tickets (op_key TEXT PRIMARY KEY, params_hash TEXT NOT NULL, ticket_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS faults (op_key TEXT PRIMARY KEY)")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, body):
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not self.path.startswith("/tickets/"):
                return self.respond(404, {"error": "not found"})
            key = self.path[len("/tickets/"):]
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT params_hash,ticket_id FROM tickets WHERE op_key=?", (key,)).fetchone()
            return self.respond(200, {"params_hash": row[0], "ticket_id": row[1]}) if row else self.respond(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/tickets":
                return self.respond(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key, params_hash = body["key"], body["params_hash"]
                actual = hashlib.sha256(json.dumps(body["params"], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                if actual != params_hash:
                    return self.respond(400, {"error": "params hash mismatch"})
                with sqlite3.connect(db_path, timeout=5) as db:
                    db.execute("INSERT OR IGNORE INTO tickets VALUES (?,?,?)",
                               (key, params_hash, "HTTP-MOCK-" + hashlib.sha256(key.encode()).hexdigest()[:12]))
                    row = db.execute("SELECT params_hash,ticket_id FROM tickets WHERE op_key=?", (key,)).fetchone()
                    if row[0] != params_hash:
                        return self.respond(409, {"error": "operation key conflict"})
                    should_drop = bool(drop_once and db.execute(
                        "INSERT OR IGNORE INTO faults VALUES (?) RETURNING op_key", (key,)).fetchone())
            except (KeyError, ValueError, json.JSONDecodeError):
                return self.respond(400, {"error": "invalid request"})
            if should_drop:
                self.connection.close()  # Ticket committed; caller sees unknown outcome.
                return
            return self.respond(200, {"params_hash": row[0], "ticket_id": row[1]})

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve(host=os.getenv("DIAN_TICKET_MOCK_HOST", "127.0.0.1"),
          port=int(os.getenv("DIAN_TICKET_MOCK_PORT", "18191")),
          drop_once=os.getenv("DIAN_TICKET_MOCK_DROP_ONCE") == "true")
