"""Every permission decision (design §4).

Routes and MCP tools call these; none of them compare owner ids or roles
themselves. `user` is None for a signed-out visitor, who can read public
domains and nothing else.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Document, Domain, DomainGrant, Project, User

READ = "read"
WRITE = "write"                  # create and edit documents, notes and projects
DELETE = "delete"                # move to trash, restore from trash
PURGE = "purge"                  # delete permanently
MANAGE = "manage"                # rename, describe or delete the domain itself
GRANTS = "grants"                # give other people roles on the domain

ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset({READ, WRITE, DELETE, PURGE, MANAGE, GRANTS}),
    "owner": frozenset({READ, WRITE, DELETE, PURGE, MANAGE, GRANTS}),
    "moderator": frozenset({READ, WRITE, DELETE, GRANTS}),
    "editor": frozenset({READ, WRITE, DELETE}),
    "reader": frozenset({READ}),
}
GRANTABLE = ("reader", "editor", "moderator")


class Denied(Exception):
    """Not found or not allowed; the two are deliberately indistinguishable
    to the caller unless it can at least read the domain."""

    def __init__(self, what: str, status: int = 404):
        super().__init__(what)
        self.status = status


def role_on(s: Session, user: User | None, d: Domain) -> str | None:
    if user is not None:
        if user.is_admin:
            return "admin"
        if d.scope == "personal" and d.owner_user_id == user.id:
            return "owner"
        role = s.scalar(select(DomainGrant.role).where(
            DomainGrant.domain_id == d.id, DomainGrant.user_id == user.id))
        if role:
            return role
    return "reader" if d.scope == "public" else None


def can(s: Session, user: User | None, d: Domain, action: str) -> bool:
    role = role_on(s, user, d)
    return role is not None and action in ROLES.get(role, frozenset())


def assignable(s: Session, user: User | None, d: Domain) -> list[str]:
    """Roles this user may hand out on the domain. Moderators exist only on
    public domains, and only owners and admins appoint them."""
    role = role_on(s, user, d)
    if role is None or GRANTS not in ROLES[role]:
        return []
    out = ["reader", "editor"]
    if d.scope == "public" and role in ("admin", "owner"):
        out.append("moderator")
    return out


def domain(s: Session, user: User | None, domain_id: str, action: str = READ) -> Domain:
    d = s.get(Domain, domain_id)
    role = role_on(s, user, d) if d is not None else None
    if role is None:
        raise Denied("domain not found")
    if action not in ROLES[role]:
        raise Denied("you can't do that in this domain", 403)
    return d


def project(s: Session, user: User | None, project_id: str, action: str = READ) -> Project:
    p = s.get(Project, project_id)
    if p is None:
        raise Denied("project not found")
    try:
        domain(s, user, p.domain_id, action)
    except Denied as exc:
        raise Denied("project not found" if exc.status == 404 else str(exc), exc.status) from None
    return p


def document(s: Session, user: User | None, doc_id: str, action: str = READ,
             trashed: bool = False) -> Document:
    """A document the user may act on. Trashed documents are only found when
    `trashed` is set (restore, purge, history)."""
    doc = s.get(Document, doc_id)
    if doc is None or (doc.deleted_at is not None and not trashed):
        raise Denied("document not found")
    try:
        domain(s, user, doc.domain_id, action)
    except Denied as exc:
        raise Denied("document not found" if exc.status == 404 else str(exc), exc.status) from None
    return doc


def visible_domains(s: Session, user: User | None) -> list[Domain]:
    """Public domains, the user's own, and those shared with them. Admins can
    open anyone's domain, but other people's personal domains are not listed."""
    q = select(Domain).where(Domain.scope == "public")
    if user is not None:
        shared = select(DomainGrant.domain_id).where(DomainGrant.user_id == user.id)
        q = select(Domain).where(or_(Domain.scope == "public", Domain.owner_user_id == user.id,
                                     Domain.id.in_(shared)))
    return list(s.scalars(q.order_by(Domain.name)))


def http(exc: Denied) -> HTTPException:
    return HTTPException(exc.status, str(exc))
