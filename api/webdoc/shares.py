"""Read-only share links to one document (design §14 phase 4), modelled on
nodewright's shares.py.

A link shows the document's current title and body to anyone holding it, no
account needed; no history, no other documents. Only someone who may grant
roles on the domain (its owner, an admin or moderator) creates or revokes
links, since a link hands read access outside the domain's members. Public
domains are readable signed out already, so they don't need links.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import access
from .activity import Actor, record
from .models import Document, ShareLink, utcnow

MAX_PER_DOCUMENT = 20


class ShareError(ValueError):
    pass


def out(link: ShareLink, public_url: str) -> dict:
    return {"id": link.id, "document_id": link.document_id,
            "url": f"{public_url.rstrip('/')}/s/{link.token}",
            "created_at": link.created_at.isoformat(),
            "by": link.creator.username if link.creator else None, "views": link.views,
            "last_viewed_at": link.last_viewed_at.isoformat() if link.last_viewed_at else None}


def for_document(s: Session, doc_id: str) -> list[ShareLink]:
    return list(s.scalars(select(ShareLink).where(ShareLink.document_id == doc_id)
                          .order_by(ShareLink.created_at)))


def create(s: Session, actor: Actor, doc: Document) -> ShareLink:
    if doc.domain.scope == "public":
        raise ShareError("documents in public domains are open to everyone already; "
                         "share the page's address")
    if len(for_document(s, doc.id)) >= MAX_PER_DOCUMENT:
        raise ShareError(f"a document can have at most {MAX_PER_DOCUMENT} share links")
    link = ShareLink(token=secrets.token_urlsafe(24), document_id=doc.id,
                     created_by=actor.user.id)
    s.add(link)
    s.flush()
    record(s, actor, "share.create", "document", doc.id,
           f"made a read-only link to {doc.title}", doc.domain_id)
    return link


def revoke(s: Session, actor: Actor, link: ShareLink) -> None:
    doc = s.get(Document, link.document_id)
    s.delete(link)
    if doc is not None:
        record(s, actor, "share.revoke", "document", doc.id,
               f"revoked a read-only link to {doc.title}", doc.domain_id)


def resolve(s: Session, token: str, count: bool = True) -> Document:
    """The document a token shows, counting the view (not for its images).
    Trashed documents aren't shown (the link works again if the document is
    restored)."""
    link = s.scalar(select(ShareLink).where(ShareLink.token == token)) if token else None
    doc = s.get(Document, link.document_id) if link else None
    if link is None or doc is None or doc.deleted_at is not None:
        raise access.Denied("this link doesn't exist or was revoked")
    if count:
        link.views += 1
        link.last_viewed_at = utcnow()
    return doc


def shared_out(doc: Document) -> dict:
    return {"title": doc.title, "body": doc.body, "kind": doc.kind, "type": doc.template_type,
            "fields": dict(doc.fields or {}), "tags": doc.tags,
            "updated_at": doc.updated_at.isoformat(), "revision": doc.revision}
