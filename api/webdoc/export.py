"""Markdown export (design §3): a document as a .md file with YAML frontmatter,
a domain as a zip of them with one folder per project, so the result opens as
an Obsidian vault or drops into a repo's docs/."""

from __future__ import annotations

import io
import re
import zipfile

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, Domain


def filename(title: str) -> str:
    return (re.sub(r"[^A-Za-z0-9 _.()-]", "", title).strip()[:120] or "untitled") + ".md"


def markdown(doc: Document) -> str:
    meta = {"title": doc.title, "kind": doc.kind, "domain": doc.domain.slug}
    if doc.project:
        meta["project"] = doc.project.slug
    if doc.tags:
        meta["tags"] = doc.tags
    if doc.template_type:
        meta["type"] = doc.template_type
    meta.update(doc.fields or {})
    meta["created"] = doc.created_at.isoformat()
    meta["updated"] = doc.updated_at.isoformat()
    meta["webdoc_id"] = doc.id
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n{doc.body}"


def domain_zip(s: Session, d: Domain) -> bytes:
    docs = s.scalars(select(Document).where(Document.domain_id == d.id,
                                            Document.deleted_at.is_(None))
                     .order_by(Document.title)).all()
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for doc in docs:
            folder = f"{d.slug}/" + (f"{doc.project.slug}/" if doc.project else "")
            name, n = folder + filename(doc.title), 1
            while name in used:
                n += 1
                name = folder + filename(f"{doc.title} ({n})")
            used.add(name)
            z.writestr(name, markdown(doc))
    return buf.getvalue()
