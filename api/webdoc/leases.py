"""Edit leases: who is editing a document right now (design §7).

Adapted from nodewright's leases.py, with one difference: writes don't take
a lease, they only respect one. A lease exists while a person has the editor
open (the tab renews it every few seconds) or after an agent calls
lock_document. Section edits already apply to the latest revision, so a
lease is about not editing under someone, not about merging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import events
from .models import DocumentLease, User, utcnow

UI_TTL = timedelta(seconds=45)             # the editor renews every 15 s
AGENT_TTL = timedelta(minutes=5)
MAX_AGENT_LOCK = timedelta(minutes=15)


@dataclass(frozen=True)
class Holder:
    kind: str           # ui | agent
    user: User
    client: str         # browser tab id, or the agent's client name
    label: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.user.id}:{self.client}"[:160]

    @property
    def ttl(self) -> timedelta:
        return UI_TTL if self.kind == "ui" else AGENT_TTL


class Locked(Exception):
    def __init__(self, lease: DocumentLease):
        super().__init__(describe(lease)["message"])
        self.lease = lease


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def active(s: Session, doc_id: str) -> DocumentLease | None:
    row = s.get(DocumentLease, doc_id)
    if row is not None and _aware(row.expires_at) <= utcnow():
        return None
    return row


def describe(row: DocumentLease | None) -> dict | None:
    if row is None:
        return None
    left = max(0, int((_aware(row.expires_at) - utcnow()).total_seconds()))
    where = "in the editor" if row.kind == "ui" else "(agent lock)"
    return {"holder": row.holder, "kind": row.kind, "user_id": row.user_id, "label": row.label,
            "expires_at": _aware(row.expires_at).isoformat(), "seconds_left": left,
            "message": f"{row.label} is editing this document {where}; free in {left} s "
                       "unless renewed"}


def check(s: Session, doc_id: str, who: Holder) -> None:
    """Raise Locked if someone other than `who` holds a live lease."""
    row = active(s, doc_id)
    if row is not None and row.holder != who.key:
        raise Locked(row)


def take(s: Session, doc_id: str, who: Holder, ttl: timedelta | None = None,
         force: bool = False) -> DocumentLease:
    """Take or renew. Raises Locked if someone else holds a live lease,
    unless `force` (a person taking over from the editor)."""
    expires = utcnow() + min(ttl or who.ttl, MAX_AGENT_LOCK)
    row = s.get(DocumentLease, doc_id)
    live_other = (row is not None and row.holder != who.key
                  and _aware(row.expires_at) > utcnow())
    if live_other and not force:
        raise Locked(row)
    fresh = row is None or row.holder != who.key or _aware(row.expires_at) <= utcnow()
    if row is None:
        row = DocumentLease(document_id=doc_id, holder=who.key, kind=who.kind,
                            user_id=who.user.id, label=who.label[:160], expires_at=expires)
        try:
            with s.begin_nested():
                s.add(row)
        except IntegrityError:
            # Another writer created it between our read and insert.
            s.expire_all()
            raced = active(s, doc_id)
            if raced is not None and raced.holder != who.key and not force:
                raise Locked(raced) from None
            return take(s, doc_id, who, ttl, force)
    else:
        row.holder, row.kind, row.user_id = who.key, who.kind, who.user.id
        row.label, row.expires_at = who.label[:160], expires
    if fresh:
        events.queue(s, doc_id, {"type": "lease", "lease": describe(row)})
    return row


def release(s: Session, doc_id: str, who: Holder | None = None) -> bool:
    """Drop the lease; with `who`, only if they hold it."""
    row = s.get(DocumentLease, doc_id)
    if row is None or (who is not None and row.holder != who.key):
        return False
    s.delete(row)
    events.queue(s, doc_id, {"type": "lease", "lease": None})
    return True
