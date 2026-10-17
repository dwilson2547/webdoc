"""The page tree inside a domain (design §3).

Any document can have child pages; there is no separate folder object, so a
"folder" is a page with children. A parent is always in the same domain, and
a page is never moved under itself or one of its own descendants.
Contributions stay out of the tree (they are listed by their forum, §12).

Siblings are ordered by `position`, kept numbered 0..n-1 whenever one moves.
Changing a page's parent is a revision (documents.write); reordering among
siblings is not.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import access
from .models import Document, Domain, User

SEP = "/"


class TreeError(ValueError):
    pass


def _live(domain_id: str):
    return (Document.domain_id == domain_id, Document.deleted_at.is_(None),
            Document.kind != "contribution")


def check_parent(doc: Document | None, parent: Document | None, domain_id: str) -> None:
    """Refuse a parent that can't hold `doc` (None: a new page)."""
    if parent is None:
        return
    if parent.domain_id != domain_id:
        raise TreeError("a page's parent must be in the same domain")
    if parent.deleted_at is not None:
        raise TreeError(f"{parent.title} is in the trash")
    if parent.kind == "contribution" or (doc is not None and doc.kind == "contribution"):
        raise TreeError("contributions stay out of the page tree")
    if doc is not None:
        node: Document | None = parent
        while node is not None:
            if node.id == doc.id:
                raise TreeError("a page can't move under itself or one of its own pages")
            node = node.parent


def resolve(s: Session, d: Domain, ref: str) -> Document:
    """A live page of this domain by id, or by title path ("Infra/k8s"),
    matched case-insensitively from the top. Ambiguous or unknown paths fail
    with the candidates."""
    ref = (ref or "").strip()
    doc = s.get(Document, ref)
    if doc is not None and doc.domain_id == d.id and doc.deleted_at is None \
            and doc.kind != "contribution":
        return doc
    parts = [p.strip() for p in ref.strip(SEP).split(SEP) if p.strip()]
    if not parts:
        raise TreeError("give a page id or a title path such as Infra/k8s")
    parent_id: str | None = None
    found: Document | None = None
    for i, part in enumerate(parts):
        here = list(s.scalars(select(Document).where(*_live(d.id),
                                                    Document.parent_id.is_(parent_id)
                                                    if parent_id is None
                                                    else Document.parent_id == parent_id)))
        match = [x for x in here if x.title.lower() == part.lower()]
        where = SEP.join(parts[:i]) or f"the top of {d.name}"
        if not match:
            names = ", ".join(sorted(x.title for x in here)[:20]) or "nothing"
            raise TreeError(f"no page {part!r} under {where}; there: {names}")
        if len(match) > 1:
            raise TreeError(f"{len(match)} pages are called {part!r} under {where}; use an id: "
                            + ", ".join(x.id for x in match))
        found = match[0]
        parent_id = found.id
    assert found is not None
    return found


def ancestors(doc: Document) -> list[Document]:
    """From the top of the domain down to the page's parent."""
    out: list[Document] = []
    node = doc.parent
    while node is not None and len(out) < 100:
        out.append(node)
        node = node.parent
    return out[::-1]


def path(doc: Document) -> str:
    return SEP.join([a.title for a in ancestors(doc)] + [doc.title])


def _links(s: Session, domain_id: str, *conds) -> dict[str | None, list[str]]:
    rows = s.execute(select(Document.id, Document.parent_id)
                     .where(Document.domain_id == domain_id, *conds)).all()
    kids: dict[str | None, list[str]] = {}
    for i, p in rows:
        kids.setdefault(p, []).append(i)
    return kids


def descendant_ids(s: Session, doc: Document, *conds) -> list[str]:
    """Every page below `doc`, parents before children."""
    kids = _links(s, doc.domain_id, *conds)
    out, todo = [], list(kids.get(doc.id, []))
    while todo:
        i = todo.pop(0)
        out.append(i)
        todo.extend(kids.get(i, []))
    return out


def siblings(s: Session, domain_id: str, parent_id: str | None,
             exclude: str | None = None) -> list[Document]:
    q = select(Document).where(*_live(domain_id),
                               Document.parent_id.is_(None) if parent_id is None
                               else Document.parent_id == parent_id)
    if exclude:
        q = q.where(Document.id != exclude)
    return list(s.scalars(q.order_by(Document.position, Document.title)))


def place(s: Session, doc: Document, position: int | None = None) -> None:
    """Put `doc` at `position` among its current siblings (None: last) and
    renumber them."""
    rest = siblings(s, doc.domain_id, doc.parent_id, exclude=doc.id)
    at = len(rest) if position is None else max(0, min(position, len(rest)))
    rest.insert(at, doc)
    for i, x in enumerate(rest):
        x.position = i


def next_position(s: Session, domain_id: str, parent_id: str | None) -> int:
    return len(siblings(s, domain_id, parent_id))


def outline(s: Session, user: User | None, d: Domain, under: Document | None = None,
            depth: int | None = None) -> list[dict]:
    """The tree (or the part under one page) as nested nodes, siblings in order."""
    rows = list(s.scalars(select(Document).where(*_live(d.id), access.visible_documents(s, user))
                          .order_by(Document.position, Document.title)))
    kids: dict[str | None, list[Document]] = {}
    for x in rows:
        kids.setdefault(x.parent_id, []).append(x)
    known = {x.id for x in rows}

    def node(x: Document, level: int) -> dict:
        o = {"id": x.id, "title": x.title, "kind": x.kind, "type": x.template_type,
             "status": x.status, "updated_at": x.updated_at.isoformat(),
             "child_count": len(kids.get(x.id, []))}
        if depth is None or level < depth:
            o["children"] = [node(c, level + 1) for c in kids.get(x.id, [])]
        return o

    if under is not None:
        return [node(c, 1) for c in kids.get(under.id, [])]
    # A page whose parent the caller can't see (a hidden one) shows at the top.
    top = [x for x in rows if x.parent_id is None or x.parent_id not in known]
    return [node(x, 1) for x in top]


def flat(s: Session, user: User | None, d: Domain) -> list[dict]:
    """Every visible page with its parent and position, depth first (each page
    followed by its children), for the sidebar."""
    rows = list(s.scalars(select(Document).where(*_live(d.id), access.visible_documents(s, user))
                          .order_by(Document.position, Document.title)))
    kids: dict[str | None, list[Document]] = {}
    for x in rows:
        kids.setdefault(x.parent_id, []).append(x)
    known = {x.id for x in rows}
    out: list[dict] = []

    def walk(x: Document, parent_id: str | None, depth: int) -> None:
        out.append({"id": x.id, "title": x.title, "kind": x.kind, "type": x.template_type,
                    "parent_id": parent_id, "position": x.position, "depth": depth,
                    "status": x.status, "updated_at": x.updated_at.isoformat()})
        for c in kids.get(x.id, []):
            walk(c, x.id, depth + 1)

    # A page whose parent the caller can't see (a hidden one) shows at the top.
    for x in rows:
        if x.parent_id is None or x.parent_id not in known:
            walk(x, None, 0)
    return out


def renumber(s: Session, domain_id: str, parent_id: str | None) -> None:
    for i, x in enumerate(siblings(s, domain_id, parent_id)):
        x.position = i
