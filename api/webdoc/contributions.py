"""Public contribution domains: the automated Stack Overflow (design §12).

A public domain that accepts contributions lets any signed-in user add
documents of its accepted types. Everything starts as a draft only its author
sees; a person publishes it from the web UI after reading it (the REST API
takes browser sessions only, and no MCP tool publishes). The secret scan
(scan.py) runs at publish time.

Replies follow the same gate. Confirmations ("worked for me") are the quality
signal; the contribution's author can mark a reply as the resolution.
Moderators (the domain's, and admins) hide, lock and mark duplicates, and work
through reports. Rate limits count rows in a sliding window, so they hold
across restarts and replicas.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import access, documents, events, scan, templates
from .activity import Actor, record
from .models import (Confirmation, Document, DocumentRevision, Domain, Reply, Report, User,
                     utcnow)

REPLY_KINDS = {"solution": "Another fix", "didnt-work": "Didn't work for me",
               "comment": "Comment"}
MAX_REPLY = 20_000
MAX_ENV = 300
MAX_REASON = 2000

# (count, window) per limit; per user, and per client for agent-made drafts.
LIMITS = {
    "draft_user": (20, timedelta(days=1)), "draft_client": (10, timedelta(hours=1)),
    "reply_user": (30, timedelta(days=1)), "reply_client": (15, timedelta(hours=1)),
    "confirm_user": (100, timedelta(days=1)), "report_user": (20, timedelta(days=1)),
}


class ContribError(ValueError):
    pass


class RateLimited(ContribError):
    pass


def _limit(n: int, name: str, what: str) -> None:
    cap, window = LIMITS[name]
    if n >= cap:
        span = "day" if window >= timedelta(days=1) else "hour"
        raise RateLimited(f"limit reached: at most {cap} {what} per {span}"
                          + (" from one app" if name.endswith("_client") else "")
                          + "; try again later")


def _since(name: str):
    return utcnow() - LIMITS[name][1]


# --- domain settings ------------------------------------------------------------

def configure(s: Session, actor: Actor, d: Domain, accepts: bool, types: list[str]) -> Domain:
    """Turn contributions on or off and set the accepted types (moderators)."""
    if d.scope != "public":
        raise ContribError("only public domains take contributions")
    clean = []
    for t in dict.fromkeys((x or "").strip().lower() for x in types or []):
        if t:
            templates.resolve(s, d, t)            # raises TemplateError if unknown
            clean.append(t)
    d.accepts_contributions, d.contribution_types = bool(accepts), clean
    d.updated_at = utcnow()
    record(s, actor, "domain.contributions", "domain", d.id,
           f"{'opened' if accepts else 'closed'} {d.name} to contributions"
           + (f" ({', '.join(clean)})" if accepts and clean else ""), d.id)
    return d


def can_contribute(user: User | None, d: Domain) -> bool:
    return user is not None and d.scope == "public" and d.accepts_contributions


# --- drafting and publishing ----------------------------------------------------

def possible_duplicates(s: Session, emb, d: Domain, title: str, body: str,
                        exclude: str | None = None, limit: int = 5) -> list[dict]:
    """Published contributions in this domain that look like the same thing."""
    from . import search
    q = f"{title}\n{(body or '')[:500]}"
    res = search.search(s, None, emb, q, search.Filters(domain_ids=[d.id], kind="contribution"),
                        limit=limit + 1)
    return [{"id": r["id"], "title": r["title"], "heading": r["heading"], "snippet": r["snippet"],
             "matched": r["matched"]} for r in res["results"] if r["id"] != exclude][:limit]


def draft(s: Session, actor: Actor, d: Domain, *, title: str, body: str = "",
          type: str | None = None, fields: dict | None = None, tags: list[str] | None = None,
          max_bytes: int = documents.DEFAULT_MAX_BYTES) -> Document:
    if not can_contribute(actor.user, d):
        raise ContribError(f"{d.name} doesn't take contributions")
    type = (type or "").strip().lower() or None
    if d.contribution_types and type not in d.contribution_types:
        raise ContribError(f"{d.name} takes contributions of type "
                           + " or ".join(d.contribution_types)
                           + "; get_template shows the sections")
    mine = (select(func.count()).select_from(Document)
            .where(Document.created_by == actor.user.id, Document.kind == "contribution",
                   Document.created_at > _since("draft_user")))
    _limit(s.scalar(mine) or 0, "draft_user", "contributions")
    if actor.client != "web":
        by_client = (select(func.count()).select_from(DocumentRevision)
                     .join(Document, Document.id == DocumentRevision.document_id)
                     .where(DocumentRevision.actor_id == actor.user.id,
                            DocumentRevision.client == actor.client[:100],
                            DocumentRevision.revision == 1, Document.kind == "contribution",
                            DocumentRevision.created_at > _since("draft_client")))
        _limit(s.scalar(by_client) or 0, "draft_client", "contribution drafts")
    spec = templates.resolve(s, d, type) if type else None
    return documents.create(s, actor, d, kind="contribution", title=title, body=body, tags=tags,
                            spec=spec, fields=fields, max_bytes=max_bytes, status="draft")


def scan_text(doc: Document) -> str:
    return "\n".join([doc.title, doc.body or ""] + [str(v) for v in (doc.fields or {}).values()]
                     + list(doc.tags))


def publish(s: Session, actor: Actor, doc: Document, ack_warnings: bool = False) -> dict:
    """Make a draft public: its author only, from the web UI, past the scan."""
    if doc.kind != "contribution" or doc.status != "draft":
        raise ContribError("only a draft contribution can be published")
    if doc.created_by != actor.user.id:
        raise access.Denied("only its author publishes a draft", 403)
    if not can_contribute(actor.user, doc.domain):
        raise ContribError(f"{doc.domain.name} doesn't take contributions any more")
    result = scan.check(scan_text(doc), ack_warnings)
    doc.status, doc.published_at = "published", utcnow()
    record(s, actor, "contribution.publish", "document", doc.id, f"published {doc.title}",
           doc.domain_id)
    events.queue(s, doc.id, {"type": "published", "by": actor.label})
    return result


def check_edit(doc: Document, body: str | None, title: str | None, ack_warnings: bool) -> None:
    """Edits to a published contribution go public at once, so they pass the
    scan too (drafts are scanned when published)."""
    if doc.kind != "contribution" or doc.status != "published":
        return
    text = "\n".join(x for x in (title, body) if x is not None)
    if text:
        scan.check(text, ack_warnings)


# --- replies --------------------------------------------------------------------

def _open_for_discussion(doc: Document) -> None:
    if doc.kind != "contribution" or doc.status != "published" or doc.deleted_at is not None:
        raise ContribError("replies and confirmations are for published contributions")
    if doc.locked or doc.merged_into_id:
        raise ContribError("a moderator locked this contribution"
                           + ("; it duplicates another" if doc.merged_into_id else ""))


def add_reply(s: Session, actor: Actor, doc: Document, kind: str, body: str,
              publish_now: bool = False, ack_warnings: bool = False) -> Reply:
    """A reply; agents draft (publish_now is for the web UI, past the scan)."""
    _open_for_discussion(doc)
    if kind not in REPLY_KINDS:
        raise ContribError("kind must be one of " + ", ".join(REPLY_KINDS))
    body = (body or "").replace("\r\n", "\n").strip()
    if not body or len(body) > MAX_REPLY:
        raise ContribError(f"a reply needs 1-{MAX_REPLY} characters")
    n = s.scalar(select(func.count()).select_from(Reply).where(
        Reply.author_id == actor.user.id, Reply.created_at > _since("reply_user"))) or 0
    _limit(n, "reply_user", "replies")
    if actor.client != "web":
        n = s.scalar(select(func.count()).select_from(Reply).where(
            Reply.author_id == actor.user.id, Reply.client == actor.client[:100],
            Reply.created_at > _since("reply_client"))) or 0
        _limit(n, "reply_client", "replies")
    if publish_now:
        scan.check(body, ack_warnings)
    r = Reply(document_id=doc.id, author_id=actor.user.id, client=actor.client[:100], kind=kind,
              body=body, status="published" if publish_now else "draft",
              published_at=utcnow() if publish_now else None)
    s.add(r)
    s.flush()
    if publish_now:
        _reply_published(s, actor, doc, r)
    else:
        record(s, actor, "reply.draft", "document", doc.id, f"drafted a reply to {doc.title}", None)
    return r


def _reply_published(s: Session, actor: Actor, doc: Document, r: Reply) -> None:
    record(s, actor, "reply.publish", "document", doc.id,
           f"replied to {doc.title} ({REPLY_KINDS[r.kind].lower()})", doc.domain_id)
    events.queue(s, doc.id, {"type": "reply", "by": actor.label})


def reply_for(s: Session, user: User, reply_id: str) -> tuple[Reply, Document]:
    r = s.get(Reply, reply_id)
    doc = s.get(Document, r.document_id) if r else None
    if r is None or doc is None:
        raise access.Denied("reply not found")
    access.document(s, user, doc.id)
    if r.status == "draft" and r.author_id != user.id:
        raise access.Denied("reply not found")
    return r, doc


def publish_reply(s: Session, actor: Actor, r: Reply, doc: Document,
                  ack_warnings: bool = False) -> dict:
    if r.status != "draft":
        raise ContribError("only a draft reply can be published")
    if r.author_id != actor.user.id:
        raise access.Denied("only its author publishes a draft", 403)
    _open_for_discussion(doc)
    result = scan.check(r.body, ack_warnings)
    r.status, r.published_at = "published", utcnow()
    _reply_published(s, actor, doc, r)
    return result


def edit_reply_draft(r: Reply, actor: Actor, kind: str | None, body: str | None) -> Reply:
    if r.status != "draft" or r.author_id != actor.user.id:
        raise ContribError("only your own draft replies can be edited; published ones stay as "
                           "they were posted")
    if kind is not None:
        if kind not in REPLY_KINDS:
            raise ContribError("kind must be one of " + ", ".join(REPLY_KINDS))
        r.kind = kind
    if body is not None:
        body = body.replace("\r\n", "\n").strip()
        if not body or len(body) > MAX_REPLY:
            raise ContribError(f"a reply needs 1-{MAX_REPLY} characters")
        r.body = body
    return r


def withdraw_reply(s: Session, actor: Actor, r: Reply, doc: Document) -> None:
    """A draft is deleted; a published reply is withdrawn (kept, not shown)."""
    if r.author_id != actor.user.id:
        raise access.Denied("only its author withdraws a reply", 403)
    if r.status == "draft":
        s.delete(r)
        return
    if r.status != "published":
        raise ContribError("this reply isn't published")
    r.status = "withdrawn"
    if doc.accepted_reply_id == r.id:
        doc.accepted_reply_id = None
    record(s, actor, "reply.withdraw", "document", doc.id, f"withdrew a reply to {doc.title}",
           doc.domain_id)


def accept(s: Session, actor: Actor, doc: Document, r: Reply | None) -> None:
    """The contribution's author (or a moderator) marks the reply that resolved it."""
    role = access.role_on(s, actor.user, doc.domain)
    if doc.created_by != actor.user.id and access.MODERATE not in access.ROLES.get(role or "", ()):
        raise access.Denied("only the contribution's author marks the resolution", 403)
    if r is not None and (r.document_id != doc.id or r.status != "published"):
        raise ContribError("that reply isn't a published reply to this contribution")
    doc.accepted_reply_id = r.id if r else None
    record(s, actor, "reply.accept", "document", doc.id,
           f"marked a reply as the resolution of {doc.title}" if r
           else f"unmarked the resolution of {doc.title}", doc.domain_id)


# --- confirmations --------------------------------------------------------------

def confirm(s: Session, actor: Actor, doc: Document, r: Reply | None = None,
            environment: str = "") -> Confirmation:
    _open_for_discussion(doc)
    if r is not None and (r.document_id != doc.id or r.status != "published"):
        raise ContribError("that reply isn't a published reply to this contribution")
    author = r.author_id if r is not None else doc.created_by
    if author == actor.user.id:
        raise ContribError("you can't confirm your own fix")
    environment = " ".join((environment or "").split())[:MAX_ENV]
    if environment and not scan.clean(scan.scan(environment)):
        raise ContribError("keep the environment generic (OS, versions, hardware); it looks like "
                           "it names private hosts, paths, addresses or secrets")
    n = s.scalar(select(func.count()).select_from(Confirmation).where(
        Confirmation.user_id == actor.user.id,
        Confirmation.created_at > _since("confirm_user"))) or 0
    _limit(n, "confirm_user", "confirmations")
    target = r.id if r else ""
    row = s.scalar(select(Confirmation).where(Confirmation.document_id == doc.id,
                                              Confirmation.user_id == actor.user.id,
                                              Confirmation.target == target))
    if row is None:
        row = Confirmation(document_id=doc.id, reply_id=r.id if r else None, target=target,
                           user_id=actor.user.id, client=actor.client[:100])
        s.add(row)
        record(s, actor, "contribution.confirm", "document", doc.id,
               f"confirmed {'a reply to ' if r else ''}{doc.title} worked", doc.domain_id)
    row.environment = environment
    s.flush()
    events.queue(s, doc.id, {"type": "confirmation", "by": actor.label})
    return row


def unconfirm(s: Session, actor: Actor, doc: Document, reply_id: str | None = None) -> bool:
    row = s.scalar(select(Confirmation).where(Confirmation.document_id == doc.id,
                                              Confirmation.user_id == actor.user.id,
                                              Confirmation.target == (reply_id or "")))
    if row is None:
        return False
    s.delete(row)
    return True


# --- moderation -----------------------------------------------------------------

def _moderator(s: Session, user: User, doc: Document) -> None:
    if not access.can(s, user, doc.domain, access.MODERATE):
        raise access.Denied("only this domain's moderators and admins do that", 403)


def hide(s: Session, actor: Actor, doc: Document, hidden: bool, reason: str = "") -> None:
    _moderator(s, actor.user, doc)
    if doc.status == "draft":
        raise ContribError("drafts aren't public; there's nothing to hide")
    doc.status = "hidden" if hidden else "published"
    doc.hidden_reason = (reason or "").strip()[:MAX_REASON] if hidden else ""
    record(s, actor, "moderation.hide" if hidden else "moderation.unhide", "document", doc.id,
           f"{'hid' if hidden else 'unhid'} {doc.title}"
           + (f": {doc.hidden_reason}" if hidden and doc.hidden_reason else ""), doc.domain_id)
    events.queue(s, doc.id, {"type": "moderation", "by": actor.label})


def hide_reply(s: Session, actor: Actor, r: Reply, doc: Document, hidden: bool,
               reason: str = "") -> None:
    _moderator(s, actor.user, doc)
    if r.status not in ("published", "hidden"):
        raise ContribError("only published replies can be hidden")
    r.status = "hidden" if hidden else "published"
    r.hidden_reason = (reason or "").strip()[:MAX_REASON] if hidden else ""
    if hidden and doc.accepted_reply_id == r.id:
        doc.accepted_reply_id = None
    record(s, actor, "moderation.hide_reply" if hidden else "moderation.unhide_reply", "document",
           doc.id, f"{'hid' if hidden else 'unhid'} a reply to {doc.title}", doc.domain_id)


def lock(s: Session, actor: Actor, doc: Document, locked: bool) -> None:
    _moderator(s, actor.user, doc)
    doc.locked = locked
    record(s, actor, "moderation.lock" if locked else "moderation.unlock", "document", doc.id,
           f"{'locked' if locked else 'unlocked'} {doc.title}", doc.domain_id)
    events.queue(s, doc.id, {"type": "moderation", "by": actor.label})


def mark_duplicate(s: Session, actor: Actor, doc: Document, of: Document | None) -> None:
    _moderator(s, actor.user, doc)
    if of is not None:
        if of.id == doc.id or of.domain_id != doc.domain_id or of.status != "published" \
                or of.kind != "contribution" or of.merged_into_id:
            raise ContribError("a duplicate points at another published contribution in the "
                               "same domain")
    doc.merged_into_id = of.id if of else None
    record(s, actor, "moderation.duplicate", "document", doc.id,
           f"marked {doc.title} as a duplicate of {of.title}" if of
           else f"unmarked {doc.title} as a duplicate", doc.domain_id)
    events.queue(s, doc.id, {"type": "moderation", "by": actor.label})


# --- reports --------------------------------------------------------------------

def report(s: Session, actor: Actor, doc: Document, reason: str, r: Reply | None = None) -> Report:
    if doc.domain.scope != "public" or doc.status == "draft":
        raise ContribError("reports are for published documents in public domains")
    reason = (reason or "").strip()
    if not reason:
        raise ContribError("say what's wrong; moderators read it")
    n = s.scalar(select(func.count()).select_from(Report).where(
        Report.reporter_id == actor.user.id, Report.created_at > _since("report_user"))) or 0
    _limit(n, "report_user", "reports")
    row = Report(domain_id=doc.domain_id, document_id=doc.id, reply_id=r.id if r else None,
                 reporter_id=actor.user.id, reason=reason[:MAX_REASON])
    s.add(row)
    s.flush()
    # Reports are between the reporter and the moderators: no public event.
    return row


def open_reports(s: Session, user: User) -> list[Report]:
    ids = access.moderated_domain_ids(s, user)
    if not ids:
        return []
    return list(s.scalars(select(Report).where(Report.status == "open",
                                               Report.domain_id.in_(ids))
                          .order_by(Report.created_at)))


def resolve_report(s: Session, actor: Actor, rep: Report, action: str, note: str = "") -> Report:
    """dismiss, or hide what was reported (the contribution or the reply)."""
    doc = s.get(Document, rep.document_id)
    _moderator(s, actor.user, doc)
    if rep.status != "open":
        raise ContribError("this report was already handled")
    if action == "hide":
        if rep.reply_id:
            hide_reply(s, actor, s.get(Reply, rep.reply_id), doc, True, note or rep.reason)
        else:
            hide(s, actor, doc, True, note or rep.reason)
        rep.status = "actioned"
    elif action == "dismiss":
        rep.status = "dismissed"
    else:
        raise ContribError("action must be dismiss or hide")
    rep.decided_by, rep.decided_at, rep.note = actor.user.id, utcnow(), (note or "")[:MAX_REASON]
    return rep


def report_out(s: Session, rep: Report) -> dict:
    doc = s.get(Document, rep.document_id)
    r = s.get(Reply, rep.reply_id) if rep.reply_id else None
    return {"id": rep.id, "document_id": rep.document_id, "title": doc.title if doc else None,
            "domain_id": rep.domain_id, "domain": doc.domain.name if doc else None,
            "reply_id": rep.reply_id, "reply_excerpt": r.body[:300] if r else None,
            "reason": rep.reason, "by": rep.reporter.username if rep.reporter else None,
            "status": rep.status, "created_at": rep.created_at.isoformat()}


# --- reading --------------------------------------------------------------------

def reply_out(r: Reply, confirms: list[Confirmation], user: User | None, moderator: bool) -> dict:
    o = {"id": r.id, "kind": r.kind, "kind_text": REPLY_KINDS.get(r.kind, r.kind),
         "status": r.status, "by": r.author.username if r.author else None, "client": r.client,
         "created_at": r.created_at.isoformat(),
         "published_at": r.published_at.isoformat() if r.published_at else None,
         "confirmations": len(confirms),
         "confirmed_by_me": any(c.user_id == (user.id if user else None) for c in confirms),
         "mine": user is not None and r.author_id == user.id}
    if r.status in ("published", "draft") or moderator:
        o["body"] = r.body
    if r.status == "hidden":
        o["hidden_reason"] = r.hidden_reason
    return o


def thread(s: Session, user: User | None, doc: Document) -> dict:
    """Replies, confirmations and what the viewer may do, for a contribution."""
    role = access.role_on(s, user, doc.domain)
    moderator = access.MODERATE in access.ROLES.get(role or "", ())
    confirms = list(s.scalars(select(Confirmation).where(Confirmation.document_id == doc.id)
                              .order_by(Confirmation.created_at)))
    by_target: dict[str, list[Confirmation]] = {}
    for c in confirms:
        by_target.setdefault(c.target, []).append(c)
    replies = []
    for r in s.scalars(select(Reply).where(Reply.document_id == doc.id)
                       .order_by(Reply.created_at)):
        visible = (r.status == "published" or moderator
                   or (user is not None and r.author_id == user.id and r.status != "withdrawn"))
        if visible:
            replies.append(reply_out(r, by_target.get(r.id, []), user, moderator))
    merged = s.get(Document, doc.merged_into_id) if doc.merged_into_id else None
    signed_in = user is not None
    open_ = doc.status == "published" and not doc.locked and not doc.merged_into_id
    own = [c for c in by_target.get("", [])]
    return {
        "status": doc.status, "locked": doc.locked, "hidden_reason": doc.hidden_reason or None,
        "published_at": doc.published_at.isoformat() if doc.published_at else None,
        "author": doc.author.username if doc.author else None,
        "merged_into": {"id": merged.id, "title": merged.title} if merged else None,
        "accepted_reply_id": doc.accepted_reply_id,
        "confirmations": [{"by": c.user.username, "environment": c.environment,
                           "at": c.created_at.isoformat()} for c in own],
        "confirmed_by_me": any(c.user_id == (user.id if user else None) for c in own),
        "replies": replies,
        "can": {"reply": signed_in and open_, "confirm": signed_in and open_
                and doc.created_by != (user.id if user else None),
                "publish": doc.status == "draft" and signed_in and doc.created_by == user.id,
                "accept": signed_in and (doc.created_by == user.id or moderator),
                "moderate": moderator, "report": signed_in and doc.status == "published"},
    }


def drafts(s: Session, user: User) -> dict:
    docs = s.scalars(select(Document).where(Document.created_by == user.id,
                                            Document.status == "draft",
                                            Document.deleted_at.is_(None))
                     .order_by(Document.updated_at.desc()))
    reps = s.scalars(select(Reply).where(Reply.author_id == user.id, Reply.status == "draft")
                     .order_by(Reply.created_at.desc()))
    return {
        "contributions": [{"id": d.id, "title": d.title, "domain_id": d.domain_id,
                           "domain": d.domain.name, "type": d.template_type,
                           "updated_at": d.updated_at.isoformat(),
                           "by_client": _first_client(s, d.id)} for d in docs],
        "replies": [{"id": r.id, "document_id": r.document_id,
                     "title": s.get(Document, r.document_id).title, "kind": r.kind,
                     "excerpt": r.body[:200], "client": r.client,
                     "created_at": r.created_at.isoformat()} for r in reps],
    }


def _first_client(s: Session, doc_id: str) -> str:
    return s.scalar(select(DocumentRevision.client).where(
        DocumentRevision.document_id == doc_id, DocumentRevision.revision == 1)) or "web"


def draft_count(s: Session, user: User) -> int:
    a = s.scalar(select(func.count()).select_from(Document).where(
        Document.created_by == user.id, Document.status == "draft",
        Document.deleted_at.is_(None))) or 0
    b = s.scalar(select(func.count()).select_from(Reply).where(
        Reply.author_id == user.id, Reply.status == "draft")) or 0
    return a + b
