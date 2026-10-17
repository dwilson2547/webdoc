"""Document types: templates with per-section guidance (design §6).

A template's skeleton is markdown. Its headings are the document's sections;
an HTML comment under a heading is guidance for that section (for whoever is
writing it, never stored in the document); a heading ending in "(optional)"
is a section that may be left out. Every other heading is required.

Which template a type name means depends on the domain: the domain's own,
then its owner's (the user's for a personal domain, the platform's for a
public one), then the built-ins below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import access, sections, tags as tagmod
from .activity import Actor, record
from .models import PLATFORM, Domain, Template, TemplateRevision, User, utcnow

TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
RESERVED_FIELDS = {"title", "kind", "domain", "project", "tags", "type", "created", "updated",
                   "webdoc_id", "parent", "position", "revision"}
COMMENT = re.compile(r"<!--(.*?)-->", re.S)
OPTIONAL = re.compile(r"\s*\((?i:optional)\)\s*$")
MAX_SKELETON = 50_000
MAX_FIELDS = 20


class TemplateError(ValueError):
    pass


@dataclass
class Spec:
    """A resolved template, built-in or stored."""
    type: str
    name: str
    description: str
    skeleton: str
    fields: list[dict] = field(default_factory=list)
    default_tags: list[str] = field(default_factory=list)
    version: int = 1
    id: str | None = None             # None for built-ins
    source: str = "built-in"          # built-in | personal | public | domain


# --- built-ins ------------------------------------------------------------------

BUILTINS: dict[str, Spec] = {s.type: s for s in [
    Spec("architecture", "Architecture", "How a system is put together and why.", """\
## Overview
<!-- What the system is for and who uses it, in a paragraph. Link related documents. -->

## Components
<!-- The parts, one short subsection or bullet each: responsibility, technology, where it runs. -->

## Data flow
<!-- How requests and data move between the components. A numbered walk-through of the main path. -->

## Decisions
<!-- The choices that shaped the design and the alternatives turned down, with the reason for each. -->

## Risks and open questions (optional)
<!-- What could go wrong, what is not decided yet, and what is unverified. -->
""", [{"name": "status", "description": "draft, accepted or superseded", "required": True}],
         ["architecture"]),
    Spec("decision", "Decision record", "One decision: the context, the choice and its consequences.", """\
## Context
<!-- The situation and the forces at play: what made a decision necessary now. -->

## Decision
<!-- What was decided, stated in one or two sentences. -->

## Alternatives considered
<!-- Each option that was on the table and why it lost. -->

## Consequences
<!-- What becomes easier or harder because of this; follow-up work. -->
""", [{"name": "status", "description": "proposed, accepted, superseded or rejected", "required": True},
      {"name": "decided", "description": "date of the decision (YYYY-MM-DD)", "required": False}],
         ["decision"]),
    Spec("issue-resolution", "Issue resolution", "A problem that was solved: symptom, cause and fix.", """\
## Observed symptom
<!-- What was seen: error messages, metrics, unexpected behaviour. Specific numbers and reproduction steps. -->

## Root cause
<!-- The mechanism, not just the symptom. One ### subsection per distinct contributing cause. -->

## Troubleshooting steps taken
<!-- Numbered, past tense. Each step says what was checked and what it ruled in or out. -->

## Fix
<!-- What was changed and why it resolves the root cause. One ### subsection per change. -->

## Files changed (optional)
<!-- Bullet list of `path — function/section`. -->
""", [{"name": "severity", "description": "Critical, High, Medium or Low, with a short reason", "required": True},
      {"name": "component", "description": "the part of the system responsible", "required": True}],
         ["issue"]),
]}


# --- parsing and checking -------------------------------------------------------

def outline(skeleton: str) -> list[dict]:
    """Sections of a skeleton: heading path (without "(optional)"), whether it
    is required, and its guidance."""
    lines = skeleton.split("\n")
    heads = sections.parse(skeleton)
    out = []
    for i, h in enumerate(heads):
        own_end = heads[i + 1].line if i + 1 < len(heads) else len(lines)
        own = "\n".join(lines[h.line + 1:min(own_end, h.end)])
        guidance = " ".join(" ".join(m.split()) for m in COMMENT.findall(own)).strip()
        out.append({"path": [OPTIONAL.sub("", p) for p in h.path],
                    "required": not OPTIONAL.search(h.text), "guidance": guidance})
    return out


def starter(skeleton: str) -> str:
    """The skeleton as a new document's body: guidance comments and "(optional)"
    markers removed."""
    body = COMMENT.sub("", skeleton)
    lines = [OPTIONAL.sub("", ln) if sections.ATX.match(ln) else ln for ln in body.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in lines)).strip()
    return text + "\n" if text else ""


def check(spec: Spec, body: str, fields: dict) -> dict:
    """What a document of this type is missing: required sections (by heading
    path) and required fields."""
    missing_sections = []
    for sec in outline(spec.skeleton):
        if not sec["required"]:
            continue
        try:
            sections.find(body, sec["path"])
        except sections.SectionError as exc:
            # Two matches still means the section is there.
            if "matches" not in str(exc):
                missing_sections.append(" > ".join(sec["path"]))
    missing_fields = [f["name"] for f in spec.fields
                      if f.get("required") and not str((fields or {}).get(f["name"], "")).strip()]
    return {"missing_sections": missing_sections, "missing_fields": missing_fields}


def clean_fields(spec: Spec | None, given: dict | None) -> dict:
    """Field values for a document: strings, only names the type declares
    (or any valid name for an untyped document)."""
    out: dict[str, str] = {}
    known = {f["name"] for f in spec.fields} if spec else None
    for k, v in (given or {}).items():
        if known is not None and k not in known:
            raise TemplateError(f"{spec.type} has no field {k!r}; its fields are "
                                + (", ".join(sorted(known)) or "none"))
        if not FIELD_RE.match(k) or k in RESERVED_FIELDS:
            raise TemplateError(f"invalid field name {k!r}")
        if v is None or str(v).strip() == "":
            continue
        out[k] = str(v).strip()[:500]
    return out


def validate(type_: str, name: str, skeleton: str, fields: list | None,
             default_tags: list | None) -> tuple[str, str, str, list[dict], list[str]]:
    type_ = (type_ or "").strip().lower()
    if not TYPE_RE.match(type_):
        raise TemplateError("type must be lowercase letters, digits and dashes, e.g. architecture")
    name = " ".join((name or "").split())
    if not name or len(name) > 100:
        raise TemplateError("name must be 1-100 characters")
    skeleton = (skeleton or "").replace("\r\n", "\n")
    if len(skeleton) > MAX_SKELETON:
        raise TemplateError("skeleton is too long")
    if not sections.parse(skeleton):
        raise TemplateError("the skeleton needs at least one heading (## Section)")
    out, seen = [], set()
    for f in fields or []:
        n = str((f or {}).get("name", "")).strip().lower()
        if not FIELD_RE.match(n) or n in RESERVED_FIELDS:
            raise TemplateError(f"invalid field name {n!r} (lowercase, digits, underscores; not "
                                + ", ".join(sorted(RESERVED_FIELDS)) + ")")
        if n in seen:
            raise TemplateError(f"field {n!r} appears twice")
        seen.add(n)
        out.append({"name": n, "description": str(f.get("description") or "").strip()[:300],
                    "required": bool(f.get("required"))})
    if len(out) > MAX_FIELDS:
        raise TemplateError(f"at most {MAX_FIELDS} fields")
    return type_, name, skeleton, out, tagmod.clean(default_tags)


# --- stored templates -----------------------------------------------------------

def _spec(t: Template) -> Spec:
    source = "domain" if t.domain_id else ("public" if t.owner_key == PLATFORM else "personal")
    return Spec(t.type, t.name, t.description, t.skeleton, list(t.fields or []),
                list(t.default_tags or []), t.version, t.id, source)


def available(s: Session, d: Domain) -> list[Spec]:
    """Every type usable in a domain, the most specific definition of each."""
    found: dict[str, Spec] = {}
    for t in s.scalars(select(Template).where(Template.domain_id == d.id)):
        found.setdefault(t.type, _spec(t))
    for t in s.scalars(select(Template).where(Template.owner_key == d.owner_key,
                                              Template.domain_id.is_(None))):
        found.setdefault(t.type, _spec(t))
    for t, spec in BUILTINS.items():
        found.setdefault(t, spec)
    return sorted(found.values(), key=lambda x: x.name.lower())


def resolve(s: Session, d: Domain, type_: str) -> Spec:
    want = (type_ or "").strip().lower()
    for spec in available(s, d):
        if spec.type == want or spec.name.lower() == want:
            return spec
    raise TemplateError(f"no document type {type_!r} in {d.name}; types here: "
                        + ", ".join(x.type for x in available(s, d)))


def can_manage(s: Session, user: User | None, owner_key: str, d: Domain | None) -> bool:
    """Who may create or change templates for an owner (and optionally one of
    its domains): the user for their own, admins for public ones, and a public
    domain's moderators for that domain's."""
    if not access.is_editor(user):
        return False
    if owner_key == f"user:{user.id}":
        return d is None or d.owner_key == owner_key
    if owner_key == PLATFORM:
        if user.is_admin:
            return True
        return d is not None and access.role_on(s, user, d) == "moderator"
    return False


def _snapshot(s: Session, t: Template, actor: Actor) -> None:
    s.add(TemplateRevision(template_id=t.id, version=t.version, name=t.name,
                           description=t.description, skeleton=t.skeleton, fields=list(t.fields),
                           default_tags=list(t.default_tags), actor_id=actor.user.id))


def create(s: Session, actor: Actor, owner_key: str, d: Domain | None, *, type: str, name: str,
           description: str = "", skeleton: str, fields: list | None = None,
           default_tags: list | None = None) -> Template:
    type_, name, skeleton, fields, default_tags = validate(type, name, skeleton, fields, default_tags)
    clash = select(Template.id).where(Template.owner_key == owner_key, Template.type == type_)
    clash = clash.where(Template.domain_id == d.id) if d else clash.where(Template.domain_id.is_(None))
    if s.scalar(clash):
        raise TemplateError(f"there is already a {type_!r} template here; change that one instead")
    t = Template(owner_key=owner_key, domain_id=d.id if d else None, type=type_, name=name,
                 description=(description or "").strip(), skeleton=skeleton, fields=fields,
                 default_tags=default_tags, version=1, created_by=actor.user.id)
    s.add(t)
    s.flush()
    _snapshot(s, t, actor)
    record(s, actor, "template.create", "template", t.id, f"created template {name}",
           d.id if d else None)
    return t


def update(s: Session, actor: Actor, t: Template, *, name: str | None = None,
           description: str | None = None, skeleton: str | None = None,
           fields: list | None = None, default_tags: list | None = None) -> Template:
    type_, name_, skeleton_, fields_, tags_ = validate(
        t.type, name if name is not None else t.name,
        skeleton if skeleton is not None else t.skeleton,
        fields if fields is not None else t.fields,
        default_tags if default_tags is not None else t.default_tags)
    desc = (description if description is not None else t.description).strip()
    if (name_, desc, skeleton_, fields_, tags_) == (t.name, t.description, t.skeleton,
                                                    list(t.fields), list(t.default_tags)):
        return t
    t.name, t.description, t.skeleton, t.fields, t.default_tags = name_, desc, skeleton_, fields_, tags_
    t.version += 1
    t.updated_at = utcnow()
    _snapshot(s, t, actor)
    record(s, actor, "template.update", "template", t.id,
           f"changed template {t.name} (version {t.version})", t.domain_id)
    return t


def delete(s: Session, actor: Actor, t: Template) -> None:
    """Documents made from it keep their type name and fields; a built-in or
    owner template of the same type takes over for new ones."""
    record(s, actor, "template.delete", "template", t.id, f"deleted template {t.name}", t.domain_id)
    s.delete(t)


def managed(s: Session, user: User) -> list[Template]:
    """Templates this user may change: their own, plus public ones for admins
    and the public domains they moderate."""
    rows = list(s.scalars(select(Template).where(Template.owner_key == f"user:{user.id}")))
    for t in s.scalars(select(Template).where(Template.owner_key == PLATFORM)):
        d = s.get(Domain, t.domain_id) if t.domain_id else None
        if can_manage(s, user, PLATFORM, d):
            rows.append(t)
    return sorted(rows, key=lambda t: t.name.lower())


def out(spec: Spec, with_skeleton: bool = True) -> dict:
    o = {"id": spec.id, "type": spec.type, "name": spec.name, "description": spec.description,
         "source": spec.source, "version": spec.version, "fields": spec.fields,
         "default_tags": spec.default_tags, "sections": outline(spec.skeleton)}
    if with_skeleton:
        o["skeleton"] = spec.skeleton
        o["starter"] = starter(spec.skeleton)
    return o


def stored_out(s: Session, t: Template) -> dict:
    o = out(_spec(t))
    d = s.get(Domain, t.domain_id) if t.domain_id else None
    o["domain_id"], o["domain"] = t.domain_id, d.name if d else None
    o["updated_at"] = t.updated_at.isoformat()
    return o


def spec_of(t: Template) -> Spec:
    return _spec(t)
