"""Codex OCR, one book at a time, for a whole queue: extract --llm-ocr, then pagemap and index for
that book, then a pause before the next one. Built so that "is it stuck?" has an answer on disk.

  .venv\\Scripts\\python.exe scripts\\ocr_batch.py                      # config/ocr-queue-2.txt
  .venv\\Scripts\\python.exe scripts\\ocr_batch.py --queue config/ocr-queue.txt --all-pages
  .venv\\Scripts\\python.exe scripts\\ocr_batch.py --limit 2            # trial

While it runs:
  <data>\\reports\\ocr-batch-status.md   rewritten every 30 s: current book, OCR progress, the last
                                          sign of life, books done, pages per minute, time left
  <data>\\reports\\ocr-batch-state.json  per-book outcome; a re-run skips books already done
  <data>\\logs\\<time>-ocr-batch.log      everything, including each book's own output
To stop cleanly after the current book, create <data>\\reports\\ocr-batch.STOP (any content).
Ctrl+C also works: pages Codex has already returned are cached (objects\\<sha>\\ocr-cache) and
are not paid for again.

Safeguards:
  * stall: no output and no new OCR page for --stall-min minutes -> the book is killed (with its
    Codex children), marked 'stalled', and retried once after the rest of the queue;
  * Codex failing (most pages of a book fail, e.g. the plan's usage limit): wait --cooldown
    minutes and retry the same book; after --max-cooldowns in a row, stop with the reason in the
    status file instead of burning through the queue;
  * books not in the index (stale queue lines) are reported and skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
os.chdir(REPO)
from librarian import config  # noqa: E402
from librarian.cli import select_books  # noqa: E402
from librarian.db import connect  # noqa: E402

RE_START = re.compile(r"LLM OCR \(codex\): (\d+) pages")
RE_PROG = re.compile(r"OCR (\d+)/(\d+) pages, (\d+) failed")
RE_SKIP = re.compile(r"skip \(cached\)")
RE_QUOTA = re.compile(r"hit your usage limit.*?try again at ([A-Z][a-z]{2} \d{1,2})(?:st|nd|rd|th)?, (\d{4}) (\d{1,2}:\d{2} [AP]M)")


def quota_reset(line: str):
    """'... try again at Nov 11th, 2026 3:08 AM.' -> datetime, or None."""
    m = RE_QUOTA.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y %I:%M %p")
    except ValueError:
        return None


def child_env() -> dict:
    """What scripts\\run.ps1 sets up: .env (keys), UTF-8, offline Hugging Face, quiet loaders."""
    env = dict(os.environ)
    envfile = REPO / ".env"
    if envfile.is_file():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and m.group(2):
                env[m.group(1)] = m.group(2).strip("\"'")
    env["PYTHONIOENCODING"] = "utf-8"
    for k, v in (("HF_HUB_OFFLINE", "1"), ("HF_HUB_DISABLE_PROGRESS_BARS", "1"), ("HF_HUB_VERBOSITY", "error"),
                 ("TRANSFORMERS_VERBOSITY", "error"), ("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1"), ("TQDM_DISABLE", "1")):
        env.setdefault(k, v)
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec % 60:02d}s"


class Batch:
    def __init__(self, a):
        self.a = a
        self.cfg = config.load(None)
        self.reports = self.cfg.data / "reports"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.status_path = self.reports / "ocr-batch-status.md"
        self.state_path = self.reports / "ocr-batch-state.json"
        self.stop_path = self.reports / "ocr-batch.STOP"
        logs = self.cfg.data / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.log_path = logs / f"{datetime.now():%Y%m%d-%H%M%S}-ocr-batch.log"
        self.logf = open(self.log_path, "a", encoding="utf-8")
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.is_file() else {}
        self.state.setdefault("books", {})
        self.t0 = time.time()
        self.pages_done = 0          # Codex pages returned in this batch (cache hits included)
        self.phase = "starting"
        self.current: dict = {}
        self.last_life = time.time()
        self.last_progress = ""
        self.note = ""
        self.queue: list[dict] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.quota_until = None

    # ---- output -------------------------------------------------------------------------
    def log(self, msg: str, echo: bool = True) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        with self.lock:
            self.logf.write(line + "\n")
            self.logf.flush()
        if echo:
            print(line, flush=True)

    def save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def write_status(self) -> None:
        books = self.state["books"]
        ours = [q for q in self.queue]
        done = [q for q in ours if books.get(q["sha"], {}).get("status") == "done"]
        bad = [q for q in ours if books.get(q["sha"], {}).get("status") in ("stalled", "failed", "codex-failing")]
        left_pages = sum(q["est"] for q in ours if books.get(q["sha"], {}).get("status") != "done")
        el = time.time() - self.t0
        rate = self.pages_done / (el / 60) if el > 60 and self.pages_done else 0.0
        eta = f"~{fmt_dur(left_pages / rate * 60)} (queue estimate {left_pages} pages at {rate:.1f}/min)" if rate else "after the first book"
        cur = self.current
        lines = [
            "# OCR batch status",
            "",
            f"Updated: {datetime.now():%Y-%m-%d %H:%M:%S}  ",
            f"State: **{self.phase}**{('  ' + self.note) if self.note else ''}  ",
            f"Running for: {fmt_dur(el)}  (since {datetime.fromtimestamp(self.t0):%a %H:%M})  ",
            f"Last sign of life: {fmt_dur(time.time() - self.last_life)} ago "
            f"(stall limit {self.a.stall_min} min)  ",
            "",
        ]
        if cur:
            lines += [f"Current: [{cur['i']}/{len(self.queue)}] {cur['path']}  ",
                      f"Book started: {datetime.fromtimestamp(cur['t0']):%H:%M:%S} ({fmt_dur(time.time() - cur['t0'])} ago)  ",
                      f"Progress: {self.last_progress or '-'}  ", ""]
        lines += [f"Books: {len(done)} done, {len(bad)} with problems, {len(ours) - len(done)} to go (of {len(ours)})  ",
                  f"Codex pages this batch: {self.pages_done}  ",
                  f"Time left: {eta}  ", "",
                  f"Stop after the current book: create {self.stop_path}  ",
                  f"Log: {self.log_path}"]
        if bad:
            lines += ["", "## Problems", ""] + [f"- {books[q['sha']]['status']}: {q['path']}" for q in bad[-15:]]
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.status_path)

    def ticker(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.write_status()
            except Exception as e:  # noqa: BLE001
                self.log(f"status write failed: {e}", echo=False)

    # ---- one librarian command, watched ----------------------------------------------------
    def librarian(self, args: list[str], sha: str, watch_cache: bool, hard_limit_min: int) -> dict:
        cmd = [sys.executable, "-m", "librarian", *args]
        self.log("$ " + " ".join(cmd[2:]))
        proc = subprocess.Popen(cmd, cwd=REPO, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.proc = proc
        res = {"ocr_pages": 0, "done": 0, "failed": 0, "skipped": False, "db_locked": False, "missing": False,
               "quota_until": None}

        def reader():
            for line in proc.stdout:
                line = line.rstrip()
                self.last_life = time.time()
                self.log("  | " + line, echo=bool(RE_START.search(line) or RE_PROG.search(line) or "ERROR" in line))
                if m := RE_START.search(line):
                    res["ocr_pages"] = int(m.group(1))
                    self.last_progress = f"OCR 0/{m.group(1)} pages"
                elif m := RE_PROG.search(line):
                    new = int(m.group(1))
                    self.pages_done += new - res["done"]
                    res["done"], res["failed"] = new, int(m.group(3))
                    self.last_progress = line.split("INFO", 1)[-1].strip()
                elif RE_SKIP.search(line):
                    res["skipped"] = True
                elif "database is locked" in line:
                    res["db_locked"] = True
                elif "file missing:" in line:
                    res["missing"] = True
                elif "hit your usage limit" in line:
                    res["quota_until"] = quota_reset(line) or res["quota_until"] or "unknown"
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        cache = self.cfg.objects / sha / "ocr-cache"
        n_cache = lambda: sum(1 for _ in cache.glob("*.md")) if cache.is_dir() else 0  # noqa: E731
        seen, t0 = n_cache(), time.time()
        self.last_life = time.time()
        while proc.poll() is None:
            time.sleep(10)
            if watch_cache and (n := n_cache()) != seen:
                seen, self.last_life = n, time.time()
            quiet = time.time() - self.last_life
            if quiet > self.a.stall_min * 60 or time.time() - t0 > hard_limit_min * 60:
                why = f"no output or new page for {fmt_dur(quiet)}" if quiet > self.a.stall_min * 60 else f"over {hard_limit_min} min"
                self.log(f"STALLED ({why}): killing {args[0]} for {sha[:16]}")
                kill_tree(proc)
                res["stalled"] = why
                break
        th.join(timeout=10)
        res["exit"] = proc.returncode
        return res

    # ---- the queue ---------------------------------------------------------------------------
    def read_queue(self, queue: str, all_pages: bool) -> list[dict]:
        qfile = REPO / queue if not Path(queue).is_absolute() else Path(queue)
        est = {}
        pats = []
        for line in qfile.read_text(encoding="utf-8").splitlines():
            p = line.split("#", 1)[0].strip()
            if not p:
                continue
            pats.append(p)
            m = re.search(r"#\s*(\d+)\s+pages", line)
            est[p.lower()] = int(m.group(1)) if m else 0
        # Read the queue and close: an open connection here would keep the index locked against the
        # librarian commands this script starts (it did: every book failed with "database is locked").
        con = connect(self.cfg.db_path)
        try:
            rows = select_books(con, "@" + str(qfile))
        finally:
            con.close()
        found, out = set(), []
        for r in rows:
            key = next(p for p in est if r["sha256"].startswith(p) or p in r["path"].lower())
            found.add(key)
            out.append({"sha": r["sha256"], "path": r["path"], "est": est[key], "all_pages": all_pages})
        for p in pats:
            if p.lower() not in found:
                self.log(f"not in the index, skipped: {p}")
        return out

    def load_queue(self) -> None:
        self.queue = self.read_queue(self.a.queue, self.a.all_pages)
        if self.a.limit:
            todo = [q for q in self.queue if self.state["books"].get(q["sha"], {}).get("status") != "done"]
            keep = {q["sha"] for q in todo[: self.a.limit]}
            self.queue = [q for q in self.queue if q["sha"] in keep]
        for i, q in enumerate(self.queue, 1):
            q["i"] = i

    def do_book(self, q: dict) -> str:
        sha = q["sha"]
        self.current = dict(q, t0=time.time())
        self.last_progress = ""
        self.phase = "OCR"
        self.write_status()
        self.log(f"[{q['i']}/{len(self.queue)}] {q['path']}")
        ex = ["extract", "--select", sha[:16], "--llm-ocr"] + ([] if q.get("all_pages", self.a.all_pages) else ["--body-only"])
        r = self.librarian(ex, sha, watch_cache=True, hard_limit_min=self.a.book_hours * 60)
        rec = {"path": q["path"], "finished": datetime.now().isoformat(timespec="seconds"),
               "seconds": round(time.time() - self.current["t0"]), "ocr_pages": r["ocr_pages"],
               "ocr_done": r["done"], "ocr_failed": r["failed"]}
        if r["quota_until"] and r["failed"] * 2 >= max(r["done"], 1):
            rec["status"], rec["why"] = "quota", f"Codex usage limit until {r['quota_until']}"
            self.quota_until = r["quota_until"]
        elif r["missing"]:
            # the book's file is not where the index says (renamed or moved): not Codex's fault
            rec["status"], rec["why"] = "failed", "file missing: run librarian inventory"
        elif r["db_locked"] and r["done"] == 0:
            rec["status"] = "db-locked"
        elif r.get("stalled"):
            rec["status"], rec["why"] = "stalled", r["stalled"]
        elif r["exit"] != 0:
            rec["status"] = "failed"
        elif r["ocr_pages"] >= 4 and r["failed"] * 2 >= max(r["done"], 1):
            rec["status"] = "codex-failing"
        else:
            if not r["skipped"]:
                self.phase = "page map + index"
                self.write_status()
                for stage in ("pagemap", "index"):
                    s = self.librarian([stage, "--select", sha[:16]], sha, watch_cache=False, hard_limit_min=60)
                    if s.get("stalled") or s["exit"] != 0:
                        rec["status"] = "failed"
                        rec["why"] = f"{stage}: " + (s.get("stalled") or f"exit {s['exit']}")
                        break
            rec.setdefault("status", "done")
            rec["used_codex"] = r["done"] > 0
        self.state["books"][sha] = rec
        self.save_state()
        self.log(f"  -> {rec['status']}: {r['done']}/{r['ocr_pages']} OCR pages, {r['failed']} failed, {fmt_dur(rec['seconds'])}")
        return rec["status"]

    def take_inserted(self, pending) -> None:
        """`librarian store` hands newly filed books over in reports/ocr-batch.insert: do them next."""
        f = self.reports / "ocr-batch.insert"
        if not f.is_file():
            return
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
            f.unlink()
            items = self.read_queue(spec["queue"], bool(spec.get("all_pages")))
        except (OSError, ValueError, KeyError) as e:
            self.log(f"could not read {f}: {e}")
            return
        books = self.state["books"]
        queued = {q["sha"] for q in pending}
        items = [q for q in items if books.get(q["sha"], {}).get("status") != "done" and q["sha"] not in queued]
        if not items:
            return
        known = {q["sha"] for q in self.queue}
        self.queue = [q for q in items if q["sha"] not in known] + self.queue
        for i, q in enumerate(self.queue, 1):
            q["i"] = i
        pending.extendleft(reversed(items))
        self.log(f"{len(items)} newly filed book(s) from {spec['queue']}: doing them next")

    def stop_requested(self) -> bool:
        if self.stop_path.exists():
            self.stop_path.unlink(missing_ok=True)
            self.phase, self.note = "stopped", "(STOP file)"
            return True
        return False

    def wait(self, minutes: float, phase: str) -> bool:
        """Sleep, but keep the status fresh and honour the STOP file. False if stop requested."""
        self.phase, end = phase, time.time() + minutes * 60
        self.note = f"until {datetime.now() + timedelta(minutes=minutes):%H:%M:%S}"
        while time.time() < end:
            self.last_life = time.time()
            if self.stop_requested():
                return False
            time.sleep(min(5, max(end - time.time(), 0)))
        self.note = ""
        return True

    def run(self) -> int:
        threading.Thread(target=self.ticker, daemon=True).start()
        self.load_queue()
        books = self.state["books"]
        todo = [q for q in self.queue if self.a.recheck_done or books.get(q["sha"], {}).get("status") != "done"]
        self.log(f"queue {self.a.queue}: {len(self.queue)} books, {len(todo)} to do; status: {self.status_path}")
        # pick up books renamed or moved since the last inventory (cheap: hashes are reused)
        self.phase = "inventory"
        self.write_status()
        inv = self.librarian(["inventory"], "-", watch_cache=False, hard_limit_min=60)
        if inv.get("stalled") or inv["exit"] != 0:
            self.log("inventory failed (see log); carrying on with the paths already in the index")
        else:
            self.load_queue()          # paths may have changed
            todo = [q for q in self.queue if self.a.recheck_done or books.get(q["sha"], {}).get("status") != "done"]
        if not self.a.no_audit and not self.state.get("audited"):
            self.phase = "audit --mark"
            self.write_status()
            a = self.librarian(["audit", "--mark"], "-", watch_cache=False, hard_limit_min=120)
            if a.get("stalled") or a["exit"] != 0:
                # not fatal: pages marked by an earlier audit still count, and extract --llm-ocr
                # also picks up every low-quality and legacy page on its own
                self.log("audit --mark failed (see log); carrying on with the pages already marked")
            else:
                self.state["audited"] = datetime.now().isoformat(timespec="seconds")
                self.save_state()
        from collections import deque
        pending, retry, retried = deque(todo), [], False
        while True:
            self.take_inserted(pending)
            if not pending:
                if retry and not retried:
                    self.log(f"retrying {len(retry)} stalled book(s)")
                    pending.extend(retry)
                    retried = True
                    continue
                break
            q = pending.popleft()
            pass_no = 2 if retried else 1
            if True:
                if self.stop_requested():
                    break
                cooldowns = 0
                while True:
                    st = self.do_book(q)
                    if st == "db-locked":
                        # another program holds the index open (a stuck librarian MCP server, a search):
                        # nothing was spent, so wait and retry the same book rather than fail the queue
                        self.log("index is locked by another program: retrying this book in 5 min "
                                 "(close whatever has F:\\Readings\\librarian\\librarian.sqlite open)")
                        if not self.wait(5, "waiting: index locked by another program"):
                            break
                        continue
                    if st == "quota":
                        until = self.quota_until
                        if self.a.wait_for_quota and isinstance(until, datetime):
                            mins = max(1.0, (until - datetime.now()).total_seconds() / 60 + 5)
                            self.log(f"Codex usage limit reached: waiting until {until:%a %H:%M} ({mins / 60:.1f} h)")
                            if not self.wait(mins, f"waiting for the Codex usage limit to reset ({until:%a %d %b %H:%M})"):
                                break
                            continue
                        self.phase = "stopped"
                        self.note = (f"Codex usage limit reached; it resets {until:%a %d %b %H:%M}" if isinstance(until, datetime)
                                     else "Codex usage limit reached") + ". Run the script again then (or use --wait-for-quota)."
                        self.current = {}
                        self.write_status()
                        self.log("STOP: " + self.note)
                        return 3
                    if st != "codex-failing":
                        break
                    cooldowns += 1
                    if cooldowns > self.a.max_cooldowns:
                        self.phase = "stopped"
                        self.note = (f"Codex kept failing ({self.a.max_cooldowns} cooldowns): check "
                                     f"`codex` and the plan's usage, then run the script again")
                        self.current = {}
                        self.write_status()
                        self.log("STOP: " + self.note)
                        return 2
                    self.log(f"Codex failing on most pages: cooldown {self.a.cooldown} min ({cooldowns}/{self.a.max_cooldowns})")
                    if not self.wait(self.a.cooldown, "cooldown (Codex failing)"):
                        break
                if st == "stalled" and pass_no == 1:
                    retry.append(q)
                if self.phase == "stopped":
                    break
                if books.get(q["sha"], {}).get("used_codex") and not self.wait(self.a.pause / 60, "pause between books"):
                    break
        self.current = {}
        if self.phase != "stopped":
            self.phase = "finished"
        self.write_status()
        self.log(f"{self.phase}: {self.pages_done} Codex pages in {fmt_dur(time.time() - self.t0)}; see {self.status_path}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", default="config/ocr-queue-2.txt", help="queue file (sha prefixes or path patterns)")
    ap.add_argument("--pause", type=int, default=60, help="seconds to wait after a book that used Codex (default 60)")
    ap.add_argument("--body-only", action="store_true",
                    help="skip the first/last 5%% of each book (old behaviour; it cut real content from scans)")
    ap.add_argument("--all-pages", action="store_true", help="(default now) OCR every low-quality page, front and back matter included")
    ap.add_argument("--recheck-done", action="store_true",
                    help="also run books already marked done (e.g. to OCR the edges skipped by an earlier --body-only run)")
    ap.add_argument("--limit", type=int, help="do at most N books still to do (trial runs)")
    ap.add_argument("--stall-min", type=int, default=20, help="kill a book after N minutes with no output and no new page")
    ap.add_argument("--book-hours", type=int, default=6, help="hard limit per book")
    ap.add_argument("--cooldown", type=int, default=30, help="minutes to wait when Codex fails most pages")
    ap.add_argument("--max-cooldowns", type=int, default=6, help="give up after this many cooldowns in a row")
    ap.add_argument("--no-audit", action="store_true", help="skip the first 'audit --mark'")
    ap.add_argument("--wait-for-quota", action="store_true",
                    help="when Codex reports its usage limit, sleep until the reset time it gives and carry on")
    a = ap.parse_args()
    a.all_pages = not a.body_only
    b = Batch(a)
    try:
        return b.run()
    except KeyboardInterrupt:
        # the child runs in its own process group (so a stall can be killed as a tree), so Ctrl+C
        # does not reach it: stop it and its Codex workers here
        if b.proc and b.proc.poll() is None:
            kill_tree(b.proc)
        b.phase, b.note = "stopped", "(Ctrl+C; finished OCR pages are cached)"
        b.write_status()
        b.log("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
