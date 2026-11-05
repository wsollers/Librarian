"""A tool call during start-up must answer "loading" quickly instead of blocking until the models load."""
import asyncio
import time

from librarian import mcp_server


class _Ollama:
    def state(self):
        return {}

    def stop(self):
        pass


class _LoadingState:
    """Stands in for mcp_server.State while load() is still running."""
    def __init__(self):
        import threading
        self.ready = threading.Event()
        self.error = None
        self.warnings = []
        self.loaded_in = None
        self.device = "?"
        self.started = time.perf_counter()
        self.con = None
        self.ollama = _Ollama()


def _tool(app, name):
    return app._tool_manager.get_tool(name).fn


def test_tools_answer_loading_without_blocking(monkeypatch):
    monkeypatch.setattr(mcp_server, "LOAD_WAIT_S", 0.2)
    holder = {"state": _LoadingState()}
    app = mcp_server.build(holder)

    t = time.perf_counter()
    out = asyncio.run(_tool(app, "search")(query="compactness"))
    assert out["loading"] is True and "status" in out["message"]
    assert time.perf_counter() - t < 2

    st = asyncio.run(_tool(app, "status")())
    assert st["ready"] is False and st["loading_for_s"] >= 0
