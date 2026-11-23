"""Local service telemetry and file retention. Never records credentials or request bodies."""
from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / '.runtime' / 'logs'
RETENTION_SECONDS = 90 * 24 * 60 * 60
request_id = contextvars.ContextVar('librarian_request_id', default=None)


class Telemetry:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.lock = threading.Lock()

    def write(self, event: str, **fields):
        now = datetime.now(timezone.utc)
        record = {'timestamp': now.isoformat(), 'event': event, **fields}
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f'events-{now:%Y-%m-%d}.jsonl'
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=True) + '\n')

    def cleanup(self, now: float | None = None):
        """Remove only aged log files inside this root; never follow links or junctions."""
        cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
        removed = 0
        if not self.root.exists():
            return removed
        def linked(path):
            info = path.lstat()
            return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
        with self.lock:
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                dirs[:] = [name for name in dirs if not linked(Path(directory) / name)]
                for name in files:
                    path = Path(directory) / name
                    if not (name.endswith(('.log', '.jsonl')) or '.log.' in name):
                        continue
                    try:
                        if linked(path) or not path.resolve().is_relative_to(self.root):
                            continue
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    except (FileNotFoundError, PermissionError):
                        # Another request may have removed it, or Windows may still have it open.
                        continue
        return removed

    def finish_request(self, **fields):
        self.write('http_request', **fields)
        removed = self.cleanup()
        if removed:
            self.write('retention_cleanup', removed_files=removed)

    def setup_logging(self):
        logger = logging.getLogger('librarian')
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(RuntimeHandler(self))


class RuntimeHandler(logging.Handler):
    def __init__(self, telemetry):
        super().__init__()
        self.telemetry = telemetry

    def emit(self, record):
        try:
            # No traceback formatting: exception strings can contain SQL or source text.
            self.telemetry.write('runtime', level=record.levelname, message=record.getMessage())
        except OSError:
            self.handleError(record)


def route_name(path):
    if path in ('/mcp', '/v1/status', '/v1/search', '/v1/books', '/v1/subjects'):
        return path
    if path.startswith('/v1/books/'):
        if '/pages/' in path:
            return '/v1/books/{book}/pages/{page}'
        if path.endswith('/contents'):
            return '/v1/books/{book}/contents'
    return 'other'


def client_ip(scope):
    peer = (scope.get('client') or ('unknown', 0))[0]
    # Caddy overwrites X-Forwarded-For. Trust it only on the loopback-only backend.
    if peer in ('127.0.0.1', '::1'):
        headers = dict(scope.get('headers', []))
        value = headers.get(b'x-forwarded-for', b'').decode('latin1').split(',')[-1].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    return peer
