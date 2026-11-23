"""Authenticated LAN REST and Streamable HTTP MCP. Backend listens on loopback only."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import mcp_server
from .telemetry import Telemetry, ROOT, request_id, route_name, client_ip

Text = Annotated[str, Field(min_length=1, max_length=512)]
BookID = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Search(Input):
    query: Annotated[str, Field(min_length=1, max_length=2048)]
    k: Annotated[int, Field(ge=1, le=50)] = 10
    author: Text | None = None
    book: Text | None = None
    chapter: Text | None = None
    section: Text | None = None
    subject: Text | None = None
    mode: Literal["hybrid", "phrase", "fts", "semantic"] = "hybrid"
    rerank: bool = True


class Books(Input):
    filter: Text | None = None
    author: Text | None = None
    subject: Text | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0, le=100000)] = 0


class Page(Input):
    book: BookID
    pdf_page: Annotated[int, Field(ge=1, le=100000)]
    line_start: Annotated[int, Field(ge=1, le=100000)] | None = None
    line_end: Annotated[int, Field(ge=1, le=100000)] | None = None
    context: Annotated[int, Field(ge=0, le=50)] = 6


class Contents(Input):
    book: BookID
    depth: Annotated[int, Field(ge=1, le=3)] = 2


MODELS = {"search": Search, "list_books": Books, "read_page": Page,
          "contents": Contents, "subjects": Input, "status": Input}


def read_tokens(path: Path) -> dict[str, str]:
    """Read on every request, so revocations take effect without a model reload."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
        for k, v in data.items()
    ):
        raise ValueError("invalid token store")
    return data


def write_private(path: Path, data: str) -> None:
    # On Windows the enclosing directory must already have restricted NTFS ACLs.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


def save_tokens(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    write_private(temp, json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


class Boundary:
    """ASGI boundary protecting every route, including MCP and unknown routes."""
    def __init__(self, app, tokens: Path | None, origin: str, telemetry: Telemetry, trust_lan=False):
        self.app, self.tokens, self.origin = app, tokens, origin
        self.hosts = {urlsplit(origin).netloc, "127.0.0.1:8765", "localhost:8765"}
        self.active = 0
        self.telemetry = telemetry
        self.trust_lan = trust_lan

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        identifier = secrets.token_hex(12)
        context = request_id.set(identifier)
        started = time.perf_counter()
        status = 499
        size = 0
        async def observed_send(event):
            nonlocal status, size
            if event['type'] == 'http.response.start':
                status = event['status']
                event['headers'] = list(event.get('headers', [])) + [(b'x-request-id', identifier.encode())]
            elif event['type'] == 'http.response.body':
                size += len(event.get('body', b''))
            await send(event)
        try:
            return await self.handle(scope, receive, observed_send)
        except Exception:
            status = 500
            raise
        finally:
            fields = dict(request_id=identifier, method=scope['method'], route=route_name(scope['path']),
                          client_ip=client_ip(scope), client=scope.get('librarian_client'), status=status,
                          duration_ms=round((time.perf_counter()-started)*1000, 2), response_bytes=size)
            try:
                await anyio.to_thread.run_sync(lambda: self.telemetry.finish_request(**fields))
            finally:
                request_id.reset(context)

    async def handle(self, scope, receive, send):

        async def reject(code, message):
            response_headers = {"Cache-Control": "no-store"}
            if code == 401:
                response_headers["WWW-Authenticate"] = "Bearer"
            await JSONResponse({"error": message}, code, headers=response_headers)(scope, receive, send)

        headers = {k.lower(): v for k, v in scope["headers"]}
        if headers.get(b"host", b"").decode("latin1") not in self.hosts:
            return await reject(400, "Invalid host")
        if b"origin" in headers and headers[b"origin"].decode("latin1") != self.origin:
            return await reject(403, "Invalid origin")
        if self.trust_lan:
            try:
                peer = ipaddress.ip_address((scope.get('client') or ('', 0))[0])
                if not peer.is_loopback:
                    return await reject(403, 'Loopback proxy required')
                forwarded = headers.get(b'x-forwarded-for')
                if forwarded is not None and ipaddress.ip_address(forwarded.decode('ascii')) not in ipaddress.ip_network('192.168.1.0/24'):
                    return await reject(403, 'LAN access only')
            except (ValueError, UnicodeError):
                return await reject(403, 'Invalid client address')
            client = 'trusted-lan'
        else:
            token = headers.get(b"authorization", b"")
            if not token.startswith(b"Bearer ") or len(token) > 512:
                return await reject(401, "Authentication required")
            try:
                digest = hashlib.sha256(token[7:]).hexdigest()
                client = next((name for name, value in read_tokens(self.tokens).items() if hmac.compare_digest(digest, value)), None)
            except (OSError, ValueError):
                return await reject(503, "Authentication unavailable")
            if client is None:
                return await reject(401, "Authentication required")
        scope['librarian_client'] = client
        if self.active >= 16:
            return await reject(429, "Too many requests")
        self.active += 1
        try:
            body = bytearray()
            try:
                with anyio.fail_after(10):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        body.extend(event.get("body", b""))
                        if len(body) > 16384:
                            return await reject(413, "Request too large")
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "Request body timed out")
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            async def secure_send(event):
                if event["type"] == "http.response.start":
                    event["headers"] = list(event.get("headers", [])) + [
                        (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
                await send(event)

            await self.app(scope, replay, secure_send)
        finally:
            self.active -= 1


class Service:
    def __init__(self, holder: dict, operations: dict, origin: str, timeout: float = 60, telemetry=None):
        self.holder, self.operations, self.origin = holder, operations, origin
        self.timeout = timeout
        self.tasks: set[asyncio.Task] = set()
        self.telemetry = telemetry

    def public(self, value):
        if isinstance(value, list):
            return [self.public(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {k: self.public(v) for k, v in value.items() if k not in {"pdf", "error", "warnings"}}
        if "error" in value:
            out["error"] = "Requested content is unavailable or ambiguous"
        if "pdf" in value and re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", ""))):
            out["page_url"] = f'{self.origin}/v1/books/{value["sha256"]}/pages/{value["pdf_page"]}'
        return out

    async def call(self, name: str, args: dict) -> tuple[dict, int]:
        started = time.perf_counter()
        result, status = await self._call(name, args)
        if self.telemetry:
            self.telemetry.write('operation', request_id=request_id.get(), operation=name, status=status,
                                 duration_ms=round((time.perf_counter()-started)*1000, 2),
                                 hits=len(result['hits']) if 'hits' in result else None)
        return result, status

    async def _call(self, name: str, args: dict) -> tuple[dict, int]:
        try:
            values = MODELS[name].model_validate(args).model_dump()
            if name == "read_page" and values["line_end"] is not None and (
                values["line_start"] is None or values["line_end"] < values["line_start"]
            ):
                raise ValueError("invalid line range")
        except (ValidationError, ValueError):
            return {"error": "Invalid arguments"}, 422
        st = self.holder["state"]
        ready = st.ready.is_set() and not st.error
        if name == "status":
            return {"ready": ready, "state": "failed" if st.error else "ready" if ready else "loading"}, 200
        if not ready:
            return {"error": "Search unavailable" if st.error else "Models loading", "ready": False}, 503
        if len(self.tasks) >= 4:
            return {"error": "Search queue full"}, 429

        async def execute():
            try:
                result = self.public(await self.operations[name](**values))
                if len(json.dumps(result).encode("utf-8")) > 1024 * 1024:
                    return {"error": "Response too large; narrow the request"}, 413
                return result, 404 if "error" in result else 200
            except Exception:
                # Do not log exception strings: SQL, paths or source text may be embedded.
                return {"error": "Search operation failed"}, 500

        task = asyncio.create_task(execute())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        try:
            # A running GPU/SQLite thread cannot be safely killed. Keep its queue slot until
            # it finishes even after the HTTP timeout, preventing unbounded abandoned work.
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except TimeoutError:
            return {"error": "Search timed out"}, 504


def create_app(tokens: Path | None, origin: str, *, holder=None, operations=None, log_root: Path = ROOT, trust_lan=False):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations

    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("origin must be an HTTPS origin without a path")
    if not trust_lan and (tokens is None or not read_tokens(tokens)):
        raise ValueError("provision at least one client token before starting")
    supplied_state = holder is not None
    holder = holder if supplied_state else {}
    if operations is None:
        operations = {}
        mcp_server.build(holder, operations)
    telemetry = Telemetry(log_root)
    service = Service(holder, operations, origin, telemetry=telemetry)
    mcp = FastMCP("librarian-readonly", stateless_http=True, json_response=True,
                  instructions="Search, then read_page before citing. Use full sha256 book IDs and page_url citations.",
                  transport_security=TransportSecuritySettings(
                      allowed_hosts=[parsed.netloc, "127.0.0.1:8765", "localhost:8765"],
                      allowed_origins=[origin]))

    # Typed wrappers give Codex useful schemas; Service validates the same models for REST.
    async def search(query: str, k: int = 10, author: str | None = None, book: str | None = None,
                     chapter: str | None = None, section: str | None = None, subject: str | None = None,
                     mode: str = "hybrid", rerank: bool = True) -> dict:
        """Search passages; read the returned page before citing it."""
        return (await service.call("search", {k: v for k, v in locals().items() if k in MODELS["search"].model_fields}))[0]

    async def read_page(book: str, pdf_page: int, line_start: int | None = None,
                        line_end: int | None = None, context: int = 6) -> dict:
        """Read line-numbered page text using a full SHA-256 book ID."""
        return (await service.call("read_page", {k: v for k, v in locals().items() if k in MODELS["read_page"].model_fields}))[0]

    async def list_books(filter: str | None = None, author: str | None = None,
                         subject: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        """Browse indexed books with pagination."""
        return (await service.call("list_books", {k: v for k, v in locals().items() if k in MODELS["list_books"].model_fields}))[0]

    async def contents(book: str, depth: int = 2) -> dict:
        """Read a book's table of contents."""
        return (await service.call("contents", {k: v for k, v in locals().items() if k in MODELS["contents"].model_fields}))[0]

    async def subjects() -> dict:
        """List available subjects."""
        return (await service.call("subjects", {}))[0]

    async def status() -> dict:
        """Check whether the search models are ready."""
        return (await service.call("status", {}))[0]

    for fn in (search, read_page, list_books, contents, subjects, status):
        mcp.add_tool(fn, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                    idempotentHint=True, openWorldHint=False))

    async def rest(request: Request, name: str):
        try:
            args = await request.json() if request.method == "POST" else dict(request.query_params)
            if not isinstance(args, dict):
                raise ValueError()
            args.update(request.path_params)
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "Invalid JSON"}, 400)
        result, code = await service.call(name, args)
        return JSONResponse(result, code)

    def route(path, name, methods=None):
        async def endpoint(request):
            return await rest(request, name)
        return Route(path, endpoint, methods=methods or ["GET"])

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        removed = telemetry.cleanup()
        telemetry.write('startup', removed_files=removed, retention_days=90)
        if not supplied_state:
            from . import config
            cfg = config.load(None)
            telemetry.setup_logging()
            holder["state"] = mcp_server.State(cfg)
            def load():
                holder["state"].load()
                holder["state"].load_vectors()
            worker = threading.Thread(target=load, name="api-load", daemon=True)
            worker.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if service.tasks:
                await asyncio.gather(*service.tasks)
            if worker:
                await asyncio.to_thread(worker.join)
                holder["state"].close()

    app = Starlette(routes=[
        route("/v1/search", "search", ["POST"]), route("/v1/books", "list_books"),
        route("/v1/books/{book}/pages/{pdf_page}", "read_page"),
        route("/v1/books/{book}/contents", "contents"), route("/v1/subjects", "subjects"),
        route("/v1/status", "status"), Mount("/", app=mcp.streamable_http_app())], lifespan=lifespan)
    app.state.service = service
    app.state.mcp = mcp
    return Boundary(app, tokens, origin, telemetry, trust_lan=trust_lan)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    auth = serve.add_mutually_exclusive_group(required=True)
    auth.add_argument("--tokens", type=Path)
    auth.add_argument("--trust-lan", action="store_true", help="Trust 192.168.1.0/24 via the loopback HTTPS proxy without tokens")
    serve.add_argument("--origin", required=True, help="HTTPS LAN origin, e.g. https://hal5000.home.arpa")
    for cmd in ("token-add", "token-revoke"):
        p = sub.add_parser(cmd)
        p.add_argument("--tokens", type=Path, required=True)
        p.add_argument("--client", required=True)
        if cmd == "token-add":
            p.add_argument("--output", type=Path, required=True, help="new private file for the client token")
    args = parser.parse_args(argv)
    if sys.prefix == sys.base_prefix:
        parser.error("use the repository .venv")
    if args.command != "serve":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.client):
            parser.error("client must be 1-64 letters, digits, dots, underscores or hyphens")
        data = read_tokens(args.tokens) if args.tokens.exists() else {}
        if args.command == "token-add":
            if args.output.resolve() == args.tokens.resolve():
                parser.error("client output and server token store must be different files")
            if args.client in data:
                parser.error("client already exists; revoke first or use a new client name for rotation")
            token = secrets.token_urlsafe(32)
            write_private(args.output, token + "\n")
            data[args.client] = hashlib.sha256(token.encode()).hexdigest()
        else:
            if args.client not in data:
                parser.error("unknown client")
            del data[args.client]
        save_tokens(args.tokens, data)
        print("Client credential updated; token values are never printed.")
        return 0
    import uvicorn
    tokens = args.tokens.resolve() if args.tokens else None
    os.chdir(mcp_server.REPO)
    mcp_server.load_env(mcp_server.REPO / ".env")
    app = create_app(tokens, args.origin, trust_lan=args.trust_lan)
    uvicorn.run(app, host="127.0.0.1", port=8765, workers=1, access_log=False,
                proxy_headers=False, limit_concurrency=32, timeout_keep_alive=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
