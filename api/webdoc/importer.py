"""Archive import (design §16 phase 2): a .zip or .tar.gz of markdown into a
domain's page tree.

Two kinds of archive are understood:

- webdoc's own export (export.py): frontmatter carries `webdoc_id`, so a page
  that still exists in this domain is updated (a new revision, only if
  something changed) instead of duplicated. `_attachments/<id>/<name>` files
  and links come back as attachments.
- any folder of markdown (an Obsidian vault, a repo's docs/, dan-wiki's bulk
  import): every .md is a page; a folder `X/` beside `X.md` holds that page's
  children, and a folder without one becomes an empty page titled `X`.
  Relative links to other files in the archive become attachments of the page
  that links them; links to other .md files become links to those pages.

The sync tool (cli/webdoc_sync.py) adds `.webdoc/push.json`: which files
changed locally (only those are written; the rest only anchor the tree),
whether to check each changed file's `revision` against the server's, and
pages to trash. Without it every file counts as changed and nothing is
checked, which is what restoring an export wants.

`plan()` reads and decides without writing; `apply()` writes everything in
the caller's transaction. Nothing from the archive touches the server's disk.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import tarfile
import uuid
import zipfile
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import access, attachments, documents, leases, templates, tree
from .activity import Actor, record
from .models import Attachment, Document, Domain

MAX_ARCHIVE = 100 * 1024 * 1024
MAX_UNPACKED = 500 * 1024 * 1024
MAX_FILES = 5000
MANIFEST = ".webdoc/push.json"
ATTACH_DIR = "_attachments"
RESERVED = {"title", "kind", "domain", "parent", "position", "tags", "type", "created", "updated",
            "webdoc_id", "revision", "project"}
# ](target) in markdown links and images; the target ends at whitespace or ")".
LINK = re.compile(r"(\]\()(<[^>]+>|[^)\s]+)((?:\s+\"[^\"]*\")?\))")
H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)


class ImportError_(ValueError):
    """The archive can't be imported at all (as opposed to a skipped file)."""


@dataclass
class Node:
    key: str                         # archive path without .md, e.g. "Infra/k8s"
    path: str | None                 # the .md file, None for a folder page
    title: str
    body: str = ""
    meta: dict = field(default_factory=dict)
    kind: str = "document"
    tags: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    type: str | None = None
    order: tuple = ()
    parent_key: str | None = None
    changed: bool = True
    doc: Document | None = None      # the existing page it updates
    new_id: str | None = None        # the id a new page will get
    action: str = "create"           # create | update | anchor | conflict | skip
    reason: str = ""


@dataclass
class Plan:
    domain: Domain
    under: Document | None
    nodes: list[Node]
    files: dict[str, bytes]
    uploads: dict[str, dict] = field(default_factory=dict)     # archive path -> attachment info
    trash: list[Document] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- reading the archive -----------------------------------------------------------

def _clean_path(raw: str) -> str | None:
    p = raw.replace("\\", "/").lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    parts = [x for x in p.split("/") if x]
    if not parts or any(x == ".." for x in parts):
        return None
    return "/".join(parts)


def _hidden(path: str) -> bool:
    if path == MANIFEST:
        return False
    return any(x.startswith(".") or x == "__MACOSX" for x in path.split("/"))


def read_archive(data: bytes, warnings: list[str]) -> dict[str, bytes]:
    """Every regular file in a zip or tar(.gz/.bz2/.xz), by cleaned path,
    within the size and count limits."""
    if len(data) > MAX_ARCHIVE:
        raise ImportError_(f"archives are limited to {MAX_ARCHIVE // (1024 * 1024)} MB")
    out: dict[str, bytes] = {}
    total = 0

    def add(name: str, read, size: int) -> None:
        nonlocal total
        path = _clean_path(name)
        if path is None:
            warnings.append(f"skipped {name!r}: not a safe relative path")
            return
        if _hidden(path):
            return
        if len(out) >= MAX_FILES:
            raise ImportError_(f"archives are limited to {MAX_FILES} files")
        total += size
        if total > MAX_UNPACKED:
            raise ImportError_(f"archives are limited to {MAX_UNPACKED // (1024 * 1024)} MB unpacked")
        body = read()
        # Declared sizes can lie; count what was actually read too.
        total += max(0, len(body) - size)
        if total > MAX_UNPACKED:
            raise ImportError_(f"archives are limited to {MAX_UNPACKED // (1024 * 1024)} MB unpacked")
        out[path] = body

    if data[:4] == b"PK\x03\x04" or data[:4] == b"PK\x05\x06":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    add(info.filename, lambda i=info: _read_capped(z.open(i)), info.file_size)
        except zipfile.BadZipFile as exc:
            raise ImportError_(f"not a readable zip: {exc}") from None
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                for m in t:
                    if not m.isreg():
                        continue
                    add(m.name, lambda m=m: _read_capped(t.extractfile(m)), m.size)
        except tarfile.TarError:
            raise ImportError_("not a zip or tar archive") from None
    return out


def _read_capped(f) -> bytes:
    data = f.read(MAX_UNPACKED + 1)
    if len(data) > MAX_UNPACKED:
        raise ImportError_(f"archives are limited to {MAX_UNPACKED // (1024 * 1024)} MB unpacked")
    return data


def _strip_wrapper(files: dict[str, bytes]) -> dict[str, bytes]:
    """Drop one top-level folder that wraps everything (export.py writes
    `<slug>/...`; zipping a folder does the same)."""
    tops = {p.split("/", 1)[0] for p in files if p != MANIFEST}
    if len(tops) != 1:
        return files
    top = next(iter(tops))
    if any(p == top for p in files) or f"{top}.md" in files:
        return files
    if not all(p.startswith(top + "/") or p == MANIFEST for p in files):
        return files
    return {(p if p == MANIFEST else p[len(top) + 1:]): v for p, v in files.items()}


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    after = text[end + 4:]
    if after and not after.startswith("\n"):
        return {}, text
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, after[1:] if after.startswith("\n") else after


# --- planning ------------------------------------------------------------------

def _stem(path: str) -> str:
    return posixpath.basename(path)[:-3] if path.endswith(".md") else posixpath.basename(path)


def _node_from_md(path: str, raw: bytes, warnings: list[str]) -> Node:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        warnings.append(f"{path}: not valid UTF-8; unreadable characters were replaced")
    text = text.replace("\r\n", "\n").lstrip("﻿")
    meta, body = split_frontmatter(text)
    body = body.lstrip("\n")
    title = str(meta.get("title") or "").strip()
    if not title:
        m = H1.search(body)
        title = m.group(1).strip() if m else _stem(path)
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    elif not isinstance(tags, list):
        tags = []
    fields: dict[str, str] = {}
    for k, v in meta.items():
        if k in RESERVED:
            continue
        if isinstance(v, (dict, list)) or not templates.FIELD_RE.match(str(k)):
            warnings.append(f"{path}: frontmatter {k!r} isn't a simple field; left out")
            continue
        if v is not None and str(v).strip():
            fields[str(k)] = str(v).strip()[:500]
    kind = "note" if meta.get("kind") == "note" else "document"
    pos = meta.get("position")
    order = (0, pos, _stem(path).lower()) if isinstance(pos, int) else (1, 0, _stem(path).lower())
    return Node(key=path[:-3], path=path, title=title[:300], body=body, meta=meta, kind=kind,
                tags=[str(t) for t in tags if str(t).strip()][:50], fields=fields,
                type=str(meta["type"]).strip().lower() if meta.get("type") else None, order=order)


def plan(s: Session, user, d: Domain, data: bytes, under: Document | None = None,
         max_bytes: int = documents.DEFAULT_MAX_BYTES) -> Plan:
    warnings: list[str] = []
    files = _strip_wrapper(read_archive(data, warnings))
    manifest: dict = {}
    if MANIFEST in files:
        try:
            manifest = json.loads(files.pop(MANIFEST).decode())
        except (ValueError, UnicodeDecodeError):
            raise ImportError_(f"{MANIFEST} isn't valid JSON") from None
    changed_paths = set(manifest.get("changed", [])) if manifest else None
    check = bool(manifest.get("check_revisions")) if manifest else False

    p = Plan(domain=d, under=under, nodes=[], files=files, warnings=warnings)
    nodes: dict[str, Node] = {}
    for path in sorted(files):
        if path.lower().endswith(".md") and not path.startswith(ATTACH_DIR + "/"):
            nodes[path[:-3]] = _node_from_md(path, files[path], warnings)
    # Folders without their own .md become empty pages.
    for key in list(nodes):
        parts = key.split("/")
        for i in range(1, len(parts)):
            folder = "/".join(parts[:i])
            if folder not in nodes:
                nodes[folder] = Node(key=folder, path=None, title=parts[i - 1][:300],
                                     order=(1, 0, parts[i - 1].lower()))
    for n in nodes.values():
        n.parent_key = n.key.rsplit("/", 1)[0] if "/" in n.key else None
        if changed_paths is not None:
            # A folder with no .md of its own only exists here if it is new.
            n.changed = n.path in changed_paths if n.path else True

    seen: set[str] = set()
    for n in sorted(nodes.values(), key=lambda x: (x.key.count("/"), x.order)):
        wid = str(n.meta.get("webdoc_id") or "")
        doc = s.get(Document, wid) if wid else None
        if wid and wid in seen:
            warnings.append(f"{n.path}: webdoc_id {wid} appears twice; this copy is a new page")
            doc = None
        if doc is not None and (doc.domain_id != d.id or doc.kind == "contribution"):
            doc = None
        if doc is not None and doc.deleted_at is not None:
            warnings.append(f"{n.path}: its page is in the trash; importing it as a new page")
            doc = None
        if doc is not None and not access.can_see(user, doc, access.role_on(s, user, d)):
            doc = None
        if doc is not None:
            seen.add(doc.id)
            n.doc = doc
        if len(n.body.encode()) > max_bytes:
            n.action, n.reason = "skip", f"larger than {max_bytes // 1024} KiB"
        elif doc is None and wid and not n.changed:
            n.action, n.reason = "skip", "its page no longer exists here; pull to catch up"
        elif doc is None:
            n.action = "create"
            n.new_id = uuid.uuid4().hex
        elif not n.changed:
            n.action = "anchor"
        else:
            base = n.meta.get("revision")
            lease = leases.active(s, doc.id)
            if check and isinstance(base, int) and base != doc.revision:
                n.action = "conflict"
                n.reason = (f"changed on the server since revision {base} "
                            f"(now {doc.revision})")
            elif lease is not None:
                n.action, n.reason = "conflict", leases.describe(lease)["message"]
            else:
                n.action = "update"
        if n.type:
            try:
                templates.resolve(s, d, n.type)
            except templates.TemplateError:
                warnings.append(f"{n.path}: no document type {n.type!r} in {d.name}; imported untyped")
                n.type = None
    p.nodes = sorted(nodes.values(), key=lambda x: (x.key.count("/"), x.order))
    for n in p.nodes:
        if n.action == "skip":
            p.skipped.append({"path": n.path, "reason": n.reason})

    for wid in manifest.get("trash", []) if manifest else []:
        doc = s.get(Document, str(wid))
        if doc is None or doc.domain_id != d.id or doc.deleted_at is not None:
            warnings.append(f"page {wid} to trash isn't here (already gone?)")
            continue
        if not access.can(s, user, d, access.DELETE):
            raise access.Denied("you can't delete pages in this domain", 403)
        p.trash.append(doc)

    _plan_links(s, p)
    used = {u["path"] for u in p.uploads.values()} | {n.path for n in p.nodes if n.path}
    for path in sorted(files):
        if path not in used and not path.startswith(ATTACH_DIR + "/"):
            p.skipped.append({"path": path, "reason": "not markdown, and no page links to it"})
    return p


def _target_of(n: Node, raw: str) -> str | None:
    """The archive path a relative link points at, or None for anything
    absolute (a URL, /api/..., #anchor, mailto:)."""
    t = raw[1:-1] if raw.startswith("<") else raw
    if not t or re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) or t.startswith(("/", "#")):
        return None
    t = unquote(t.split("#", 1)[0].split("?", 1)[0])
    base = posixpath.dirname(n.path or n.key)
    joined = posixpath.normpath(posixpath.join(base, t))
    return None if joined.startswith("..") else joined


def _plan_links(s: Session, p: Plan) -> None:
    """Decide what each relative link becomes: an existing attachment, a new
    one, or a link to another imported page."""
    existing = {a.id: a for a in s.scalars(
        select(Attachment).join(Document).where(Document.domain_id == p.domain.id))}
    by_path = {n.path: n for n in p.nodes if n.path}
    for n in p.nodes:
        if not n.path or n.action not in ("create", "update"):
            continue
        for m in LINK.finditer(n.body):
            target = _target_of(n, m.group(2))
            if target is None or target in by_path:
                continue
            parts = target.split("/")
            if len(parts) >= 2 and parts[0] == ATTACH_DIR and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
                if parts[1] in existing:
                    continue
            if target in p.files and target not in p.uploads:
                p.uploads[target] = {"path": target, "page": n, "id": uuid.uuid4().hex,
                                     "name": attachments.clean_name(posixpath.basename(target))}
            elif target not in p.files and target not in p.uploads:
                p.warnings.append(f"{n.path}: link to {target} points at nothing in the archive")


def _rewrite(n: Node, p: Plan, existing: set[str], page_ids: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        target = _target_of(n, m.group(2))
        if target is None:
            return m.group(0)
        if target in page_ids:
            return f"{m.group(1)}/d/{page_ids[target]}{m.group(3)}"
        parts = target.split("/")
        if len(parts) >= 2 and parts[0] == ATTACH_DIR and parts[1] in existing:
            name = parts[2] if len(parts) > 2 else "file"
            return f"{m.group(1)}/api/attachments/{parts[1]}/{quote(name)}{m.group(3)}"
        if target in p.uploads:
            u = p.uploads[target]
            return f"{m.group(1)}/api/attachments/{u['id']}/{quote(u['name'])}{m.group(3)}"
        return m.group(0)
    return LINK.sub(sub, n.body)


def summary(p: Plan, dry_run: bool) -> dict:
    def item(n: Node) -> dict:
        o = {"path": n.path, "title": n.title,
             "id": n.doc.id if n.doc else n.new_id}
        if n.reason:
            o["reason"] = n.reason
        if n.doc is not None:
            o["revision"] = n.doc.revision
        return o
    pick = lambda a: [item(n) for n in p.nodes if n.action == a]  # noqa: E731
    return {"dry_run": dry_run, "created": pick("create"), "updated": pick("update"),
            "unchanged": pick("unchanged"), "conflicts": pick("conflict"),
            "trashed": [{"id": d.id, "title": d.title} for d in p.trash],
            "attachments": [{"path": u["path"], "name": u["name"], "page": u["page"].title}
                            for u in p.uploads.values()],
            "skipped": p.skipped, "warnings": p.warnings}


# --- applying ------------------------------------------------------------------

def apply(s: Session, actor: Actor, p: Plan, store, max_bytes: int,
          uploaded: list[str] | None = None) -> None:
    """Write the plan. Each object key stored is appended to `uploaded` as it
    goes, so a caller whose transaction fails can remove them."""
    uploaded = [] if uploaded is None else uploaded
    d = p.domain
    existing = {a.id for a in s.scalars(
        select(Attachment.id).join(Document).where(Document.domain_id == d.id))}
    page_ids = {n.path: (n.doc.id if n.doc else n.new_id) for n in p.nodes
                if n.path and n.action != "skip"}
    by_key = {n.key: n for n in p.nodes}

    def parent_doc(n: Node) -> Document | None:
        k = n.parent_key
        while k is not None:
            parent = by_key.get(k)
            if parent is not None and parent.action != "skip":
                pid = parent.doc.id if parent.doc else parent.new_id
                return s.get(Document, pid)
            k = k.rsplit("/", 1)[0] if "/" in k else None
        return p.under

    for n in p.nodes:            # parents come before children
        if n.action not in ("create", "update"):
            continue
        body = _rewrite(n, p, existing, page_ids) if n.path else n.body
        parent = parent_doc(n)
        if n.action == "create":
            doc = documents.create(s, actor, d, kind=n.kind, title=n.title, body=body,
                                   tags=n.tags, parent=parent, fields=n.fields,
                                   max_bytes=max_bytes, doc_id=n.new_id)
            if n.type:
                spec = templates.resolve(s, d, n.type)
                doc.template_type, doc.template_version = spec.type, spec.version
            n.doc = doc
        else:
            doc = n.doc
            old = dict(doc.fields or {})
            fields = {**{k: None for k in old if k not in n.fields}, **n.fields}
            changed = documents.write(s, actor, doc, title=n.title, body=body, tags=n.tags,
                                      fields=fields, parent=parent, summary=None,
                                      source="import", max_bytes=max_bytes)
            if n.type and n.type != doc.template_type:
                spec = templates.resolve(s, d, n.type)
                doc.template_type, doc.template_version = spec.type, spec.version
            if not changed:
                n.action = "unchanged"
    s.flush()

    if p.uploads and store is None:
        raise ImportError_("this archive has files to attach, but attachments aren't set up "
                           "on this server")
    for u in p.uploads.values():
        data = p.files[u["path"]]
        page: Node = u["page"]
        a = Attachment(id=u["id"], document_id=page.doc.id, uploader_id=actor.user.id,
                       name=u["name"], size=len(data),
                       content_type=attachments.content_type(data, None))
        s.add(a)
        s.flush()
        store.put(a.key, data, a.content_type)
        uploaded.append(a.key)

    for doc in p.trash:
        documents.trash(s, actor, doc)

    counts = {a: sum(1 for n in p.nodes if n.action == a) for a in ("create", "update")}
    record(s, actor, "domain.import", "domain", d.id,
           f"imported into {d.name}: {counts['create']} new, {counts['update']} updated, "
           f"{len(p.uploads)} files", d.id)
