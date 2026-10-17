"""Every permission decision (design §4).

Routes and MCP tools call these; none of them compare owner ids or roles
themselves. `user` is None for a signed-out visitor, who can read public
domains and nothing else.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import Session

from .models import Document, Domain, DomainGrant, User

READ = "read"
WRITE = "write"                  # create and edit documents and notes
DELETE = "delete"                # move to trash, restore from trash
PURGE = "purge"                  # delete permanently
MANAGE = "manage"                # rename, describe or delete the domain itself
GRANTS = "grants"                # give other people roles on the domain
MODERATE = "moderate"            # hide, lock, mark duplicates, handle reports, contribution settings

ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset({READ, WRITE, DELETE, PURGE, MANAGE, GRANTS, MODERATE}),
    "owner": frozenset({READ, WRITE, DELETE, PURGE, MANAGE, GRANTS, MODERATE}),
    "moderator": frozenset({READ, WRITE, DELETE, GRANTS, MODERATE}),
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


def is_editor(user: User | None) -> bool:
    """Editors (and admins) create and write; viewers only read and report."""
    return user is not None and (user.is_admin or user.is_editor)


def role_on(s: Session, user: User | None, d: Domain) -> str | None:
    """The user's role on a domain. A viewer's is capped at reader, wherever
    they own or were granted more (design §4)."""
    role = _role_on(s, user, d)
    if role is not None and role != "reader" and not is_editor(user):
        return "reader"
    return role


def _role_on(s: Session, user: User | None, d: Domain) -> str | None:
    """Admins control public domains; personal domains are their owner's and
    whoever they share with, admins included (design §4: admins review a
    personal document only through a report, which is not built yet)."""
    if user is not None:
        if user.is_admin and d.scope == "public":
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


def can_see(user: User | None, doc: Document, role: str | None) -> bool:
    """Drafts are their author's alone; hidden documents are seen by their
    author and the domain's moderators (design §12)."""
    if role is None:
        return False
    if doc.status == "published":
        return True
    if user is not None and doc.created_by == user.id:
        return True
    return doc.status == "hidden" and MODERATE in ROLES[role]


def doc_allows(user: User | None, doc: Document, role: str | None, action: str) -> bool:
    """The domain role, plus a contribution's author editing and trashing
    their own (they need no role on a public domain); a moderator's lock
    stops everyone but moderators."""
    if role is None:
        return False
    ok = action in ROLES[role]
    if not ok and doc.kind == "contribution" and user is not None \
            and doc.created_by == user.id and action in (WRITE, DELETE):
        ok = True
    if ok and action == WRITE and (doc.locked or doc.merged_into_id) \
            and MODERATE not in ROLES[role]:
        return False
    return ok


def document(s: Session, user: User | None, doc_id: str, action: str = READ,
             trashed: bool = False) -> Document:
    """A document the user may act on. Trashed documents are only found when
    `trashed` is set (restore, purge, history)."""
    doc = s.get(Document, doc_id)
    if doc is None or (doc.deleted_at is not None and not trashed):
        raise Denied("document not found")
    d = doc.domain
    role = role_on(s, user, d)
    if not can_see(user, doc, role):
        raise Denied("document not found")
    if not doc_allows(user, doc, role, action):
        why = ("a moderator locked this contribution" if doc.locked or doc.merged_into_id
               else "you can't do that in this domain")
        raise Denied(why, 403)
    return doc


def moderated_domain_ids(s: Session, user: User | None) -> list[str]:
    if user is None:
        return []
    q = select(Domain.id).where(Domain.id.in_(
        select(DomainGrant.domain_id).where(DomainGrant.user_id == user.id,
                                            DomainGrant.role == "moderator")))
    if user.is_admin:
        q = select(Domain.id).where(or_(Domain.scope == "public", Domain.id.in_(q)))
    return list(s.scalars(q))


def visible_documents(s: Session, user: User | None):
    """SQL condition matching can_see, for lists and search (callers already
    limit to readable domains)."""
    if user is None:
        return Document.status == "published"
    mod = moderated_domain_ids(s, user)
    return or_(Document.status == "published", Document.created_by == user.id,
               and_(Document.status == "hidden", Document.domain_id.in_(mod)) if mod else false())


def visible_domains(s: Session, user: User | None) -> list[Domain]:
    """Public domains, the user's own, and those shared with them."""
    q = select(Domain).where(Domain.scope == "public")
    if user is not None:
        shared = select(DomainGrant.domain_id).where(DomainGrant.user_id == user.id)
        q = select(Domain).where(or_(Domain.scope == "public", Domain.owner_user_id == user.id,
                                     Domain.id.in_(shared)))
    return list(s.scalars(q.order_by(Domain.name)))


def http(exc: Denied) -> HTTPException:
    return HTTPException(exc.status, str(exc))
