"""Domains, projects and role grants (design §3, §4, §5).

Domains are an owner's closed taxonomy: this module is called from the web UI
only. The MCP server can list domains but has no way to create, rename or
delete one (proposals arrive in phase 2).
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import access
from .activity import Actor, record
from .models import (PLATFORM, Document, Domain, DomainGrant, DomainRevision, Project, User,
                     utcnow)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class DomainError(ValueError):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:63]


def personal_key(user: User) -> str:
    return f"user:{user.id}"


def _scope(owner_key: str) -> str:
    if owner_key == PLATFORM:
        return "public"
    if owner_key.startswith("user:"):
        return "personal"
    raise DomainError(f"unknown owner {owner_key!r}")


def _slug(name: str, slug: str | None) -> str:
    out = (slug or "").strip().lower() or slugify(name)
    if not SLUG_RE.match(out):
        raise DomainError("slug must be lowercase letters, digits and dashes")
    return out


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 100:
        raise DomainError("name must be 1-100 characters")
    return name


def _snapshot(s: Session, d: Domain, actor: Actor) -> None:
    s.add(DomainRevision(domain_id=d.id, revision=d.revision, slug=d.slug, name=d.name,
                         description=d.description, actor_id=actor.user.id))


def create(s: Session, actor: Actor, owner_key: str, name: str, slug: str | None = None,
           description: str = "") -> Domain:
    scope = _scope(owner_key)
    name, slug = _clean_name(name), _slug(name, slug)
    if s.scalar(select(Domain.id).where(Domain.owner_key == owner_key, Domain.slug == slug)):
        raise DomainError(f"there is already a domain {slug!r}")
    d = Domain(owner_key=owner_key, scope=scope, slug=slug, name=name,
               description=(description or "").strip(), revision=1,
               owner_user_id=owner_key.split(":", 1)[1] if scope == "personal" else None)
    s.add(d)
    s.flush()
    _snapshot(s, d, actor)
    record(s, actor, "domain.create", "domain", d.id, f"created domain {name}", d.id)
    return d


def update(s: Session, actor: Actor, d: Domain, name: str | None = None,
           slug: str | None = None, description: str | None = None) -> Domain:
    changed = []
    if name is not None and _clean_name(name) != d.name:
        d.name = _clean_name(name)
        changed.append("name")
    if slug is not None and slug != d.slug:
        new = _slug(d.name, slug)
        if s.scalar(select(Domain.id).where(Domain.owner_key == d.owner_key, Domain.slug == new,
                                            Domain.id != d.id)):
            raise DomainError(f"there is already a domain {new!r}")
        d.slug = new
        changed.append("slug")
    if description is not None and description.strip() != d.description:
        d.description = description.strip()
        changed.append("description")
    if changed:
        d.revision += 1
        d.updated_at = utcnow()
        _snapshot(s, d, actor)
        record(s, actor, "domain.update", "domain", d.id,
               f"changed {', '.join(changed)} of {d.name}", d.id)
    return d


def delete(s: Session, actor: Actor, d: Domain) -> None:
    """Only an empty domain (trash included) can go, so deleting one can never
    take documents or their history with it."""
    n = s.scalar(select(func.count()).select_from(Document).where(Document.domain_id == d.id))
    if n:
        raise DomainError(f"{d.name} still holds {n} document(s), counting the trash; "
                          "move or purge them first")
    record(s, actor, "domain.delete", "domain", d.id, f"deleted domain {d.name}", d.id)
    s.delete(d)


def counts(s: Session, domain_ids: list[str]) -> dict[str, tuple[int, object]]:
    """Live document count and last update per domain."""
    if not domain_ids:
        return {}
    rows = s.execute(select(Document.domain_id, func.count(), func.max(Document.updated_at))
                     .where(Document.domain_id.in_(domain_ids), Document.deleted_at.is_(None),
                            Document.status == "published")
                     .group_by(Document.domain_id))
    return {r[0]: (r[1], r[2]) for r in rows}


def out(s: Session, d: Domain, user: User | None, stats=None) -> dict:
    n, last = (stats or {}).get(d.id, (0, None))
    return {"id": d.id, "slug": d.slug, "name": d.name, "description": d.description,
            "scope": d.scope, "owner": d.owner.username if d.owner else None,
            "role": access.role_on(s, user, d), "documents": n,
            "accepts_contributions": d.accepts_contributions,
            "contribution_types": list(d.contribution_types or []),
            "can_contribute": user is not None and d.scope == "public" and d.accepts_contributions,
            "last_activity": (last or d.updated_at).isoformat()}


# --- projects -------------------------------------------------------------------

def create_project(s: Session, actor: Actor, d: Domain, name: str,
                   description: str = "") -> Project:
    name, slug = _clean_name(name), _slug(name, None)
    if s.scalar(select(Project.id).where(Project.domain_id == d.id, Project.slug == slug)):
        raise DomainError(f"{d.name} already has a project {slug!r}")
    p = Project(domain_id=d.id, slug=slug, name=name, description=(description or "").strip(),
                created_by=actor.user.id)
    s.add(p)
    s.flush()
    record(s, actor, "project.create", "project", p.id, f"created project {name} in {d.name}", d.id)
    return p


def find_project(s: Session, d: Domain, ref: str) -> Project:
    """A project of this domain by id, slug or name."""
    ref = (ref or "").strip()
    p = s.scalar(select(Project).where(Project.domain_id == d.id, (Project.id == ref)
                                       | (Project.slug == ref.lower())
                                       | (func.lower(Project.name) == ref.lower())))
    if p is None:
        raise DomainError(f"{d.name} has no project {ref!r}; create_project makes one")
    return p


def projects(s: Session, d: Domain) -> list[Project]:
    return list(s.scalars(select(Project).where(Project.domain_id == d.id).order_by(Project.name)))


def rename_project(s: Session, actor: Actor, p: Project, name: str) -> Project:
    p.name = _clean_name(name)
    record(s, actor, "project.update", "project", p.id, f"renamed project to {p.name}", p.domain_id)
    return p


def delete_project(s: Session, actor: Actor, p: Project) -> None:
    """Its documents stay in the domain, outside any project."""
    record(s, actor, "project.delete", "project", p.id, f"deleted project {p.name}", p.domain_id)
    s.delete(p)


def project_out(p: Project) -> dict:
    return {"id": p.id, "domain_id": p.domain_id, "slug": p.slug, "name": p.name,
            "description": p.description}


# --- grants ---------------------------------------------------------------------

def grants(s: Session, d: Domain) -> list[DomainGrant]:
    return list(s.scalars(select(DomainGrant).where(DomainGrant.domain_id == d.id)
                          .order_by(DomainGrant.created_at)))


def set_grant(s: Session, actor: Actor, d: Domain, target: User, role: str) -> DomainGrant:
    allowed = access.assignable(s, actor.user, d)
    if role not in allowed:
        raise access.Denied(f"you can't give the {role} role here", 403)
    if d.scope == "personal" and target.id == d.owner_user_id:
        raise DomainError("the owner already has full control")
    row = s.scalar(select(DomainGrant).where(DomainGrant.domain_id == d.id,
                                             DomainGrant.user_id == target.id))
    if row is not None and row.role not in allowed:
        raise access.Denied(f"you can't change a {row.role}'s role", 403)
    if row is None:
        row = DomainGrant(domain_id=d.id, user_id=target.id, role=role, granted_by=actor.user.id)
        s.add(row)
    else:
        row.role, row.granted_by = role, actor.user.id
    s.flush()
    record(s, actor, "grant.set", "domain", d.id, f"{target.username} is now {role} on {d.name}", d.id)
    return row


def remove_grant(s: Session, actor: Actor, d: Domain, target_id: str) -> None:
    row = s.scalar(select(DomainGrant).where(DomainGrant.domain_id == d.id,
                                             DomainGrant.user_id == target_id))
    if row is None:
        raise access.Denied("no such grant")
    if row.role not in access.assignable(s, actor.user, d) and target_id != actor.user.id:
        raise access.Denied(f"you can't remove a {row.role}", 403)
    record(s, actor, "grant.remove", "domain", d.id,
           f"{row.user.username} no longer has a role on {d.name}", d.id)
    s.delete(row)


def grant_out(g: DomainGrant) -> dict:
    return {"user_id": g.user_id, "username": g.user.username, "role": g.role}
