import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from librarian import api, mcp_server

TOKEN = "test-client-secret"
ORIGIN = "https://hal5000.home.arpa"
SHA = "a" * 64


@pytest.fixture
def setup(tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"linux": hashlib.sha256(TOKEN.encode()).hexdigest()}))
    ready = threading.Event()
    ready.set()
    holder = {"state": SimpleNamespace(ready=ready, error=None)}
    calls = []

    async def operation(**kw):
        calls.append(kw)
        return {"sha256": SHA, "pdf_page": 3, "printed_page": "1", "lines": [4, 8],
                "text": "4| theorem", "pdf": "file:///D:/private/book.pdf"}

    operations = {name: operation for name in mcp_server.READ_TOOLS}
    app = api.create_app(tokens, ORIGIN, holder=holder, operations=operations, log_root=tmp_path / 'logs')
    with TestClient(app, base_url=ORIGIN, headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield client, app.app.state.service, tokens, holder, calls, app


def test_auth_covers_rest_mcp_and_unknown_routes(setup):
    client, _, tokens, *_ = setup
    for path in ("/v1/status", "/v1/books", "/mcp", "/store", "/docs"):
        assert client.get(path, headers={"Authorization": ""}).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/status").json()["ready"] is True
    tokens.write_text("{}")
    assert client.get("/v1/status").status_code == 401
    tokens.write_text("invalid")
    assert client.get("/v1/status").status_code == 503


def test_readiness_validation_and_citations(setup):
    client, _, _, holder, calls, _ = setup
    result = client.post("/v1/search", json={"query": "compactness"})
    assert result.status_code == 200
    assert result.json()["page_url"] == f"{ORIGIN}/v1/books/{SHA}/pages/3"
    assert "file:" not in result.text
    assert result.json()["lines"] == [4, 8]
    assert "expand" not in calls[-1]
    for body in ({"query": "x", "expand": True}, {"query": "x", "k": 51},
                 {"query": "x", "mode": "bad"}, {"query": "x" * 2049}):
        assert client.post("/v1/search", json=body).status_code == 422
    assert client.post("/v1/search", content="{").status_code == 400
    assert client.post("/v1/search", json=[]).status_code == 400
    assert client.get(f"/v1/books/{SHA}/pages/0").status_code == 422
    assert client.get("/v1/books/not-a-sha/pages/1").status_code == 422
    assert client.get(f"/v1/books/{SHA}/pages/1?line_end=2").status_code == 422
    assert client.get("/v1/books?offset=3&limit=2").status_code == 200
    assert calls[-1]["offset"] == 3
    holder["state"].ready.clear()
    assert client.get("/v1/status").json()["state"] == "loading"
    assert client.post("/v1/search", json={"query": "x"}).status_code == 503
    holder["state"].error = "private path and exception"
    assert "private" not in client.get("/v1/status").text


def test_boundaries(setup):
    client, service, *_ = setup
    assert client.get("/v1/status", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/v1/status", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/v1/search", content="x" * 16385).status_code == 413

    async def bad(**kwargs):
        raise RuntimeError("secret /private/path")
    service.operations["search"] = bad
    r = client.post("/v1/search", json={"query": "x"})
    assert r.status_code == 500 and "secret" not in r.text


def rpc(client, method, params=None):
    return client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp_protocol_and_read_only_tools(setup):
    client, _, _, _, calls, _ = setup
    r = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == mcp_server.READ_TOOLS
    r = rpc(client, "tools/call", {"name": "search", "arguments": {"query": "compactness"}})
    assert r.status_code == 200, r.text
    assert "page_url" in r.text, r.text
    assert calls[-1]["query"] == "compactness"
    before = len(calls)
    r = rpc(client, "tools/call", {"name": "store", "arguments": {"apply": True}})
    assert r.json()["result"]["isError"] is True
    assert len(calls) == before


def test_timeout_retains_capacity_until_work_finishes():
    async def run():
        ready = threading.Event()
        ready.set()
        gate = asyncio.Event()
        async def slow(**kw):
            await gate.wait()
            return {"hits": []}
        service = api.Service({"state": SimpleNamespace(ready=ready, error=None)},
                              {"search": slow}, ORIGIN, timeout=0.01)
        for _ in range(4):
            assert (await service.call("search", {"query": "x"}))[1] == 504
        assert len(service.tasks) == 4
        assert (await service.call("search", {"query": "x"}))[1] == 429
        gate.set()
        await asyncio.gather(*service.tasks)
        await asyncio.sleep(0)
        assert len(service.tasks) == 0
    asyncio.run(run())


def test_shared_exports_and_token_lifecycle(tmp_path):
    operations = {}
    mcp_server.build({}, operations)
    assert set(operations) == mcp_server.READ_TOOLS
    tokens, output = tmp_path / "tokens.json", tmp_path / "client.token"
    api.main(["token-add", "--tokens", str(tokens), "--client", "linux", "--output", str(output)])
    secret = output.read_text().strip()
    assert len(secret) >= 40 and secret not in tokens.read_text()
    assert api.read_tokens(tokens)["linux"] == hashlib.sha256(secret.encode()).hexdigest()
    api.main(["token-revoke", "--tokens", str(tokens), "--client", "linux"])
    assert api.read_tokens(tokens) == {}
    with pytest.raises(ValueError):
        api.create_app(tokens, ORIGIN)


def test_real_sqlite_search_to_page(tmp_path):
    import sqlite3
    from librarian.config import Config
    from librarian.db import SCHEMA

    cfg = Config(data=tmp_path, sources=tmp_path / "sources")
    page_dir = cfg.objects / SHA / "pages"
    page_dir.mkdir(parents=True)
    text = "Definition. Compactness means every open cover\nhas a finite subcover.\n"
    (page_dir / "0003.md").write_text(text, encoding="utf-8")
    con = sqlite3.connect(cfg.db_path)
    con.executescript(SCHEMA)
    con.executescript("""
        CREATE VIRTUAL TABLE pages_fts USING fts5(text, sha256 UNINDEXED, pdf_page UNINDEXED);
        CREATE TABLE index_state(sha256 TEXT, indexed_at TEXT);
        CREATE TABLE sections(sha256 TEXT, seq INT, level INT, number TEXT, title TEXT,
                              printed_page TEXT, pdf_start INT, pdf_end INT);
    """)
    con.execute("INSERT INTO books(sha256,path,ext,size,title,author,pdf_pages,status,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (SHA, "Topology/Test.pdf", ".pdf", 1, "Topology", "Test Author", 3, "indexed", "now"))
    con.execute("INSERT INTO pages VALUES(?,?,?,?,?,?)", (SHA, 3, "1", "textlayer", 1, 3))
    con.execute("INSERT INTO index_state VALUES(?,?)", (SHA, "now"))
    con.execute("INSERT INTO pages_fts VALUES(?,?,?)", (text, SHA, 3))
    con.commit()
    con.close()
    st = mcp_server.State(cfg)
    st.con = sqlite3.connect(cfg.db_path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    st.ready.set()
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"linux": hashlib.sha256(TOKEN.encode()).hexdigest()}))
    app = api.create_app(tokens, ORIGIN, holder={"state": st}, log_root=tmp_path / 'logs')
    try:
        with TestClient(app, base_url=ORIGIN, headers={"Authorization": "Bearer " + TOKEN}) as client:
            r = client.post("/v1/search", json={"query": "compactness", "mode": "fts"})
            assert r.status_code == 200, r.text
            hit = r.json()["hits"][0]
            assert hit["sha256"] == SHA and hit["printed_page"] == "1"
            page = client.get(hit["page_url"])
            assert page.status_code == 200, page.text
            assert "finite subcover" in page.json()["text"]
            assert client.get("/v1/books").json()["books"][0]["sha256"] == SHA
            assert client.get("/v1/books?offset=1").json()["books"] == []
            assert client.get(f"/v1/books/{SHA}/contents").status_code == 200
            assert client.get("/v1/subjects").status_code == 200
            with pytest.raises(sqlite3.OperationalError):
                st.con.execute("DELETE FROM books")
    finally:
        st.close()
