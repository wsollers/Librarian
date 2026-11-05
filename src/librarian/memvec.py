"""All chunk vectors held in memory (on the GPU when there is one): exact nearest neighbours in milliseconds.

sqlite-vec's vec0 table answers a KNN query by scanning every stored vector, about 6 GB for the whole
library, which takes ~5 s even on an NVMe drive. Holding the vectors as one fp16 matrix (~3 GB) on the
GPU makes the same exact search a single matrix product.

Loading them through the vec0 interface (SELECT rowid, embedding) takes ~12 minutes for two million
rows, so they are read from vec0's storage tables directly, which takes seconds. That layout is a
sqlite-vec internal: the reader accepts only the v0.1 format and checks itself against vec0 (row count
and a sample of vectors) before it is used.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

import numpy as np

from .log import log

CPU_BLOCK = 262_144   # rows per block when scoring on the CPU (fp16 -> fp32 one block at a time)


def wanted_device() -> Optional[str]:
    """LIBRARIAN_VEC_MEM: auto (default: GPU if there is one, else CPU), gpu, cpu, or off."""
    mode = os.environ.get("LIBRARIAN_VEC_MEM", "auto").strip().lower()
    if mode in ("off", "0", "no", "false", "none"):
        return None
    if mode == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    if mode in ("gpu", "cuda"):
        log.warning("memvec: LIBRARIAN_VEC_MEM=%s but no CUDA device: using the CPU", mode)
    return "cpu"


class MemIndex:
    """Exact cosine search over unit vectors. Same knn / knn_among interface as index.VectorStore."""

    def __init__(self, ids: np.ndarray, vecs: np.ndarray, device: str = "cpu"):
        order = np.argsort(ids, kind="stable")
        self.ids = np.ascontiguousarray(ids[order], dtype=np.int64)
        vecs = np.ascontiguousarray(vecs[order], dtype=np.float16)
        self.n, self.dim = vecs.shape
        self.nbytes = vecs.nbytes
        self.device = "cpu"
        self.m = vecs
        if device == "cuda":
            try:
                import torch
                self.m = torch.from_numpy(vecs).to("cuda")
                self.device = "cuda"
            except Exception as e:  # noqa: BLE001 (out of GPU memory, no torch): the CPU still works
                log.warning("memvec: GPU unavailable (%s: %s): using the CPU", type(e).__name__, e)

    def _top(self, q: np.ndarray, rows: Optional[np.ndarray], k: int) -> list[tuple[int, float]]:
        n = self.n if rows is None else len(rows)
        k = min(k, n)
        if k <= 0:
            return []
        if self.device == "cuda":
            import torch
            qt = torch.from_numpy(q.astype(np.float16)).to("cuda")
            m = self.m if rows is None else self.m[torch.from_numpy(rows).to("cuda")]
            vals, idx = torch.topk((m @ qt).float(), k)
            vals, idx = vals.cpu().numpy(), idx.cpu().numpy()
        else:
            q32 = q.astype(np.float32)
            src = self.m if rows is None else self.m[rows]
            sims = np.empty(n, np.float32)
            for i in range(0, n, CPU_BLOCK):
                sims[i:i + CPU_BLOCK] = src[i:i + CPU_BLOCK].astype(np.float32) @ q32
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx], kind="stable")]
            vals = sims[idx]
        pos = idx if rows is None else rows[idx]
        return [(int(self.ids[p]), float(v)) for p, v in zip(pos, vals)]

    def knn(self, q: np.ndarray, k: int) -> list[tuple[int, float]]:
        return self._top(q, None, k)

    def knn_among(self, q: np.ndarray, ids: list[int], k: int) -> list[tuple[int, float]]:
        """Exact nearest neighbours among the given chunk ids (a book, a chapter)."""
        if not ids:
            return []
        want = np.unique(np.asarray(ids, dtype=np.int64))
        pos = np.searchsorted(self.ids, want)
        inside = pos < self.n
        pos, want = pos[inside], want[inside]
        return self._top(q, pos[self.ids[pos] == want], k)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def read_vec0(con: sqlite3.Connection, dim: int, table: str = "chunks_vec") -> tuple[np.ndarray, np.ndarray]:
    """(ids, fp16 vectors) straight from a vec0 table's storage (sqlite-vec v0.1 layout)."""
    info = dict(con.execute(f"SELECT key, value FROM {table}_info").fetchall())
    ver = str(info.get("CREATE_VERSION", ""))
    if not ver.startswith("v0.1."):
        raise RuntimeError(f"unknown sqlite-vec storage format {ver!r}")
    total = con.execute(f"SELECT coalesce(sum(size), 0) FROM {table}_chunks").fetchone()[0]
    ids = np.empty(total, np.int64)
    vecs = np.empty((total, dim), np.float16)
    n = 0
    # One chunk at a time: a join or ORDER BY over the vector blobs makes SQLite buffer all ~6 GB.
    chunks = con.execute(f"SELECT chunk_id, size, validity, rowids FROM {table}_chunks").fetchall()
    for chunk_id, size, validity, rowids in chunks:
        got = con.execute(f"SELECT vectors FROM {table}_vector_chunks00 WHERE rowid = ?", (chunk_id,)).fetchone()
        if got is None:
            raise RuntimeError(f"sqlite-vec chunk {chunk_id} has no vectors")
        blob = got[0]
        if len(blob) != size * dim * 4 or len(rowids) != size * 8 or len(validity) * 8 < size:
            raise RuntimeError("unexpected sqlite-vec chunk layout")
        valid = np.unpackbits(np.frombuffer(validity, np.uint8), bitorder="little")[:size].astype(bool)
        r = np.frombuffer(rowids, np.int64)[valid]
        ids[n:n + len(r)] = r
        vecs[n:n + len(r)] = np.frombuffer(blob, np.float32).reshape(size, dim)[valid]
        n += len(r)
    return ids[:n], vecs[:n]


def check_vec0(con: sqlite3.Connection, ids: np.ndarray, vecs: np.ndarray, table: str = "chunks_vec",
               sample: int = 32) -> None:
    """Raise unless what read_vec0 found matches vec0 itself (needs the sqlite-vec extension loaded)."""
    count = con.execute(f"SELECT count(*) FROM {table}_rowids").fetchone()[0]
    if count != len(ids):
        raise RuntimeError(f"read {len(ids)} vectors but vec0 holds {count}")
    rng = np.random.default_rng(0)
    for i in rng.choice(len(ids), size=min(sample, len(ids)), replace=False):
        row = con.execute(f"SELECT embedding FROM {table} WHERE rowid = ?", (int(ids[i]),)).fetchone()
        if row is None or not np.allclose(np.frombuffer(row[0], np.float32), vecs[i].astype(np.float32), atol=1e-3):
            raise RuntimeError(f"vector for chunk {int(ids[i])} does not match vec0")


def read_blob(con: sqlite3.Connection, dim: int) -> tuple[np.ndarray, np.ndarray]:
    total = con.execute("SELECT count(*) FROM chunks_blob").fetchone()[0]
    ids = np.empty(total, np.int64)
    vecs = np.empty((total, dim), np.float16)
    n = 0
    for i, v in con.execute("SELECT id, v FROM chunks_blob"):
        if n == total:
            break
        ids[n], vecs[n] = i, np.frombuffer(v, np.float32)
        n += 1
    return ids[:n], vecs[:n]


def load(con: sqlite3.Connection, dim: int, device: str) -> MemIndex:
    """Read every chunk vector of the index into a MemIndex on `device` ("cuda" or "cpu")."""
    t = time.perf_counter()
    tables = _tables(con)
    if "chunks_vec_vector_chunks00" in tables:
        ids, vecs = read_vec0(con, dim)
        check_vec0(con, ids, vecs)
    elif "chunks_blob" in tables:
        ids, vecs = read_blob(con, dim)
    else:
        raise RuntimeError("the index has no vectors")
    mi = MemIndex(ids, vecs, device)
    log.info("memvec: %d vectors (%.1f GB fp16) on %s in %.1fs", mi.n, mi.nbytes / 1e9, mi.device,
             time.perf_counter() - t)
    return mi
