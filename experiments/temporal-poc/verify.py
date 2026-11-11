"""Real-server acceptance: isolated provider + abrupt worker death + history replay."""
import asyncio
import json
import multiprocessing as mp
import os
import socket
import time
import uuid
from pathlib import Path

import httpx
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Replayer
from temporalio.testing import WorkflowEnvironment

from flow import TicketWorkflow
from services import provider, worker
from store import accept, db, init, prepare


async def run(client):
    init()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = mp.Process(target=provider, args=(port,))
    server.start()
    processes = [server]
    report = {'timestamp': time.time(), 'results': []}
    try:
        async with httpx.AsyncClient(trust_env=False) as h:
            for _ in range(50):
                try:
                    await h.get(f'http://127.0.0.1:{port}/ready')
                    break
                except httpx.TransportError:
                    await asyncio.sleep(.1)
            else:
                raise RuntimeError('provider failed to start')
        for mode, fault in [('engine', 'after'), ('full', 'none'), ('full', 'before'), ('full', 'after'), ('full', 'committed'), ('full', 'drop')]:
            token = uuid.uuid4().hex
            queue = 'poc-' + token
            p = dict(owner='test-user', case=token, op=token, params={'action': 'create_ticket'}, confirmed=True, mode=mode, fault=fault, url=f'http://127.0.0.1:{port}')
            wid = accept(token, p)
            assert accept(token, p) == wid
            try:
                accept(token, {**p, 'confirmed': False})
            except ValueError:
                pass
            else:
                raise AssertionError('request input conflict not rejected')
            w = mp.Process(target=worker, args=(queue,)); w.start(); processes.append(w)
            started = time.monotonic()
            handle = await client.start_workflow(TicketWorkflow.run, p, id=wid, task_queue=queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            try:
                await client.start_workflow(TicketWorkflow.run, p, id=wid, task_queue=queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            except WorkflowAlreadyStartedError:
                pass
            else:
                raise AssertionError('duplicate workflow started')
            if fault in ('before', 'after', 'committed'):
                for _ in range(200):
                    if not w.is_alive():
                        break
                    await asyncio.sleep(.1)
                assert w.exitcode == 86, f'fault was not reached: {w.exitcode}'
                w = mp.Process(target=worker, args=(queue,)); w.start(); processes.append(w)
            result = await asyncio.wait_for(handle.result(), timeout=45)
            await Replayer(workflows=[TicketWorkflow]).replay_workflow(await handle.fetch_history())
            with db() as c:
                count = c.execute('SELECT count(*) FROM provider_tickets WHERE id LIKE %s', (token + '%',)).fetchone()[0]
                if mode == 'full':
                    row = c.execute('SELECT state,ticket FROM operations WHERE id=%s', (token,)).fetchone()
                    assert row == ('succeeded', result['ticket'])
            assert count == (2 if mode == 'engine' else 1), (mode, fault, count)
            if mode == 'full':
                # A new request/workflow for the same business operation stays deduplicated.
                second = await client.execute_workflow(TicketWorkflow.run, p, id=accept(token + '-next', p), task_queue=queue)
                assert second == result
                with db() as c:
                    assert c.execute('SELECT count(*) FROM provider_tickets WHERE id=%s', (token,)).fetchone()[0] == 1
                try:
                    prepare({**p, 'owner': 'another-user'})
                except ValueError:
                    pass
                else:
                    raise AssertionError('owner mismatch accepted')
            w.terminate(); w.join(5)
            report['results'].append(dict(mode=mode, fault=fault, actual_tickets=count, worker_crash=fault in ('before','after','committed'), replay='passed', seconds=round(time.monotonic()-started, 3)))
            print(json.dumps(report['results'][-1]), flush=True)
        report['passed'] = True
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        Path('acceptance.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    mp.freeze_support()
    async def main():
        async with await WorkflowEnvironment.start_local() as env:
            os.environ['POC_TEMPORAL'] = env.client.service_client.config.target_host
            await run(env.client)
    asyncio.run(main())
