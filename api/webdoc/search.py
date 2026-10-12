"""Search and find over everything the caller can read (design §8).

`search` is hybrid: nearest chunks by embedding, chunks by full-text rank, and
documents whose title contains the query, merged by reciprocal rank fusion and
reported per document with its best-matching section. `find` is literal: every
occurrence of a word or phrase, no embeddings involved.

Permission filtering is part of every query: candidates come only from the
domains access.visible_domains returns, narrowed by the filters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import cast, func, literal, literal_column, or_, select, text
from sqlalchemy.orm import Session

from . import access, sections, tags as tagmod
from .embeddings import Embedder
from .indexer import stale
from .models import Document, DocumentChunk, DocumentTag, Domain, PgVector, Project, User

SOURCES = ("public", "personal", "shared")
RRF_K = 60
CANDIDATES = 60
MIN_SIMILARITY = 0.18
"""Nearest neighbours always exist; below this cosine similarity they are
noise. MiniLM put related sections at 0.22-0.44 and unrelated ones at or below
0.15 on a small hand-made sample (2026-10-12); tune with real data."""
SNIPPET = 240
FIND_SCAN = 5000           # documents read per find; enough until there's a trigram index


class SearchError(ValueError):
    pass


@dataclass
class Filters:
    source: str | None = None                 # public | personal | shared
    domain_ids: list[str] | None = None
    project_id: str | None = None
    kind: str | None = None
    type: str | None = None
    tags: list[str] = field(default_factory=list)
    author: str | None = None                 # username of whoever created the document
    updated_after: datetime | None = None
    updated_before: datetime | None = None


def source_of(d: Domain, user: User | None) -> str:
    if d.scope == "public":
        return "public"
    return "personal" if user is not None and d.owner_user_id == user.id else "shared"


def scope(s: Session, user: User | None, f: Filters) -> dict[str, Domain]:
    """The readable domains the filters leave, by id."""
    if f.source is not None and f.source not in SOURCES:
        raise SearchError(f"source must be one of {', '.join(SOURCES)}")
    if f.domain_ids:
        ds = [access.domain(s, user, i) for i in f.domain_ids]
    else:
        ds = access.visible_domains(s, user)
    return {d.id: d for d in ds if f.source is None or source_of(d, user) == f.source}


def _conditions(s: Session, f: Filters) -> list | None:
    """Conditions on Document; None when a filter can't match anything."""
    c = [Document.deleted_at.is_(None)]
    if f.project_id:
        c.append(Document.project_id == f.project_id)
    if f.kind:
        c.append(Document.kind == f.kind)
    if f.type:
        c.append(Document.template_type == f.type.strip().lower())
    for t in f.tags:
        c.append(Document.id.in_(select(DocumentTag.document_id)
                                 .where(DocumentTag.tag == tagmod.normalise(t))))
    if f.author:
        uid = s.scalar(select(User.id).where(User.username == f.author.strip()))
        if uid is None:
            return None
        c.append(Document.created_by == uid)
    if f.updated_after:
        c.append(Document.updated_at >= f.updated_after)
    if f.updated_before:
        c.append(Document.updated_at <= f.updated_before)
    return c


def _meta(doc: Document, d: Domain, user: User | None) -> dict:
    return {"id": doc.id, "title": doc.title, "kind": doc.kind, "type": doc.template_type,
            "domain_id": d.id, "domain": d.name, "source": source_of(d, user),
            "owner": d.owner.username if d.scope != "public" and d.owner else None,
            "project_id": doc.project_id, "project": doc.project.name if doc.project else None,
            "tags": doc.tags, "updated_at": doc.updated_at.isoformat()}


# --- search ---------------------------------------------------------------------

_WORD = re.compile(r"\w+")
_STOP = frozenset("a an and are as at be by for from how i in is it of on or that the this to "
                  "was what when where which who why with".split())


def _terms(q: str) -> list[str]:
    return [w for w in dict.fromkeys(_WORD.findall(q.lower())) if len(w) > 1 and w not in _STOP]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))       # both are unit length


def _vector_hits(s: Session, emb: Embedder, q: str, base) -> list[tuple[str, str]]:
    """(chunk id, document id), nearest first."""
    qv = emb.query(q)
    base = base.where(DocumentChunk.embedding.is_not(None), DocumentChunk.model == emb.name)
    if s.bind.dialect.name == "postgresql":
        # Filtered HNSW scans stop at ef_search rows; let pgvector keep going
        # until enough rows pass the filters (pgvector >= 0.8).
        s.execute(text("SET LOCAL hnsw.ef_search = 100"))
        s.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        vec = cast(literal("[" + ",".join(f"{x:.7g}" for x in qv) + "]"), PgVector())
        dist = DocumentChunk.embedding.op("<=>", return_type=PgVector)(vec)
        rows = s.execute(base.add_columns(dist.label("dist")).order_by(dist).limit(CANDIDATES))
        return [(r[0], r[1]) for r in rows if 1 - r[2] >= MIN_SIMILARITY]
    rows = s.execute(base.add_columns(DocumentChunk.embedding))
    scored = sorted(((_cosine(qv, r[2]), r[0], r[1]) for r in rows), reverse=True)
    return [(cid, did) for sim, cid, did in scored[:CANDIDATES] if sim >= MIN_SIMILARITY]


def _keyword_hits(s: Session, q: str, base) -> list[tuple[str, str]]:
    if s.bind.dialect.name == "postgresql":
        # The same expression as the GIN index (migration 0003), so it is used.
        tsv = func.to_tsvector(literal_column("'english'"), DocumentChunk.content)
        tsq = func.websearch_to_tsquery(literal_column("'english'"), q)
        rank = func.ts_rank_cd(tsv, tsq)
        rows = s.execute(base.where(tsv.op("@@")(tsq)).order_by(rank.desc()).limit(CANDIDATES))
        return [(r[0], r[1]) for r in rows]
    terms = _terms(q)
    if not terms:
        return []
    rows = s.execute(base.add_columns(DocumentChunk.content).where(
        or_(*[func.lower(DocumentChunk.content).contains(t, autoescape=True) for t in terms])))
    scored = []
    for cid, did, content in rows:
        low = content.lower()
        hit = [t for t in terms if t in low]
        scored.append((len(hit), sum(low.count(t) for t in hit), cid, did))
    scored.sort(reverse=True)
    return [(cid, did) for *_, cid, did in scored[:CANDIDATES]]


def _snippet(content: str, terms: list[str]) -> str:
    body = content.split("\n\n", 1)[1] if "\n\n" in content else content
    flat = " ".join(body.split())
    low = flat.lower()
    at = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, at - SNIPPET // 3)
    piece = flat[start:start + SNIPPET]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET < len(flat) else "")


def search(s: Session, user: User | None, emb: Embedder | None, query: str, f: Filters,
           limit: int = 20) -> dict:
    query = " ".join((query or "").split())
    if not query:
        raise SearchError("query is empty")
    domains = scope(s, user, f)
    cond = _conditions(s, f)
    out = {"query": query, "semantic": emb is not None, "results": [],
           "pending": _pending(s, list(domains)) if domains else 0}
    if not domains or cond is None:
        return out
    base = (select(DocumentChunk.id, DocumentChunk.document_id)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.domain_id.in_(list(domains)),
                   Document.domain_id == DocumentChunk.domain_id, *cond))
    lists = {"keyword": _keyword_hits(s, query, base)}
    if emb is not None:
        lists["semantic"] = _vector_hits(s, emb, query, base)
    titles = list(s.scalars(select(Document.id).where(
        Document.domain_id.in_(list(domains)), *cond,
        func.lower(Document.title).contains(query.lower(), autoescape=True))
        .order_by(Document.updated_at.desc()).limit(CANDIDATES)))

    doc_score: dict[str, float] = {}
    how: dict[str, set] = {}
    chunk_score: dict[str, float] = {}
    best: dict[str, str] = {}
    for name, hits in lists.items():
        seen: set[str] = set()
        for rank, (cid, did) in enumerate(hits):
            chunk_score[cid] = chunk_score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            if did not in seen:
                seen.add(did)
                doc_score[did] = doc_score.get(did, 0.0) + 1.0 / (RRF_K + len(seen) - 1)
                how.setdefault(did, set()).add(name)
            if did not in best or chunk_score[cid] > chunk_score[best[did]]:
                best[did] = cid
    for rank, did in enumerate(titles):
        doc_score[did] = doc_score.get(did, 0.0) + 1.0 / (RRF_K + rank)
        how.setdefault(did, set()).add("title")

    top = sorted(doc_score, key=doc_score.get, reverse=True)[:limit]
    docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_(top)))}
    chunks = {c.id: c for c in s.scalars(select(DocumentChunk).where(
        DocumentChunk.id.in_([best[d] for d in top if d in best])))}
    terms = _terms(query)
    for did in top:
        doc = docs[did]
        o = _meta(doc, domains[doc.domain_id], user)
        c = chunks.get(best.get(did, ""))
        o["heading"] = c.heading if c else ""
        o["snippet"] = _snippet(c.content, terms) if c else _snippet(doc.body or "", terms)
        o["matched"] = sorted(how[did])
        o["score"] = round(doc_score[did] * 1000, 2)
        out["results"].append(o)
    return out


def _pending(s: Session, domain_ids: list[str]) -> int:
    return s.scalar(select(func.count()).select_from(Document).where(
        Document.domain_id.in_(domain_ids), Document.deleted_at.is_(None), stale())) or 0


# --- find -----------------------------------------------------------------------

def pattern(needle: str, whole_word: bool, case_sensitive: bool) -> re.Pattern:
    """Literal text; whole_word adds a word boundary on each end that is a
    word character, so "E-1234" and "foo.bar()" still work."""
    p = re.escape(needle)
    if whole_word:
        if re.match(r"\w", needle[0]):
            p = r"(?<!\w)" + p
        if re.match(r"\w", needle[-1]):
            p = p + r"(?!\w)"
    return re.compile(p, 0 if case_sensitive else re.IGNORECASE)


def _heading_at(heads, line: int) -> str:
    path: tuple = ()
    for h in heads:
        if h.line <= line < h.end:
            path = h.path
    return " > ".join(path)


def _line_excerpt(line: str, m: re.Match) -> str:
    line = line.strip()
    if len(line) <= SNIPPET:
        return line
    at = max(0, line.find(m.group(0)) - SNIPPET // 3)
    return ("…" if at else "") + line[at:at + SNIPPET] + ("…" if at + SNIPPET < len(line) else "")


def find(s: Session, user: User | None, needle: str, f: Filters, *, whole_word: bool = True,
         case_sensitive: bool = False, limit: int = 20, per_document: int = 5) -> dict:
    needle = (needle or "").strip()
    if not needle:
        raise SearchError("text to find is empty")
    if len(needle) > 200:
        raise SearchError("text to find is longer than 200 characters")
    domains = scope(s, user, f)
    cond = _conditions(s, f)
    out = {"text": needle, "whole_word": whole_word, "case_sensitive": case_sensitive,
           "results": [], "limited": False}
    if not domains or cond is None:
        return out
    pat = pattern(needle, whole_word, case_sensitive)
    low = needle.lower()
    # SQL narrows to documents containing the text in any case; the regex
    # then applies case and word boundaries.
    q = (select(Document).where(Document.domain_id.in_(list(domains)), *cond,
                                or_(func.lower(Document.body).contains(low, autoescape=True),
                                    func.lower(Document.title).contains(low, autoescape=True)))
         .order_by(Document.updated_at.desc()).limit(FIND_SCAN))
    for doc in s.scalars(q):
        matches, count = [], 0
        if pat.search(doc.title):
            count += 1
            matches.append({"line": 0, "heading": "", "text": doc.title, "in": "title"})
        lines = (doc.body or "").split("\n")
        heads = None
        for i, ln in enumerate(lines):
            hits = list(pat.finditer(ln))
            if not hits:
                continue
            count += len(hits)
            if len(matches) < per_document:
                heads = heads if heads is not None else sections.parse(doc.body)
                matches.append({"line": i + 1, "heading": _heading_at(heads, i),
                                "text": _line_excerpt(ln, hits[0]), "in": "body"})
        if count:
            o = _meta(doc, domains[doc.domain_id], user)
            o["count"], o["matches"] = count, matches
            out["results"].append(o)
            if len(out["results"]) >= limit:
                out["limited"] = True   # there may be more
                break
    return out
