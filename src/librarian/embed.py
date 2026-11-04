"""Embedding backends. Spec string: 'st:<hf-model>' (sentence-transformers, GPU if present),
'ollama:<model>' (local Ollama), or 'hash:<dim>' (deterministic, for tests only)."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Sequence

import numpy as np

from .log import log

DEFAULT = os.environ.get("LIBRARIAN_EMBED", "st:BAAI/bge-base-en-v1.5")


class Embedder:
    spec: str
    dim: int

    def docs(self, texts: Sequence[str]) -> np.ndarray: ...
    def query(self, text: str) -> np.ndarray: ...


def _norm(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=-1, keepdims=True), 1e-12)


class STEmbedder(Embedder):
    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # heavy import, only when used
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("embedder: %s on %s%s", model, device,
                 f" ({torch.cuda.get_device_name(0)})" if device == "cuda" else "")
        self.m = SentenceTransformer(model, device=device)
        dim_fn = getattr(self.m, "get_embedding_dimension", None) or self.m.get_sentence_embedding_dimension
        self.spec, self.dim = f"st:{model}", dim_fn()
        lower = model.lower()
        self.qprefix = ("Represent this sentence for searching relevant passages: " if "bge" in lower and "m3" not in lower
                        else "search_query: " if "nomic" in lower else "query: " if "e5" in lower else "")
        self.dprefix = "search_document: " if "nomic" in lower else "passage: " if "e5" in lower else ""
        self.batch = int(os.environ.get("LIBRARIAN_EMBED_BATCH", "64"))

    def docs(self, texts):
        return _norm(self.m.encode([self.dprefix + t for t in texts], batch_size=self.batch,
                                   normalize_embeddings=True, show_progress_bar=False))

    def query(self, text):
        return _norm(self.m.encode([self.qprefix + text], normalize_embeddings=True))[0]


class Reranker:
    """Cross-encoder: scores (query, passage) pairs jointly. Default BAAI/bge-reranker-v2-m3."""

    def __init__(self, model: str):
        from sentence_transformers import CrossEncoder
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("reranker: %s on %s", model, device)
        self.m = CrossEncoder(model, device=device, max_length=512)
        if device == "cuda":
            self.m.model.half()  # fp16: ~2x faster on the GPU, same ranking
        self.model = model

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self.m.predict([(query, p) for p in passages], batch_size=64,
                                                 show_progress_bar=False)]


RERANK_DEFAULT = os.environ.get("LIBRARIAN_RERANK", "BAAI/bge-reranker-v2-m3")


def load_reranker(model: str | None = None):
    model = model or RERANK_DEFAULT
    if model.lower() in ("", "none", "off"):
        return None
    try:
        return Reranker(model)
    except ImportError:
        log.warning("reranker unavailable (sentence-transformers not installed)")
        return None


EXPAND_PROMPT = """You help search a library of mathematics textbooks.
A reader describes a concept, possibly without knowing its name:
"{q}"
Reply with JSON only: {{"terms": [up to 6 standard names and synonyms a textbook would use, most likely first],
"passage": "two sentences as a textbook would state the definition, with the usual notation"}}"""


class Expander:
    """Query expansion with a local Ollama chat model (no cloud cost)."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("LIBRARIAN_EXPAND_MODEL", "llama3.1:8b")
        self.url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/generate"

    def expand(self, q: str) -> dict | None:
        body = {"model": self.model, "prompt": EXPAND_PROMPT.format(q=q), "stream": False,
                "format": "json", "options": {"temperature": 0}}
        try:
            req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(json.load(r)["response"])
        except Exception as e:  # noqa: BLE001
            log.warning("query expansion failed (%s): %s", self.model, e)
            return None
        terms = [str(t).strip() for t in out.get("terms", []) if str(t).strip()][:6]
        return {"terms": terms, "passage": str(out.get("passage", "")).strip()}


def load_expander(spec: str | None):
    spec = spec if spec is not None else os.environ.get("LIBRARIAN_EXPAND", "ollama")
    if spec.lower() in ("", "none", "off"):
        return None
    kind, _, model = spec.partition(":")
    return Expander(model or None) if kind == "ollama" else None


class OllamaEmbedder(Embedder):
    def __init__(self, model: str):
        self.url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/") + "/api/embed"
        self.model, self.spec = model, f"ollama:{model}"
        self.qprefix, self.dprefix = ("search_query: ", "search_document: ") if "nomic" in model else ("", "")
        self.dim = len(self._call(["probe"])[0])
        log.info("embedder: ollama %s dim=%d", model, self.dim)

    def _call(self, texts):
        req = urllib.request.Request(self.url, data=json.dumps({"model": self.model, "input": list(texts)}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)["embeddings"]

    def docs(self, texts):
        return _norm(self._call([self.dprefix + t for t in texts]))

    def query(self, text):
        return _norm(self._call([self.qprefix + text]))[0]


class HashEmbedder(Embedder):
    """Bag-of-words hashing. No semantics; lets the pipeline run without a model."""

    def __init__(self, dim: int):
        self.dim, self.spec = dim, f"hash:{dim}"

    def _one(self, t: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in t.lower().split():
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest(), "little")
            v[h % self.dim] += 1.0
        return v

    def docs(self, texts):
        return _norm(np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim)))

    def query(self, text):
        return _norm(self._one(text))


def load(spec: str = DEFAULT) -> Embedder:
    kind, _, arg = spec.partition(":")
    if kind == "st":
        return STEmbedder(arg)
    if kind == "ollama":
        return OllamaEmbedder(arg)
    if kind == "hash":
        return HashEmbedder(int(arg or 256))
    raise ValueError(f"unknown embedder spec {spec!r}")
