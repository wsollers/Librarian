"""The in-memory vector index must return what SQLite's vectors return."""
import sqlite3

import numpy as np
import pytest

from librarian import memvec
from librarian.index import VectorStore, has_vec

DIM = 16


def _unit(rng, n):
    m = rng.standard_normal((n, DIM)).astype(np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def _db(n=2500, deleted=range(100, 400)):
    """A vec0 index spanning several sqlite-vec chunks (1024 rows each), with a run of deleted rows."""
    con = sqlite3.connect(":memory:")
    if not has_vec(con):
        pytest.skip("sqlite-vec not installed")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    store = VectorStore(con, DIM, "test:unit")
    m = _unit(np.random.default_rng(1), n)
    ids = list(range(10, 10 + n))
    store.add(ids, m)
    store.delete([ids[i] for i in deleted])
    keep = [i for i in range(n) if i not in set(deleted)]
    return con, store, np.array(ids)[keep], m[keep]


def test_read_vec0_matches_what_was_stored():
    con, _, ids, m = _db()
    got_ids, got = memvec.read_vec0(con, DIM)
    order = np.argsort(got_ids)
    assert np.array_equal(got_ids[order], ids)
    assert np.allclose(got[order].astype(np.float32), m, atol=1e-3)
    memvec.check_vec0(con, got_ids, got)          # its own consistency check passes


def test_check_vec0_catches_a_wrong_read():
    con, _, ids, m = _db()
    got_ids, got = memvec.read_vec0(con, DIM)
    with pytest.raises(RuntimeError):
        memvec.check_vec0(con, got_ids[:-1], got[:-1])
    bad = got.copy()
    bad[:] = 0
    with pytest.raises(RuntimeError):
        memvec.check_vec0(con, got_ids, bad)


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_knn_matches_sqlite(device):
    if device == "cuda":
        torch = pytest.importorskip("torch")
        if not torch.cuda.is_available():
            pytest.skip("no CUDA device")
    con, store, ids, _ = _db()
    mi = memvec.load(con, DIM, device)
    assert mi.n == len(ids)
    rng = np.random.default_rng(2)
    def same(got, want):
        # fp16 can swap near-ties, so compare the exact similarity of each position, not the ids.
        assert len(got) == len(want)
        assert max(abs(a - b) for (_, a), (_, b) in zip(got, want)) < 5e-3
        assert len({i for i, _ in got} & {i for i, _ in want}) >= len(want) - 2

    for q in _unit(rng, 5):
        same(mi.knn(q, 20), store.knn(q, 20))
        scope = [int(i) for i in ids[::7]] + [999_999]          # includes an id that does not exist
        got = mi.knn_among(q, scope, 10)
        same(got, store.knn_among(q, scope, 10))
        assert all(i in set(scope) for i, _ in got)


def test_knn_handles_small_and_empty_requests():
    con, _, ids, _ = _db(n=50, deleted=[])
    mi = memvec.load(con, DIM, "cpu")
    q = _unit(np.random.default_rng(3), 1)[0]
    assert len(mi.knn(q, 500)) == 50
    assert mi.knn_among(q, [], 5) == []
    assert mi.knn_among(q, [123456], 5) == []


def test_off_switch(monkeypatch):
    monkeypatch.setenv("LIBRARIAN_VEC_MEM", "off")
    assert memvec.wanted_device() is None
    monkeypatch.setenv("LIBRARIAN_VEC_MEM", "cpu")
    assert memvec.wanted_device() == "cpu"
