"""Notes and documents, and their history (design §3, §7, §9.1).

Every change goes through write(), which bumps the revision and stores the
full state, the actor and client, and a summary of what changed. Restoring a
version is a new revision, never a rewrite of history. Deleting moves a
document to the trash; only purge removes it and its history.
"""

from __future__ import annotations

import difflib
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import access, events, sections, tags as tagmod, templates
from .activity import Actor, record
from .models import Document, DocumentRevision, DocumentTag, Domain, Project, User, utcnow

KINDS = ("note", "document")
MAX_TITLE = 300
DEFAULT_MAX_BYTES = 1024 * 1024


class DocError(ValueError):
    pass


class Conflict(Exception):
    def __init__(self, current: int):
        super().__init__(f"the document changed since the revision you started from; it is now at "
                         f"revision {current}")
        self.current = current


def _title(title: str) -> str:
    t = " ".join((title or "").split())
    if not t or len(t) > MAX_TITLE:
        raise DocError(f"title must be 1-{MAX_TITLE} characters")
    return t


def _check_size(body: str, max_bytes: int) -> str:
    body = (body or "").replace("\r\n", "\n")
    if len(body.encode()) > max_bytes:
        raise DocError(f"document is larger than {max_bytes // 1024} KiB")
    return body


def _set_tags(s: Session, doc: Document, tags: list[str]) -> None:
    doc.tag_rows = [DocumentTag(document_id=doc.id, tag=t) for t in tags]


def _snapshot(s: Session, doc: Document, actor: Actor, summary: str,
              base: int | None = None, restored_from: int | None = None,
              source: str | None = None) -> DocumentRevision:
    rev = DocumentRevision(document_id=doc.id, revision=doc.revision, title=doc.title,
                           body=doc.body, tags=doc.tags, fields=dict(doc.fields or {}),
                           domain_id=doc.domain_id, project_id=doc.project_id,
                           actor_id=actor.user.id, client=actor.client[:100],
                           source=source or actor.source, summary=summary[:300],
                           base_revision=base, restored_from=restored_from)
    s.add(rev)
    return rev


def create(s: Session, actor: Actor, d: Domain, *, kind: str, title: str, body: str = "",
           tags: list[str] | None = None, project: Project | None = None,
           spec: "templates.Spec | None" = None, fields: dict | None = None,
           max_bytes: int = DEFAULT_MAX_BYTES) -> Document:
    """With `spec` (a document type), the body defaults to the type's starter
    text, its default tags are added, and its required sections and fields must
    be present (design §6)."""
    if kind not in KINDS:
        raise DocError(f"kind must be one of {', '.join(KINDS)}")
    if project is not None and project.domain_id != d.id:
        raise DocError("that project belongs to another domain")
    clean = templates.clean_fields(spec, fields)
    if spec is not None:
        body = body if (body or "").strip() else templates.starter(spec.skeleton)
        tags = list(dict.fromkeys(list(tags or []) + spec.default_tags))
        gaps = templates.check(spec, body, clean)
        if gaps["missing_sections"] or gaps["missing_fields"]:
            raise DocError(_gap_text(spec, gaps))
    doc = Document(domain_id=d.id, project_id=project.id if project else None, kind=kind,
                   title=_title(title), body=_check_size(body, max_bytes), fields=clean,
                   template_type=spec.type if spec else None,
                   template_version=spec.version if spec else None, revision=1,
                   created_by=actor.user.id)
    s.add(doc)
    s.flush()
    _set_tags(s, doc, tagmod.clean(tags))
    s.flush()
    what = f"{spec.name} document" if spec else kind
    _snapshot(s, doc, actor, f"created {what}")
    record(s, actor, "document.create", "document", doc.id, f"created {what} {doc.title}", d.id)
    return doc


def _gap_text(spec, gaps: dict) -> str:
    parts = []
    if gaps["missing_sections"]:
        parts.append("missing sections: " + "; ".join(gaps["missing_sections"]))
    if gaps["missing_fields"]:
        parts.append("missing fields: " + ", ".join(gaps["missing_fields"]))
    return (f"not a complete {spec.name} document ({'; '.join(parts)}). get_template shows the "
            "sections and fields; leave the body empty to start from the template.")


_UNSET = object()


def write(s: Session, actor: Actor, doc: Document, *, title: str | None = None,
          body: str | None = None, tags: list[str] | None = None, fields: dict | None = None,
          project=_UNSET,
          domain: Domain | None = None, base_revision: int | None = None,
          summary: str | None = None, source: str | None = None,
          restored_from: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Apply changes as one new revision. `project` is a Project or None (no
    project); leave it out to keep the current one. Moving to another domain
    clears the project unless a project of that domain is given. Returns False
    (and writes nothing) when nothing changed."""
    if base_revision is not None and base_revision != doc.revision:
        raise Conflict(doc.revision)
    changed: list[str] = []
    if title is not None and _title(title) != doc.title:
        doc.title = _title(title)
        changed.append("title")
    if body is not None:
        body = _check_size(body, max_bytes)
        if body != doc.body:
            doc.body = body
            changed.append("body")
    if tags is not None:
        new = tagmod.clean(tags)
        if new != doc.tags:
            _set_tags(s, doc, new)
            changed.append("tags")
    if fields is not None:
        # Merge: a field set to "" or null is removed.
        merged = {**(doc.fields or {}), **{k: v for k, v in fields.items()}}
        new_fields = templates.clean_fields(None, {k: v for k, v in merged.items()
                                                   if v not in (None, "")})
        if new_fields != (doc.fields or {}):
            doc.fields = new_fields
            changed.append("fields")
    if domain is not None and domain.id != doc.domain_id:
        doc.domain_id = domain.id
        doc.domain = domain
        changed.append(f"moved to {domain.name}")
        if project is _UNSET:
            project = None
    if project is not _UNSET:
        pid = project.id if project is not None else None
        if project is not None and project.domain_id != doc.domain_id:
            raise DocError("that project belongs to another domain")
        if pid != doc.project_id:
            doc.project_id = pid
            changed.append("project")
    if not changed:
        return False
    doc.revision += 1
    doc.updated_at = utcnow()
    s.flush()
    note = summary or "changed " + ", ".join(changed)
    _snapshot(s, doc, actor, note, base=base_revision, restored_from=restored_from, source=source)
    record(s, actor, "document.update", "document", doc.id, f"{doc.title}: {note}", doc.domain_id)
    events.queue(s, doc.id, {"type": "revision", "revision": doc.revision, "by": actor.label,
                             "user_id": actor.user.id, "client": actor.client, "summary": note})
    return True


def edit(s: Session, actor: Actor, doc: Document, ops: list[dict],
         max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    """Section operations (sections.apply) against the latest revision."""
    body, done = sections.apply(doc.body, ops)
    write(s, actor, doc, body=body, summary="; ".join(done), max_bytes=max_bytes)
    return done


def trash(s: Session, actor: Actor, doc: Document) -> None:
    doc.deleted_at, doc.deleted_by = utcnow(), actor.user.id
    events.queue(s, doc.id, {"type": "trashed", "by": actor.label})
    record(s, actor, "document.delete", "document", doc.id, f"moved {doc.title} to the trash",
           doc.domain_id)


def untrash(s: Session, actor: Actor, doc: Document) -> None:
    if doc.deleted_at is None:
        raise DocError("the document is not in the trash")
    doc.deleted_at, doc.deleted_by = None, None
    events.queue(s, doc.id, {"type": "restored", "by": actor.label})
    record(s, actor, "document.restore", "document", doc.id, f"restored {doc.title} from the trash",
           doc.domain_id)


def purge(s: Session, actor: Actor, doc: Document) -> None:
    if doc.deleted_at is None:
        raise DocError("move the document to the trash before purging it")
    record(s, actor, "document.purge", "document", doc.id, f"permanently deleted {doc.title}",
           doc.domain_id)
    events.queue(s, doc.id, {"type": "purged", "by": actor.label})
    s.delete(doc)


# --- history --------------------------------------------------------------------

def revision(s: Session, doc: Document, number: int) -> DocumentRevision:
    row = s.scalar(select(DocumentRevision).where(DocumentRevision.document_id == doc.id,
                                                  DocumentRevision.revision == number))
    if row is None:
        raise DocError(f"no revision {number}; the latest is {doc.revision}")
    return row


def revisions(s: Session, doc: Document, before: int | None = None, named_only: bool = False,
              limit: int = 50) -> tuple[list[DocumentRevision], bool]:
    q = select(DocumentRevision).where(DocumentRevision.document_id == doc.id)
    if before is not None:
        q = q.where(DocumentRevision.revision < before)
    if named_only:
        q = q.where(DocumentRevision.label.is_not(None))
    rows = list(s.scalars(q.order_by(DocumentRevision.revision.desc()).limit(limit + 1)))
    return rows[:limit], len(rows) > limit


def label(s: Session, actor: Actor, doc: Document, number: int, text: str | None) -> DocumentRevision:
    row = revision(s, doc, number)
    row.label = (text or "").strip()[:200] or None
    record(s, actor, "version.label", "document", doc.id,
           f"named revision {number} of {doc.title}: {row.label}" if row.label
           else f"unnamed revision {number} of {doc.title}", doc.domain_id)
    return row


def restore_version(s: Session, actor: Actor, doc: Document, number: int,
                    max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Bring back an earlier revision's title, body, tags and fields as a new revision."""
    row = revision(s, doc, number)
    old = dict(row.fields or {})
    fields = {k: old.get(k) for k in set(old) | set(doc.fields or {})}
    return write(s, actor, doc, title=row.title, body=row.body, tags=list(row.tags or []),
                 fields=fields,
                 summary=f"restored revision {number}", source="restore", restored_from=number,
                 max_bytes=max_bytes)


def _as_text(title: str, tags: list[str], body: str) -> list[str]:
    head = [f"# {title}"] + ([f"tags: {', '.join(tags)}"] if tags else []) + [""]
    return [ln + "\n" for ln in head] + body.splitlines(keepends=True)


def diff(s: Session, doc: Document, a: int, b: int | None = None, context: int = 3) -> str:
    """Unified diff from revision a to b (default: latest). Title and tags
    appear as a small header so their changes show too."""
    ra = revision(s, doc, a)
    if b is None or b == doc.revision:
        title, tags, body, b = doc.title, doc.tags, doc.body, doc.revision
    else:
        rb = revision(s, doc, b)
        title, tags, body = rb.title, list(rb.tags or []), rb.body
    lines = difflib.unified_diff(_as_text(ra.title, list(ra.tags or []), ra.body),
                                 _as_text(title, tags, body),
                                 fromfile=f"revision {a}", tofile=f"revision {b}", n=context)
    return "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)


def revision_out(r: DocumentRevision) -> dict:
    return {"revision": r.revision, "created_at": r.created_at.isoformat(),
            "by": r.actor.username if r.actor else None, "client": r.client, "source": r.source,
            "summary": r.summary, "label": r.label, "restored_from": r.restored_from}


# --- reading --------------------------------------------------------------------

def out(doc: Document, *, body: bool = True, role: str | None = None) -> dict:
    o = {"id": doc.id, "kind": doc.kind, "title": doc.title, "domain_id": doc.domain_id,
         "domain": doc.domain.name, "project_id": doc.project_id,
         "project": doc.project.name if doc.project else None, "tags": doc.tags,
         "type": doc.template_type, "fields": dict(doc.fields or {}),
         "revision": doc.revision, "created_at": doc.created_at.isoformat(),
         "updated_at": doc.updated_at.isoformat(),
         "deleted_at": doc.deleted_at.isoformat() if doc.deleted_at else None}
    if body:
        o["body"] = doc.body
    if role is not None:
        o["role"] = role
        o["can_edit"] = access.WRITE in access.ROLES.get(role, ())
    return o


def listing(s: Session, user: User | None, *, domain_ids: list[str], project_id: str | None = None,
            kind: str | None = None, tag: str | None = None, q: str | None = None,
            trashed: bool = False, limit: int = 50, offset: int = 0) -> tuple[list[Document], int]:
    """Documents in the given (already authorised) domains, newest first."""
    if not domain_ids:
        return [], 0
    stmt = select(Document).where(Document.domain_id.in_(domain_ids))
    stmt = stmt.where(Document.deleted_at.is_not(None) if trashed else Document.deleted_at.is_(None))
    if project_id:
        stmt = stmt.where(Document.project_id == project_id)
    if kind:
        stmt = stmt.where(Document.kind == kind)
    if tag:
        stmt = stmt.where(Document.id.in_(select(DocumentTag.document_id)
                                          .where(DocumentTag.tag == tagmod.normalise(tag))))
    if q:
        stmt = stmt.where(func.lower(Document.title).contains(q.lower()))
    total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(s.scalars(stmt.order_by(Document.updated_at.desc()).limit(limit).offset(offset)))
    return rows, total


def readable_domain_ids(s: Session, user: User | None, domain_ids: list[str] | None = None) -> list[str]:
    visible = [d.id for d in access.visible_domains(s, user)]
    if domain_ids is None:
        return visible
    out = []
    for did in domain_ids:
        access.domain(s, user, did)            # raises if not readable
        out.append(did)
    return out
