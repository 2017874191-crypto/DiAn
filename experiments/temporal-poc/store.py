"""PoC business ledger. Dedicated local database; never Temporal internal tables."""
import hashlib
import json
import os

import psycopg

DSN = os.environ.get("POC_DSN", "postgresql://poc@127.0.0.1:15439/poc?connect_timeout=5")


def db():
    return psycopg.connect(DSN)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS requests (id text PRIMARY KEY, hash text NOT NULL, payload text NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS cases (id text PRIMARY KEY, owner text NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS operations (id text PRIMARY KEY, case_id text NOT NULL REFERENCES cases(id), hash text NOT NULL, state text NOT NULL, ticket text)")
        c.execute("CREATE TABLE IF NOT EXISTS provider_tickets (id text PRIMARY KEY, ticket text NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS faults (id text PRIMARY KEY)")


def accept(request_id, payload):
    key = digest([payload['owner'], request_id])
    encoded = json.dumps(payload, sort_keys=True)
    with db() as c:
        c.execute("INSERT INTO requests VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (key, digest(payload), encoded))
        if c.execute("SELECT hash FROM requests WHERE id=%s", (key,)).fetchone()[0] != digest(payload):
            raise ValueError("request ID reused with different input")
    return 'dian-poc-' + key


def once(key):
    with db() as c:
        return c.execute("INSERT INTO faults VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", (key,)).fetchone() is not None


def prepare(p):
    if not p.get('confirmed'):
        raise ValueError('confirmation required')
    with db() as c:
        c.execute("INSERT INTO cases VALUES (%s,%s) ON CONFLICT DO NOTHING", (p['case'], p['owner']))
        if c.execute("SELECT owner FROM cases WHERE id=%s", (p['case'],)).fetchone()[0] != p['owner']:
            raise ValueError('case owner mismatch')
        c.execute("INSERT INTO operations VALUES (%s,%s,%s,'prepared',NULL) ON CONFLICT DO NOTHING", (p['op'], p['case'], digest(p['params'])))
        row = c.execute("SELECT hash,state,ticket,case_id FROM operations WHERE id=%s", (p['op'],)).fetchone()
        if row[0] != digest(p['params']) or row[3] != p['case']:
            raise ValueError('operation key conflict')
        return row[1], row[2]


def mark(op, state, ticket=None):
    with db() as c:
        c.execute("UPDATE operations SET state=%s,ticket=%s WHERE id=%s AND state<>'succeeded'", (state, ticket, op))
