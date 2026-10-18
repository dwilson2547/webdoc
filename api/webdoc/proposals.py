"""Proposals: how agents ask for taxonomy and template changes (design §5).

An agent can't create or change a domain or a template. It files a proposal;
whoever controls that taxonomy approves or rejects it in the web UI (the
owner for a personal domain, an admin for public ones, a moderator for their
public domain's templates). Approving applies the change as the approver.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import access, domains, templates
from .activity import Actor, record
from .models import PLATFORM, Domain, DomainGrant, Proposal, Template, User, utcnow

KINDS = ("domain.create", "domain.update", "template.create", "template.update")
MAX_PENDING_PER_USER = 25


class ProposalError(ValueError):
    pass


def _file(s: Session, actor: Actor, kind: str, owner_key: str, payload: dict, rationale: str,
          domain_id: str | None = None, target_id: str | None = None) -> Proposal:
    if not access.is_editor(actor.user):
        raise ProposalError("viewers can't propose changes; an admin can make you an editor")
    rationale = (rationale or "").strip()
    if not rationale:
        raise ProposalError("say why (rationale): the person approving reads it")
    n = s.scalar(select(func.count()).select_from(Proposal).where(
        Proposal.proposed_by == actor.user.id, Proposal.status == "pending")) or 0
    if n >= MAX_PENDING_PER_USER:
        raise ProposalError(f"{n} proposals are already waiting; wait for the user to decide on them")
    p = Proposal(kind=kind, owner_key=owner_key, domain_id=domain_id, target_id=target_id,
                 payload=payload, rationale=rationale[:4000], proposed_by=actor.user.id,
                 client=actor.client[:100])
    s.add(p)
    s.flush()
    record(s, actor, "proposal.create", "proposal", p.id, f"proposed {kind}: {payload.get('name', '')}",
           domain_id)
    return p


def propose_domain(s: Session, actor: Actor, *, scope: str, name: str, description: str,
                   rationale: str, slug: str | None = None) -> Proposal:
    owner_key = PLATFORM if scope == "public" else domains.personal_key(actor.user)
    name = domains._clean_name(name)
    slug = domains._slug(name, slug)
    if s.scalar(select(Domain.id).where(Domain.owner_key == owner_key, Domain.slug == slug)):
        raise ProposalError(f"a domain {slug!r} already exists")
    return _file(s, actor, "domain.create", owner_key,
                 {"name": name, "slug": slug, "description": (description or "").strip()}, rationale)


def propose_domain_change(s: Session, actor: Actor, d: Domain, *, rationale: str,
                          name: str | None = None, slug: str | None = None,
                          description: str | None = None) -> Proposal:
    access.domain(s, actor.user, d.id, access.WRITE)
    payload = {k: v for k, v in (("name", name), ("slug", slug), ("description", description))
               if v is not None}
    if not payload:
        raise ProposalError("nothing to change: give name, slug or description")
    if "name" in payload:
        payload["name"] = domains._clean_name(payload["name"])
    if "slug" in payload:
        payload["slug"] = domains._slug(d.name, payload["slug"])
    payload["current"] = {"name": d.name, "slug": d.slug, "description": d.description}
    return _file(s, actor, "domain.update", d.owner_key, payload, rationale, d.id)


def propose_template(s: Session, actor: Actor, d: Domain | None, *, type: str, name: str,
                     skeleton: str, rationale: str, description: str | None = None,
                     fields: list | None = None, default_tags: list | None = None) -> Proposal:
    """For one domain (needs write access there), or the proposer's own
    templates when no domain is given. Changing an existing type keeps its
    description, fields and default tags unless new ones are given."""
    if d is not None:
        access.domain(s, actor.user, d.id, access.WRITE)
    owner_key = d.owner_key if d is not None else domains.personal_key(actor.user)
    existing = select(Template).where(Template.owner_key == owner_key,
                                      Template.type == (type or "").strip().lower())
    existing = existing.where(Template.domain_id == d.id) if d else existing.where(
        Template.domain_id.is_(None))
    t = s.scalar(existing)
    if t is not None:
        description = t.description if description is None else description
        fields = list(t.fields) if fields is None else fields
        default_tags = list(t.default_tags) if default_tags is None else default_tags
    type_, name, skeleton, fields, default_tags = templates.validate(type, name, skeleton, fields,
                                                                      default_tags)
    payload = {"type": type_, "name": name, "description": (description or "").strip(),
               "skeleton": skeleton, "fields": fields, "default_tags": default_tags}
    if t is not None:
        payload["current_version"] = t.version
    return _file(s, actor, "template.update" if t else "template.create", owner_key, payload,
                 rationale, d.id if d else None, t.id if t else None)


# --- deciding -------------------------------------------------------------------

def can_decide(s: Session, user: User | None, p: Proposal) -> bool:
    if user is None or p.status != "pending":
        return False
    d = s.get(Domain, p.domain_id) if p.domain_id else None
    if p.kind == "domain.create":
        return p.owner_key == f"user:{user.id}" or (p.owner_key == PLATFORM and user.is_admin)
    if p.kind == "domain.update":
        return d is not None and access.can(s, user, d, access.MANAGE)
    return templates.can_manage(s, user, p.owner_key, d)


def can_see(s: Session, user: User | None, p: Proposal) -> bool:
    return user is not None and (p.proposed_by == user.id or can_decide(s, user, p)
                                 or p.owner_key == f"user:{user.id}")


def get(s: Session, user: User, proposal_id: str) -> Proposal:
    p = s.get(Proposal, proposal_id)
    if p is None or not can_see(s, user, p):
        raise access.Denied("proposal not found")
    return p


def to_decide(s: Session, user: User) -> list[Proposal]:
    """Pending proposals this user can approve or reject."""
    keys = [f"user:{user.id}"]
    moderates = s.scalar(select(DomainGrant.id).where(DomainGrant.user_id == user.id,
                                                      DomainGrant.role.in_(("moderator", "admin"))))
    if user.is_admin or moderates:
        keys.append(PLATFORM)
    rows = s.scalars(select(Proposal).where(Proposal.status == "pending",
                                            or_(Proposal.owner_key.in_(keys),
                                                Proposal.domain_id.is_not(None)))
                     .order_by(Proposal.created_at))
    return [p for p in rows if can_decide(s, user, p)]


def mine(s: Session, user: User, status: str | None = None, limit: int = 50) -> list[Proposal]:
    q = select(Proposal).where(Proposal.proposed_by == user.id)
    if status:
        q = q.where(Proposal.status == status)
    return list(s.scalars(q.order_by(Proposal.created_at.desc()).limit(limit)))


def approve(s: Session, actor: Actor, p: Proposal, edits: dict | None = None) -> Proposal:
    """Apply the proposal, optionally with the approver's edits to its payload."""
    if not can_decide(s, actor.user, p):
        raise access.Denied("you can't decide this proposal", 403)
    pl = {**p.payload, **{k: v for k, v in (edits or {}).items() if v is not None}}
    d = s.get(Domain, p.domain_id) if p.domain_id else None
    if p.kind == "domain.create":
        result = domains.create(s, actor, p.owner_key, pl["name"], pl.get("slug"),
                                pl.get("description", ""))
    elif p.kind == "domain.update":
        result = domains.update(s, actor, d, pl.get("name"), pl.get("slug"), pl.get("description"))
    elif p.kind == "template.create":
        result = templates.create(s, actor, p.owner_key, d, type=pl["type"], name=pl["name"],
                                  description=pl.get("description", ""), skeleton=pl["skeleton"],
                                  fields=pl.get("fields"), default_tags=pl.get("default_tags"))
    else:
        t = s.get(Template, p.target_id) if p.target_id else None
        if t is None:
            raise ProposalError("the template this would change no longer exists")
        result = templates.update(s, actor, t, name=pl["name"], description=pl.get("description"),
                                  skeleton=pl["skeleton"], fields=pl.get("fields"),
                                  default_tags=pl.get("default_tags"))
    p.status, p.decided_by, p.decided_at = "approved", actor.user.id, utcnow()
    p.result_id = result.id
    if edits:
        p.decision_note = "approved with changes"
    record(s, actor, "proposal.approve", "proposal", p.id, f"approved {p.kind}: {pl.get('name', '')}",
           p.domain_id)
    return p


def reject(s: Session, actor: Actor, p: Proposal, note: str = "") -> Proposal:
    if not can_decide(s, actor.user, p):
        raise access.Denied("you can't decide this proposal", 403)
    p.status, p.decided_by, p.decided_at = "rejected", actor.user.id, utcnow()
    p.decision_note = (note or "").strip()[:2000]
    record(s, actor, "proposal.reject", "proposal", p.id,
           f"rejected {p.kind}: {p.payload.get('name', '')}", p.domain_id)
    return p


def withdraw(s: Session, actor: Actor, p: Proposal) -> Proposal:
    if p.proposed_by != actor.user.id or p.status != "pending":
        raise access.Denied("only a pending proposal's author can withdraw it", 403)
    p.status = "withdrawn"
    record(s, actor, "proposal.withdraw", "proposal", p.id, f"withdrew {p.kind}", p.domain_id)
    return p


def out(s: Session, p: Proposal, user: User | None = None) -> dict:
    d = s.get(Domain, p.domain_id) if p.domain_id else None
    return {"id": p.id, "kind": p.kind, "status": p.status, "payload": p.payload,
            "rationale": p.rationale, "scope": "public" if p.owner_key == PLATFORM else "personal",
            "domain_id": p.domain_id, "domain": d.name if d else None,
            "by": p.proposer.username if p.proposer else None, "client": p.client,
            "created_at": p.created_at.isoformat(),
            "decided_by": p.decider.username if p.decider else None,
            "decided_at": p.decided_at.isoformat() if p.decided_at else None,
            "decision_note": p.decision_note, "result_id": p.result_id,
            "can_decide": can_decide(s, user, p) if user else False}
