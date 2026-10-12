"""The activity feed and whats_new (design §9.4).

Events are visible when their domain is readable, so the feed follows read
access like history does (§9.3). Events with no domain (a proposal for the
user's own templates, say) are visible only to whoever made them.

whats_new is for agents: changes since this client last asked, grouped per
thing changed. Nothing calls it on an agent's behalf; it runs when asked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from . import access
from .models import Document, Domain, Event, FeedCursor, User, utcnow

FIRST_LOOK = timedelta(days=7)           # how far back the first whats_new goes
MAX_EVENTS = 500


def _visible(s: Session, user: User | None, domain_id: str | None):
    if domain_id:
        access.domain(s, user, domain_id)
        return Event.domain_id == domain_id
    ids = [d.id for d in access.visible_domains(s, user)]
    cond = Event.domain_id.in_(ids)
    if user is not None:
        cond = or_(cond, and_(Event.domain_id.is_(None), Event.actor_id == user.id))
    return cond


def events(s: Session, user: User | None, *, domain_id: str | None = None,
           since: datetime | None = None, before: datetime | None = None,
           limit: int = 50) -> list[Event]:
    q = select(Event).where(_visible(s, user, domain_id))
    if since is not None:
        q = q.where(Event.at > since)
    if before is not None:
        q = q.where(Event.at < before)
    return list(s.scalars(q.order_by(Event.at.desc()).limit(limit)))


def _names(s: Session, evs: list[Event]) -> tuple[dict, dict]:
    doc_ids = {e.target_id for e in evs if e.target_kind == "document"}
    docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_(doc_ids)))} \
        if doc_ids else {}
    dom_ids = {e.domain_id for e in evs if e.domain_id}
    doms = {d.id: d for d in s.scalars(select(Domain).where(Domain.id.in_(dom_ids)))} \
        if dom_ids else {}
    return docs, doms


def _seen(s: Session, user: User | None, evs: list[Event], docs: dict) -> list[Event]:
    """Leave out events about documents the user can't see now: a contribution
    hidden by a moderator, or a draft that is someone else's."""
    roles: dict[str, str | None] = {}
    out = []
    for e in evs:
        doc = docs.get(e.target_id) if e.target_kind == "document" else None
        if doc is not None and doc.status != "published":
            if doc.domain_id not in roles:
                roles[doc.domain_id] = access.role_on(s, user, doc.domain)
            if not access.can_see(user, doc, roles[doc.domain_id]):
                continue
        out.append(e)
    return out


def event_out(e: Event, docs: dict, doms: dict) -> dict:
    doc = docs.get(e.target_id) if e.target_kind == "document" else None
    d = doms.get(e.domain_id)
    return {"id": e.id, "at": e.at.isoformat(), "by": e.actor_name, "client": e.client,
            "action": e.action, "summary": e.summary, "target_kind": e.target_kind,
            "target_id": e.target_id, "domain_id": e.domain_id, "domain": d.name if d else None,
            "title": doc.title if doc else None,
            "exists": doc is not None and doc.deleted_at is None if e.target_kind == "document"
            else None}


def feed(s: Session, user: User | None, *, domain_id: str | None = None,
         before: datetime | None = None, limit: int = 50) -> dict:
    evs = events(s, user, domain_id=domain_id, before=before, limit=limit + 1)
    more = len(evs) > limit
    docs, doms = _names(s, evs[:limit])
    return {"events": [event_out(e, docs, doms) for e in _seen(s, user, evs[:limit], docs)],
            "more": more}


def whats_new(s: Session, user: User, client: str, *, since: datetime | None = None,
              domain_id: str | None = None, include_own: bool = False) -> dict:
    """Changes grouped per document (or domain, template, proposal), newest
    first. Without `since` it starts where this client's last call left off
    and moves that mark forward; with `since` it leaves the mark alone."""
    now = utcnow()
    cur = s.get(FeedCursor, (user.id, client[:100]))
    start = since or (cur.seen_at if cur else now - FIRST_LOOK)
    if start.tzinfo is None:
        start = start.replace(tzinfo=now.tzinfo)
    q = select(Event).where(_visible(s, user, domain_id), Event.at > start)
    if not include_own:
        # The client's own writes aren't news to it; the same user's other
        # clients (the web UI, another agent) are.
        q = q.where(or_(Event.actor_id != user.id, Event.client != client[:100],
                        Event.actor_id.is_(None)))
    evs = list(s.scalars(q.order_by(Event.at.desc()).limit(MAX_EVENTS + 1)))
    docs, doms = _names(s, evs)
    groups: dict[tuple, dict] = {}
    for e in _seen(s, user, evs[:MAX_EVENTS], docs):
        g = groups.get((e.target_kind, e.target_id))
        if g is None:
            o = event_out(e, docs, doms)
            title = o["title"] or (o["domain"] if e.target_kind == "domain" else None)
            g = groups[(e.target_kind, e.target_id)] = {
                "target_kind": e.target_kind, "target_id": e.target_id, "title": title,
                "domain": o["domain"], "exists": o["exists"], "last_at": o["at"], "changes": 0,
                "by": [], "recent": []}
        g["changes"] += 1
        who = e.actor_name if e.client == "web" else f"{e.actor_name} via {e.client}"
        if who not in g["by"]:
            g["by"].append(who)
        if len(g["recent"]) < 5:
            g["recent"].append(e.summary)
    if since is None:
        if cur is None:
            s.add(FeedCursor(user_id=user.id, client=client[:100], seen_at=now))
        else:
            cur.seen_at = now
    return {"since": start.isoformat(), "until": now.isoformat(),
            "changes": list(groups.values()), "complete": len(evs) <= MAX_EVENTS}
