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

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import access, documents, events, scan, templates
from .activity import Actor, record
from .models import (Confirmation, Document, DocumentRevision, Domain, Event, Reply, Report,
                     User, utcnow)

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


VIEWER = "viewers can read and report; posting, replying and confirming need editor access"


def can_contribute(user: User | None, d: Domain) -> bool:
    return access.is_editor(user) and d.scope == "public" and d.accepts_contributions


# --- drafting and publishing ----------------------------------------------------

TITLE_OVERLAP = 0.5


def possible_duplicates(s: Session, emb, d: Domain, title: str, body: str,
                        exclude: str | None = None, limit: int = 5) -> list[dict]:
    """Published contributions in this domain that look like the same thing:
    search hits, plus titles sharing most of their words with this one (which
    catches posts the indexer hasn't reached yet)."""
    from . import search
    q = f"{title}\n{(body or '')[:500]}"
    res = search.search(s, None, emb, q, search.Filters(domain_ids=[d.id], kind="contribution"),
                        limit=limit + 1)
    out = [{"id": r["id"], "title": r["title"], "heading": r["heading"], "snippet": r["snippet"],
            "matched": r["matched"]} for r in res["results"] if r["id"] != exclude]
    words = set(search._terms(title))
    if words:
        seen = {o["id"] for o in out}
        recent = s.scalars(select(Document).where(
            Document.domain_id == d.id, Document.kind == "contribution",
            Document.status == "published", Document.deleted_at.is_(None),
            Document.merged_into_id.is_(None)).order_by(Document.published_at.desc()).limit(500))
        for doc in recent:
            other = set(search._terms(doc.title))
            if doc.id in seen or doc.id == exclude or not other:
                continue
            if len(words & other) / min(len(words), len(other)) >= TITLE_OVERLAP:
                out.append({"id": doc.id, "title": doc.title, "heading": "", "snippet": "",
                            "matched": ["title words"]})
    return out[:limit]


def check_draft(d: Domain, user: User, type: str | None) -> str | None:
    """Whether this user may draft here with this type; returns the type."""
    if not access.is_editor(user):
        raise ContribError(VIEWER)
    if not can_contribute(user, d):
        raise ContribError(f"{d.name} doesn't take contributions")
    type = (type or "").strip().lower() or None
    if d.contribution_types and type not in d.contribution_types:
        raise ContribError(f"{d.name} takes contributions of type "
                           + " or ".join(d.contribution_types)
                           + "; get_template shows the sections")
    return type


def draft(s: Session, actor: Actor, d: Domain, *, title: str, body: str = "",
          type: str | None = None, fields: dict | None = None, tags: list[str] | None = None,
          max_bytes: int = documents.DEFAULT_MAX_BYTES) -> Document:
    type = check_draft(d, actor.user, type)
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

def _participant(user: User) -> None:
    if not access.is_editor(user):
        raise ContribError(VIEWER)


def _open_for_discussion(doc: Document) -> None:
    if doc.kind != "contribution" or doc.status != "published" or doc.deleted_at is not None:
        raise ContribError("replies and confirmations are for published contributions")
    if doc.locked or doc.merged_into_id:
        raise ContribError("a moderator locked this contribution"
                           + ("; it duplicates another" if doc.merged_into_id else ""))


def add_reply(s: Session, actor: Actor, doc: Document, kind: str, body: str,
              publish_now: bool = False, ack_warnings: bool = False) -> Reply:
    """A reply; agents draft (publish_now is for the web UI, past the scan)."""
    _participant(actor.user)
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
    _participant(actor.user)
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
    _participant(actor.user)
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
    _set_hidden(s, actor, doc, hidden, reason)


def _set_hidden(s: Session, actor: Actor, doc: Document, hidden: bool, reason: str) -> None:
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
    """Anyone who can read a published document can report it. Reports in a
    public domain go to its moderators and admins; reports on a personal
    domain's document go to admins, who may then open that one document
    (design §4), and to nobody in the domain."""
    if doc.status == "draft":
        raise ContribError("drafts aren't public; there's nothing to report")
    if doc.domain.scope != "public" and (doc.domain.owner_user_id == actor.user.id
                                         or doc.created_by == actor.user.id):
        raise ContribError("this is your own document")
    reason = (reason or "").strip()
    if not reason:
        raise ContribError("say what's wrong; moderators read it")
    n = s.scalar(select(func.count()).select_from(Report).where(
        Report.reporter_id == actor.user.id, Report.created_at > _since("report_user"))) or 0
    _limit(n, "report_user", "reports")
    row = Report(domain_id=doc.domain_id, document_id=doc.id, reply_id=r.id if r else None,
                 reporter_id=actor.user.id, reason=reason[:MAX_REASON], revision=doc.revision)
    s.add(row)
    s.flush()
    # Reports are between the reporter and the moderators: no public event.
    return row


def _reports_for(s: Session, user: User):
    """Reports this user handles: their moderated public domains', and as an
    admin, those on personal documents too."""
    ids = access.moderated_domain_ids(s, user)
    cond = Report.domain_id.in_(ids) if ids else None
    if user.is_admin:
        personal = Report.domain_id.in_(select(Domain.id).where(Domain.scope != "public"))
        cond = personal if cond is None else or_(cond, personal)
    return cond


def handles_reports(s: Session, user: User) -> bool:
    return user.is_admin or bool(access.moderated_domain_ids(s, user))


def open_reports(s: Session, user: User) -> list[Report]:
    cond = _reports_for(s, user)
    if cond is None:
        return []
    return list(s.scalars(select(Report).where(Report.status == "open", cond)
                          .order_by(Report.created_at)))


def recent_decisions(s: Session, user: User, limit: int = 30) -> list[Report]:
    cond = _reports_for(s, user)
    if cond is None:
        return []
    return list(s.scalars(select(Report).where(Report.status != "open", cond)
                          .order_by(Report.decided_at.desc()).limit(limit)))


def report_for(s: Session, user: User, report_id: str) -> Report:
    rep = s.get(Report, report_id)
    cond = _reports_for(s, user)
    if rep is None or cond is None or not s.scalar(
            select(func.count()).select_from(Report).where(Report.id == rep.id, cond)):
        raise access.Denied("report not found")
    return rep


def _may_handle(s: Session, user: User, doc: Document) -> None:
    if doc.domain.scope == "public":
        _moderator(s, user, doc)
    elif not user.is_admin:
        raise access.Denied("only admins handle reports on personal documents", 403)


def review(s: Session, actor: Actor, rep: Report) -> dict:
    """The reported document, for whoever handles the report, while it is
    open. For a personal document this is an admin's only way in, and the
    owner sees in their activity that it happened."""
    doc = s.get(Document, rep.document_id)
    _may_handle(s, actor.user, doc)
    if rep.status != "open":
        raise ContribError("this report was handled; the document is no longer open for review")
    out = {"report": report_out(s, rep), "title": doc.title, "body": doc.body,
           "fields": dict(doc.fields or {}), "tags": doc.tags, "kind": doc.kind,
           "status": doc.status, "revision": doc.revision, "domain": doc.domain.name,
           "scope": doc.domain.scope,
           "owner": doc.domain.owner.username if doc.domain.owner else None,
           "updated_at": doc.updated_at.isoformat(), "reported": None}
    if rep.revision and rep.revision != doc.revision:
        old = s.scalar(select(DocumentRevision).where(DocumentRevision.document_id == doc.id,
                                                      DocumentRevision.revision == rep.revision))
        if old is not None:
            out["reported"] = {"revision": old.revision, "title": old.title, "body": old.body}
    if doc.domain.scope != "public":
        seen = s.scalar(select(Event.id).where(Event.action == "report.review",
                                               Event.actor_id == actor.user.id,
                                               Event.target_id == doc.id,
                                               Event.at >= rep.created_at))
        if seen is None:
            record(s, actor, "report.review", "document", doc.id,
                   f"an admin ({actor.user.username}) opened {doc.title} to review a report",
                   doc.domain_id)
    return out


def resolve_report(s: Session, actor: Actor, rep: Report, action: str, note: str = "") -> Report:
    """dismiss, or hide what was reported (the contribution or the reply)."""
    doc = s.get(Document, rep.document_id)
    _may_handle(s, actor.user, doc)
    if rep.status != "open":
        raise ContribError("this report was already handled")
    if action == "hide":
        if rep.reply_id:
            hide_reply(s, actor, s.get(Reply, rep.reply_id), doc, True, note or rep.reason)
        else:
            _set_hidden(s, actor, doc, True, note or rep.reason)
        rep.status = "actioned"
    elif action == "dismiss":
        rep.status = "dismissed"
    else:
        raise ContribError("action must be dismiss or hide")
    rep.decided_by, rep.decided_at, rep.note = actor.user.id, utcnow(), (note or "")[:MAX_REASON]
    return rep


def undo_report(s: Session, actor: Actor, rep: Report) -> Report:
    """Reverse a hide made from a report (the report stays, as dismissed)."""
    doc = s.get(Document, rep.document_id)
    _may_handle(s, actor.user, doc)
    if rep.status != "actioned":
        raise ContribError("only a report that hid something can be undone")
    if rep.reply_id:
        r = s.get(Reply, rep.reply_id)
        if r is not None and r.status == "hidden":
            hide_reply(s, actor, r, doc, False)
    elif doc.status == "hidden":
        _set_hidden(s, actor, doc, False, "")
    rep.status, rep.decided_by, rep.decided_at = "dismissed", actor.user.id, utcnow()
    rep.note = ((rep.note + "; ") if rep.note else "") + "hide undone"
    return rep


def report_out(s: Session, rep: Report) -> dict:
    doc = s.get(Document, rep.document_id)
    r = s.get(Reply, rep.reply_id) if rep.reply_id else None
    return {"id": rep.id, "document_id": rep.document_id, "title": doc.title if doc else None,
            "domain_id": rep.domain_id, "domain": doc.domain.name if doc else None,
            "reply_id": rep.reply_id, "reply_excerpt": r.body[:300] if r else None,
            "reason": rep.reason, "by": rep.reporter.username if rep.reporter else None,
            "status": rep.status, "created_at": rep.created_at.isoformat(),
            "scope": doc.domain.scope if doc else None, "note": rep.note,
            "decided_at": rep.decided_at.isoformat() if rep.decided_at else None}


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
    signed_in = access.is_editor(user)
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
                "moderate": moderator,
                "report": user is not None and doc.status == "published"},
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
