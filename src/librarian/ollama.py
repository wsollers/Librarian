"""Ollama on demand, for the long-running MCP server.

Nothing happens until the first search that asks for expansion. Then:
* Ollama already running (the Windows tray app, or `ollama serve` elsewhere): use it, never stop it.
* Not running: start `ollama serve` ourselves and own it. On Windows it is put in a Job Object
  that kills it (and its model runners) when this process exits, even if we are killed hard.
* The expansion model is loaded with keep_alive=-1, so only the first call pays the load.
At shutdown: unload the model (keep_alive=0), and stop Ollama only if we started it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .embed import ollama_url
from .log import log


def _get(path: str, timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(ollama_url() + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def _post(path: str, body: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(ollama_url() + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def find_exe() -> Optional[str]:
    exe = os.environ.get("LIBRARIAN_OLLAMA_EXE") or shutil.which("ollama")
    if exe:
        return exe
    if sys.platform == "win32":
        for base, sub in ((os.environ.get("LOCALAPPDATA"), "Programs/Ollama"), (os.environ.get("ProgramFiles"), "Ollama")):
            if base and (Path(base) / sub / "ollama.exe").is_file():
                return str(Path(base) / sub / "ollama.exe")
    return None


def _kill_on_close_job(proc: subprocess.Popen):
    """Windows: a Job Object that kills its processes when its last handle closes (when we exit)."""
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wt.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(wt.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    if not k32.AssignProcessToJobObject(wt.HANDLE(job), wt.HANDLE(int(proc._handle))):  # type: ignore[attr-defined]
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
    return job  # keep the handle for our lifetime; the OS closes it when we exit


class OllamaManager:
    def __init__(self, model: Optional[str] = None, log_dir: Optional[Path] = None):
        self.model = model or os.environ.get("LIBRARIAN_EXPAND_MODEL", "llama3.1:8b")
        self.log_dir = log_dir
        self.proc: Optional[subprocess.Popen] = None
        self.job = None
        self.owned = False
        self.loaded = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()

    def state(self) -> dict:
        v = _get("/api/version", 0.5)
        return {"running": v is not None, "version": (v or {}).get("version"), "started_by_server": self.owned,
                "model": self.model, "model_loaded": self.loaded, "error": self.error}

    def ensure(self) -> bool:
        """Ollama up with the expansion model loaded. Safe to call on every expanding search."""
        with self._lock:
            if self.loaded and _get("/api/version", 0.5) is not None:
                return True
            self.error = None
            if _get("/api/version") is None and not self._start():
                return False
            try:
                t = time.perf_counter()
                _post("/api/generate", {"model": self.model, "prompt": "", "keep_alive": -1})  # load, keep resident
                log.info("ollama: %s loaded in %.1fs", self.model, time.perf_counter() - t)
                self.loaded = True
                os.environ["LIBRARIAN_EXPAND_KEEP_ALIVE"] = "-1"
                return True
            except Exception as e:  # noqa: BLE001
                self.error = f"could not load {self.model}: {e} (is it pulled? `ollama pull {self.model}`)"
                log.warning("ollama: %s", self.error)
                return False

    def _start(self) -> bool:
        exe = find_exe()
        if not exe:
            self.error = "ollama is not installed (or not on PATH; set LIBRARIAN_OLLAMA_EXE)"
            return False
        out = subprocess.DEVNULL
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            out = open(self.log_dir / "ollama-serve.log", "ab")  # noqa: SIM115
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        log.info("ollama: not running, starting %s serve", exe)
        self.proc = subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                     creationflags=flags, start_new_session=sys.platform != "win32")
        self.owned = True
        if sys.platform == "win32":
            try:
                self.job = _kill_on_close_job(self.proc)
            except OSError as e:
                log.warning("ollama: no Job Object (%s); it is stopped at shutdown only", e)
        for _ in range(60):
            if _get("/api/version", 0.5) is not None:
                return True
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        self.error = "ollama serve did not come up (see logs/ollama-serve.log)"
        self.stop()
        return False

    def stop(self) -> None:
        """Unload the model; stop Ollama only if this process started it."""
        with self._lock:
            if self.loaded:
                try:
                    _post("/api/generate", {"model": self.model, "prompt": "", "keep_alive": 0}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                self.loaded = False
            if self.owned and self.proc is not None:
                if self.job is not None:   # the whole job: ollama serve and its model runners
                    import ctypes
                    k32 = ctypes.WinDLL("kernel32")
                    k32.TerminateJobObject(ctypes.c_void_p(self.job), 0)
                    k32.CloseHandle(ctypes.c_void_p(self.job))
                    self.job = None
                else:
                    self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                log.info("ollama: stopped the instance this server started")
            self.proc, self.owned = None, False
