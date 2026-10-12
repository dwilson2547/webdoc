"""Keeps the search chunks in step with documents (design §8).

Writes never wait for embedding. A document whose `indexed_revision` differs
from its `revision` is stale; a background thread picks stale documents up,
splits them by heading, and embeds only the chunks whose text changed (the
rest keep their vectors). Trashed documents keep their chunks, and search
skips them; purging deletes them with the document.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from . import sections
from .db import Database
from .embeddings import Embedder
from .models import Document, DocumentChunk

log = logging.getLogger("webdoc.indexer")

MAX_CHARS = 1000            # MiniLM reads ~256 tokens; longer text is cut off
BATCH = 20


def _pieces(text: str) -> list[str]:
    """Split text longer than MAX_CHARS at blank lines, then hard, if needed."""
    if len(text) <= MAX_CHARS:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > MAX_CHARS:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:MAX_CHARS])
            para = para[MAX_CHARS:]
        if cur and len(cur) + 2 + len(para) > MAX_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [p for p in out if p.strip()]


def chunk(title: str, body: str) -> list[dict]:
    """A chunk per stretch of text between headings (so a section's own text,
    not its subsections), each carrying the title and heading path so it
    makes sense on its own. A document with no text gets one title chunk."""
    lines = body.split("\n")
    heads = sections.parse(body)
    starts = [(0, ())] + [(h.line, h.path) for h in heads]
    out = []
    for i, (start, path) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        seg = lines[start:end]
        if path:
            seg = seg[1:]                      # the heading is in the path
        text = "\n".join(seg).strip()
        if not text:
            continue
        heading = " > ".join(path)
        for piece in _pieces(text):
            out.append({"heading": heading, "line": start,
                        "content": f"{title}\n{heading}\n\n{piece}" if heading else f"{title}\n\n{piece}"})
    if not out:
        out.append({"heading": "", "line": 0, "content": title})
    return out


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def index_document(s: Session, embedder: Embedder | None, doc: Document) -> int:
    """Rebuild the document's chunks; returns how many were embedded."""
    old = {c.hash: c.embedding for c in s.scalars(
        select(DocumentChunk).where(DocumentChunk.document_id == doc.id))
        if c.embedding is not None and c.model == (embedder.name if embedder else "")}
    pieces = chunk(doc.title, doc.body or "")
    for p in pieces:
        p["hash"] = _hash(p["content"])
    todo = [p for p in pieces if p["hash"] not in old]
    if embedder is not None and todo:
        for p, vec in zip(todo, embedder.passages([p["content"] for p in todo])):
            old[p["hash"]] = vec
    s.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    for i, p in enumerate(pieces):
        s.add(DocumentChunk(document_id=doc.id, domain_id=doc.domain_id, ord=i,
                            heading=p["heading"][:500], line=p["line"], content=p["content"],
                            hash=p["hash"], model=embedder.name if embedder else "",
                            embedding=old.get(p["hash"]) if embedder else None))
    doc.indexed_revision = doc.revision
    return len(todo) if embedder else 0


def stale():
    return or_(Document.indexed_revision.is_(None), Document.indexed_revision != Document.revision)


def pending(s: Session) -> int:
    return s.scalar(select(func.count()).select_from(Document).where(stale())) or 0


def index_pending(db: Database, embedder: Embedder | None, batch: int = BATCH) -> int:
    """Index up to `batch` stale documents; returns how many. Each document
    commits on its own, so a bad one doesn't hold up the rest."""
    with db.session() as s:
        ids = list(s.scalars(select(Document.id).where(stale())
                             .order_by(Document.updated_at.desc()).limit(batch)))
    done = 0
    for doc_id in ids:
        try:
            with db.session() as s:
                # SKIP LOCKED: a second replica (or a write in flight) leaves
                # this document for the next pass rather than waiting on it.
                doc = s.scalar(select(Document).where(Document.id == doc_id)
                               .with_for_update(skip_locked=True))
                if doc is not None and doc.indexed_revision != doc.revision:
                    index_document(s, embedder, doc)
                    done += 1
        except Exception:
            log.exception("indexing document %s failed", doc_id)
    return done


def reset_model(db: Database, embedder: Embedder | None) -> int:
    """Mark documents stale whose chunks came from another model (or none),
    so switching models re-embeds everything once."""
    name = embedder.name if embedder else ""
    with db.session() as s:
        ids = select(DocumentChunk.document_id).where(DocumentChunk.model != name).distinct()
        res = s.execute(update(Document).where(Document.id.in_(ids))
                        .values(indexed_revision=None))
        return res.rowcount or 0


class Indexer:
    """The background thread. It polls for stale documents every `interval`
    seconds when idle, and straight away while there is a backlog."""

    def __init__(self, db: Database, embedder: Embedder | None, interval: float = 2.0):
        self.db, self.embedder, self.interval = db, embedder, interval
        self.error: str | None = None
        self.ready = embedder is None
        self._prepared = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="webdoc-indexer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    @property
    def semantic(self) -> Embedder | None:
        """The embedder search may use: none until the model has loaded."""
        return self.embedder if self.ready else None

    def _prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        if self.embedder is not None:
            try:
                self.embedder.query("warm up")
            except Exception as exc:
                # Without the model, index for keyword search only rather than
                # failing every document; fixing the model re-indexes (reset_model).
                log.exception("the embedding model failed to load; semantic search is off")
                self.error = f"embedding model unavailable: {exc}"[:300]
                self.embedder = None
            self.ready = True
        try:
            n = reset_model(self.db, self.embedder)
            if n:
                log.info("re-indexing %d documents for model %s", n,
                         self.embedder.name if self.embedder else "none")
        except Exception:
            log.exception("checking the index model failed")

    def run_once(self) -> int:
        """Index everything stale now (tests, and start-up without the thread)."""
        self._prepare()
        total = 0
        while (n := index_pending(self.db, self.embedder)) > 0:
            total += n
        return total

    def _run(self) -> None:
        self._prepare()
        while not self._stop.is_set():
            try:
                busy = index_pending(self.db, self.embedder) > 0
            except Exception:
                log.exception("indexer pass failed")
                busy = False
            if not busy:
                self._stop.wait(self.interval)
