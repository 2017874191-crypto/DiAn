"""Separate mock HTTP provider and killable Temporal worker processes."""
import asyncio
import json
import os
import socket
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from flow import TicketWorkflow
from store import db, mark, once, prepare


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with db() as c:
            row = c.execute('SELECT ticket FROM provider_tickets WHERE id=%s', (self.path[1:],)).fetchone()
        self.reply(200 if row else 404, {'ticket': row[0] if row else None})

    def do_POST(self):
        p = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with db() as c:
            c.execute('INSERT INTO provider_tickets VALUES (%s,%s) ON CONFLICT DO NOTHING', (p['key'], str(uuid.uuid4())))
            ticket = c.execute('SELECT ticket FROM provider_tickets WHERE id=%s', (p['key'],)).fetchone()[0]
        if p.get('drop') and once('drop:' + p['key']):
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.reply(200, {'ticket': ticket})


def provider(port):
    ThreadingHTTPServer(('127.0.0.1', port), Provider).serve_forever()


def crash(p, position):
    if p['fault'] == position and once('kill:' + p['op']):
        os._exit(86)  # Deliberate abrupt worker death, not a caught exception.


@activity.defn
async def execute_ticket(p: dict) -> dict:
    full = p['mode'] == 'full'
    if full:
        try:
            state, ticket = prepare(p)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        if state == 'succeeded':
            return {'ticket': ticket}
    crash(p, 'before')
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        if full:
            response = await client.get(p['url'] + '/' + p['op'])
            if response.status_code == 200:
                ticket = response.json()['ticket']
                mark(p['op'], 'succeeded', ticket)
                return {'ticket': ticket}
            response.raise_for_status() if response.status_code != 404 else None
            mark(p['op'], 'unknown')
        # Engine-only ablation intentionally lacks a stable provider key.
        key = p['op'] if full else p['op'] + '-' + str(uuid.uuid4())
        response = await client.post(p['url'], json={'key': key, 'drop': p['fault'] == 'drop'})
        response.raise_for_status()
        ticket = response.json()['ticket']
    crash(p, 'after')
    if full:
        mark(p['op'], 'succeeded', ticket)
    crash(p, 'committed')
    return {'ticket': ticket}


async def serve(queue):
    client = await Client.connect(os.environ.get('POC_TEMPORAL', 'localhost:17233'))
    async with Worker(client, task_queue=queue, workflows=[TicketWorkflow], activities=[execute_ticket]):
        await asyncio.Event().wait()


def worker(queue):
    asyncio.run(serve(queue))
