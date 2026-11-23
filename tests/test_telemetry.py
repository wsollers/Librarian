import hashlib
import json
import os
import threading
import time
from types import SimpleNamespace

from starlette.testclient import TestClient

from librarian.api import create_app
from librarian.telemetry import Telemetry, RETENTION_SECONDS


def old_file(path, age):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('old')
    when = time.time() - age
    os.utime(path, (when, when))


def test_retention_deletes_only_old_logs_inside_root(tmp_path):
    root = tmp_path / 'logs'
    old_file(root / 'old.jsonl', RETENTION_SECONDS + 10)
    old_file(root / 'winsw' / 'old.log', RETENTION_SECONDS + 10)
    old_file(root / 'recent.log', RETENTION_SECONDS - 10)
    old_file(root / 'client.token', RETENTION_SECONDS + 10)
    old_file(tmp_path / 'outside.log', RETENTION_SECONDS + 10)
    assert Telemetry(root).cleanup() == 2
    assert (root / 'recent.log').exists()
    assert (root / 'client.token').exists()
    assert (tmp_path / 'outside.log').exists()


def test_cleanup_at_startup_and_after_rejected_and_successful_requests(tmp_path):
    root = tmp_path / 'logs'
    token = 'private-bearer-token'
    tokens = tmp_path / 'tokens.json'
    tokens.write_text(json.dumps({'zarathustra': hashlib.sha256(token.encode()).hexdigest()}))
    ready = threading.Event()
    ready.set()
    holder = {'state': SimpleNamespace(ready=ready, error=None)}
    async def search(**args):
        return {'hits': [{'snippet': 'private book text'}]}
    old_file(root / 'startup.log', RETENTION_SECONDS + 10)
    app = create_app(tokens, 'https://hal5000.fios-router.home', holder=holder,
                     operations={'search': search}, log_root=root)
    with TestClient(app, base_url='https://hal5000.fios-router.home', client=('127.0.0.1', 1234)) as client:
        assert not (root / 'startup.log').exists()
        old_file(root / 'denied.log', RETENTION_SECONDS + 10)
        assert client.get('/v1/status').status_code == 401
        assert not (root / 'denied.log').exists()
        old_file(root / 'request.log', RETENTION_SECONDS + 10)
        r = client.post('/v1/search', json={'query': 'private query'},
                        headers={'Authorization': 'Bearer ' + token, 'X-Forwarded-For': '192.168.1.228'})
        assert r.status_code == 200
        assert not (root / 'request.log').exists()
    text = ''.join(p.read_text() for p in root.glob('events-*.jsonl'))
    for secret in (token, 'private query', 'private book text'):
        assert secret not in text
    events = [json.loads(line) for line in text.splitlines()]
    http = [e for e in events if e['event'] == 'http_request']
    assert [e['status'] for e in http] == [401, 200]
    assert http[-1]['client'] == 'zarathustra'
    assert http[-1]['client_ip'] == '192.168.1.228'
    op = next(e for e in events if e['event'] == 'operation')
    assert op['hits'] == 1 and op['operation'] == 'search'
    assert op['request_id'] == http[-1]['request_id'] == r.headers['x-request-id']
