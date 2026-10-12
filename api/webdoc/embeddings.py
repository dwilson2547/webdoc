"""Text embeddings, always computed in-process (design §8).

FastEmbedder runs sentence-transformers/all-MiniLM-L6-v2 through fastembed
(ONNX Runtime on CPU, no PyTorch, no external API): the model doc-indexer
settled on. HashEmbedder is a deterministic bag-of-words stand-in for tests
and dev runs without the model; its "semantic" matches are keyword matches.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading

from .config import Settings
from .models import EMBED_DIM

log = logging.getLogger("webdoc.embeddings")

MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Embedder:
    name: str = ""
    dim: int = EMBED_DIM

    def passages(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def query(self, text: str) -> list[float]:
        raise NotImplementedError


class FastEmbedder(Embedder):
    name = MODEL

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        # Loaded on first use (by the indexer thread, normally), so start-up and
        # the health check don't wait for it. ONNX inference isn't documented as
        # thread-safe, so calls are serialised.
        if self._model is None:
            from fastembed import TextEmbedding
            kw = {"cache_dir": self.cache_dir, "local_files_only": True} if self.cache_dir else {}
            self._model = TextEmbedding(model_name=MODEL, threads=1, **kw)
            log.info("loaded embedding model %s", MODEL)
        return self._model

    def passages(self, texts):
        with self._lock:
            return [v.tolist() for v in self._load().embed(texts, batch_size=16)]

    def query(self, text):
        with self._lock:
            return next(iter(self._load().query_embed([text]))).tolist()


class HashEmbedder(Embedder):
    name = "hash-bow"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "big")
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def passages(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


def make(settings: Settings) -> Embedder | None:
    if settings.embedder == "fastembed":
        return FastEmbedder(settings.model_dir)
    if settings.embedder == "hash":
        return HashEmbedder()
    if settings.embedder != "off":
        log.warning("EMBEDDER=%r is not fastembed, hash or off; semantic search is off",
                    settings.embedder)
    return None
