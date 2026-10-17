"""Markdown export (design §3): a document as a .md file with YAML frontmatter,
a domain as a zip of them laid out like its page tree, so the result opens as
an Obsidian vault or drops into a repo's docs/.

A page with child pages exports as `Title.md` next to a `Title/` folder
holding them. Attachments go under `_attachments/<id>/<name>` at the top of
the zip, and links to them in the body are rewritten to relative paths.
"""

from __future__ import annotations

import io
import re
import zipfile
from urllib.parse import quote

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import attachments, tree
from .models import Attachment, Document, Domain

ATTACH_DIR = "_attachments"


def filename(title: str) -> str:
    return stem(title) + ".md"


def stem(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _.()-]", "", title).strip()[:120] or "untitled"


def markdown(doc: Document, body: str | None = None) -> str:
    meta = {"title": doc.title, "kind": doc.kind, "domain": doc.domain.slug}
    parents = tree.ancestors(doc)
    if parents:
        meta["parent"] = tree.path(parents[-1])
    meta["position"] = doc.position
    if doc.tags:
        meta["tags"] = doc.tags
    if doc.template_type:
        meta["type"] = doc.template_type
    meta.update(doc.fields or {})
    meta["created"] = doc.created_at.isoformat()
    meta["updated"] = doc.updated_at.isoformat()
    meta["webdoc_id"] = doc.id
    meta["revision"] = doc.revision
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{doc.body if body is None else body}"


def domain_zip(s: Session, d: Domain, store=None, under: Document | None = None) -> bytes:
    """Published, live pages of the domain, or of one page and everything
    below it (`under`). `store` (storage.Store) adds the attachments; without
    one, links to them are left as they are."""
    docs = s.scalars(select(Document).where(Document.domain_id == d.id,
                                            Document.deleted_at.is_(None),
                                            Document.status == "published")
                     .order_by(Document.position, Document.title)).all()
    if under is not None:
        keep = {under.id, *tree.descendant_ids(s, under)}
        docs = [x for x in docs if x.id in keep]
    by_id = {x.id: x for x in docs}
    kids: dict[str | None, list[Document]] = {}
    for x in docs:
        # A page whose parent isn't exported (a draft, a hidden page) goes to the top.
        kids.setdefault(x.parent_id if x.parent_id in by_id and x.id != getattr(under, "id", None)
                        else None, []).append(x)
    files = {}
    if store is not None and docs:
        files = {a.id: a for a in s.scalars(select(Attachment).where(
            Attachment.document_id.in_(list(by_id))))}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def write_level(level: list[Document], folder: str, depth: int) -> None:
            used: set[str] = set()
            for doc in level:
                name, n = stem(doc.title), 1
                while name.lower() in used:
                    n += 1
                    name = stem(f"{doc.title} ({n})")
                used.add(name.lower())
                up = "../" * (depth - 1)
                body = attachments.LINK_RE.sub(
                    lambda m: (f"{up}{ATTACH_DIR}/{m.group(1)}/{quote(files[m.group(1)].name)}"
                               if m.group(1) in files else m.group(0)), doc.body)
                z.writestr(f"{folder}{name}.md", markdown(doc, body))
                if kids.get(doc.id):
                    write_level(kids[doc.id], f"{folder}{name}/", depth + 1)

        if under is None:
            top = kids.get(None, [])
        else:
            top = [under] if under.id in by_id else []
        write_level(top, f"{d.slug}/", 1)
        for a in files.values():
            try:
                data = b"".join(store.get(a.key))
            except KeyError:
                continue
            z.writestr(f"{d.slug}/{ATTACH_DIR}/{a.id}/{a.name}", data)
    return buf.getvalue()
