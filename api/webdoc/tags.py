"""Tag normalisation and near-match hints (design §3).

Tags are open, but an agent introducing a new tag is shown existing tags that
look like the same thing, so sprawl ("k8s", "kubernetes", "Kubernetes ") is
visible at the moment it happens.
"""

from __future__ import annotations

import difflib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, DocumentTag

MAX_TAGS = 20
MAX_LEN = 64


class TagError(ValueError):
    pass


def normalise(tag: str) -> str:
    t = re.sub(r"\s+", "-", (tag or "").strip().lower())
    t = re.sub(r"[^a-z0-9_.+/#-]", "", t).strip("-")
    return t[:MAX_LEN]


def clean(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        t = normalise(raw)
        if t and t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise TagError(f"at most {MAX_TAGS} tags per document")
    return out


def _stem(t: str) -> str:
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


def in_domain(s: Session, domain_id: str) -> set[str]:
    return set(s.scalars(select(DocumentTag.tag).join(Document)
                         .where(Document.domain_id == domain_id,
                                Document.status == "published").distinct()))


def near_matches(new: list[str], existing: set[str]) -> dict[str, list[str]]:
    """For each tag not already in use, existing tags that look like it."""
    hints: dict[str, list[str]] = {}
    for t in new:
        if t in existing:
            continue
        close = {e for e in existing if _stem(e) == _stem(t)}
        close.update(difflib.get_close_matches(t, list(existing), n=3, cutoff=0.8))
        if close:
            hints[t] = sorted(close)
    return hints
