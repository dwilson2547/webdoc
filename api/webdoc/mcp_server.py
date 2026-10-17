"""The MCP server: what an agent sees (design §10).

Streamable HTTP at /mcp, stateless, JSON responses. Every request carries a
bearer token, either a personal access token or one issued through OAuth
(oauth.py); McpAuth rejects anything else before the SDK sees it. Agents act
with their user's role and never more. There is deliberately no tool that
creates, renames or deletes a domain (design §5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Callable, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from . import (__version__, access, attachments, contributions, documents, domains, feed, leases,
               proposals, search as searching, sections, styleguide, tags as tagmod, templates,
               tokens, tree)
from .activity import Actor
from .config import Settings
from .db import Database
from .models import Attachment, Domain, User

INSTRUCTIONS = """\
webdoc is a shared knowledge base of markdown notes and documents. You are acting for
the signed-in user who connected you, with their permissions.

- Every document lives in exactly one domain. Domains are the user's closed taxonomy:
  call list_domains before writing, and only use domains it returns. You cannot create
  or rename domains. If nothing fits, propose_domain files a request the user approves
  or rejects in the web UI (say so to the user); don't invent a domain or misfile instead.
- Document types: when the user asks for a kind of document ("an architecture document",
  "a decision record"), call get_template with that type for the domain, write each
  section following its guidance, and pass type (and the required fields) to
  create_document. list_templates shows the types a domain has. To add or change a type,
  propose_template; only the user can approve it.
- Inside a domain, documents form a page tree: any page can hold child pages. list_tree
  shows it; pass parent (a page id, or a title path such as "Infra/k8s") to create a page
  under another, and update_document moves one. File new pages where they belong in the
  tree rather than at the top. Tags are free text: when you introduce a new tag, check the
  tag_hints in the reply and reuse an existing tag if one means the same thing.
- Call get_writing_guide before writing: it says how this user likes documents written.
- Look before you create: search (by meaning) for an existing document on the subject and
  add to it rather than starting a near-duplicate. find looks up exact text (an error
  message, identifier or part number) wherever it occurs. Both cover everything the user
  can read; each result says its source (public, personal, shared) and domain.
- whats_new lists what changed since you last called it (the user's edits in the web UI,
  other agents). Call it when the user asks what's new or before picking up shared work;
  nothing calls it for you.
- Notes are short and atomic (create_note); documents are longer and sectioned
  (create_document).
- Edit by section with edit_document (replace_section, append_to_section, insert_section,
  delete_section, replace_text). They apply to the latest revision, so you never overwrite
  the user's concurrent edits. Use get_document with heading_path to read one section.
  Rewriting a whole body needs update_document with the base_revision you read.
- Every write is a revision the user can see, attributed to you. Before a large rewrite,
  save_version names the current state so it can be restored. diff_versions shows what
  changed between revisions, e.g. since you last read a document.
- If the user has a document open in the editor, writes to it are refused until they close it;
  wait or tell the user rather than retrying in a loop. For a long series of edits to one
  document, lock_document first (the user sees it and can still take over) and
  unlock_document when done.
- Public contribution domains (list_domains: accepts_contributions) are shared with everyone,
  like a Q&A site. Before posting, search them (source="public"): if the issue is already
  there, confirm the fix that worked or reply rather than posting a duplicate.
  draft_contribution checks for duplicates first and only creates a draft; the user reads
  and publishes it in the web UI, and replies work the same way. Never put private
  details in a contribution or reply: no hostnames, IPs, internal paths, project names,
  usernames or secrets. Generalise them ("a k3s cluster", "the API pod").
- delete_document moves a document to the trash (restore_document brings it back).
  Don't delete what the user wrote unless asked.
"""

SCHEME = "Bearer"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
Kind = Literal["note", "document"]
Source = Literal["public", "personal", "shared"]


class McpAuth:
    """Bearer-token gate in front of the SDK's ASGI app. A 401 points the
    client at the protected-resource metadata (RFC 9728), which is how an
    OAuth-capable client discovers it can sign the user in from a browser."""

    def __init__(self, inner: ASGIApp, db: Database, public_url: str):
        self.inner, self.db, self.public_url = inner, db, public_url.rstrip("/")
        self.metadata_url = self.public_url + RESOURCE_METADATA_PATH

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw = tokens.bearer(dict(scope["headers"]).get(b"authorization", b"").decode("latin-1"))
            ok = False
            if raw:
                with self.db.session() as s:
                    ok = tokens.principal(s, raw) is not None
            if not ok:
                challenge = (f'{SCHEME} error="invalid_token", '
                             f'resource_metadata="{self.metadata_url}"')
                res = JSONResponse(
                    {"error": "invalid_token",
                     "error_description": "sign in through your MCP client, or send an access "
                                          f"token created under Agents at {self.public_url}"},
                    status_code=401, headers={"WWW-Authenticate": challenge})
                await res(scope, receive, send)
                return
        await self.inner(scope, receive, send)


def build(db: Database, settings: Settings, indexer=None) -> tuple[MCPServer, ASGIApp]:
    server = MCPServer(name="webdoc", version=__version__, instructions=INSTRUCTIONS,
                       website_url=settings.public_url)
    limit = settings.max_document_bytes

    def run(ctx: Context, fn: Callable[[Session, User, Actor], Any]) -> Any:
        raw = tokens.bearer((ctx.headers or {}).get("authorization"))
        with db.session() as s:
            who = tokens.principal(s, raw)
            if who is None:
                raise ToolError("not signed in: the token is missing, expired or revoked")
            actor = Actor(who.user, who.client_name, "mcp")
            try:
                return fn(s, who.user, actor)
            except access.Denied as exc:
                raise ToolError(str(exc)) from None
            except documents.Conflict as exc:
                raise ToolError(f"{exc}. Read it again and retry, or use edit_document, which "
                                "applies to the latest revision.") from None
            except leases.Locked as exc:
                raise ToolError(f"{exc}. Try again after that, or ask the user; don't retry "
                                "in a loop.") from None
            except (documents.DocError, domains.DomainError, sections.SectionError,
                    tagmod.TagError, templates.TemplateError, proposals.ProposalError,
                    searching.SearchError, contributions.ContribError, tree.TreeError) as exc:
                raise ToolError(str(exc)) from None

    def holder(actor: Actor) -> leases.Holder:
        return leases.Holder("agent", actor.user, actor.client, actor.label)

    def writable(s: Session, user: User, actor: Actor, doc_id: str, action: str,
                 trashed: bool = False):
        """A document this agent may change now: allowed, and nobody else
        holds its edit lease."""
        doc = access.document(s, user, doc_id, action, trashed=trashed)
        if doc.kind == "contribution" and doc.status != "draft" and action == access.WRITE:
            # Edits to a published contribution go public at once, past the
            # human gate; the author makes them in the web UI.
            raise ToolError("this contribution is published; changes to it are made by a person "
                            "in the web UI. To add information, reply.")
        leases.check(s, doc.id, holder(actor))
        return doc

    def url(doc_id: str) -> str:
        return f"{settings.public_url}/d/{doc_id}"

    def find_domain(s: Session, user: User, ref: str, action: str = access.READ) -> Domain:
        """By id, or by slug or name among the domains the user can see."""
        ref = (ref or "").strip()
        d = s.get(Domain, ref)
        if d is None:
            hits = [x for x in access.visible_domains(s, user)
                    if x.slug == ref.lower() or x.name.lower() == ref.lower()]
            if len(hits) > 1:
                raise ToolError(f"{ref!r} names {len(hits)} domains; pass one of these ids: "
                                + ", ".join(f"{x.id} ({x.scope})" for x in hits))
            if not hits:
                raise ToolError(f"no domain {ref!r}. list_domains shows the ones you can use; "
                                "only the user can add domains, in the web UI.")
            d = hits[0]
        return access.domain(s, user, d.id, action)

    def doc_brief(doc) -> dict:
        return {"id": doc.id, "kind": doc.kind, "title": doc.title, "domain": doc.domain.slug,
                "path": tree.path(doc), "tags": doc.tags,
                "revision": doc.revision, "updated_at": doc.updated_at.isoformat()}

    def tag_hints(s: Session, d: Domain, new: list[str] | None) -> dict:
        if not new:
            return {}
        hints = tagmod.near_matches(tagmod.clean(new), tagmod.in_domain(s, d.id))
        return {"tag_hints": hints} if hints else {}

    @server.tool()
    def list_domains(ctx: Context) -> dict:
        """The domains you can use (the user's closed taxonomy, plus public and
        shared ones), each with your role, document count and its top-level pages."""
        def go(s, user, actor):
            ds = access.visible_domains(s, user)
            stats = domains.counts(s, [d.id for d in ds])
            out = []
            for d in ds:
                o = domains.out(s, d, user, stats)
                o["top_pages"] = [x["title"] for x in tree.outline(s, user, d, depth=0)][:50]
                out.append(o)
            return {"domains": out}
        return run(ctx, go)

    @server.tool()
    def list_tree(ctx: Context, domain: str, under: str | None = None,
                  depth: Annotated[int | None, Field(ge=1, le=20)] = None) -> dict:
        """A domain's page tree: titles, ids, kinds and child pages, siblings in
        order. under (a page id or title path) shows only what is below that page;
        depth limits how many levels come back (child_count says what was cut)."""
        def go(s, user, actor):
            d = find_domain(s, user, domain)
            page = tree.resolve(s, d, under) if under else None
            return {"domain": d.slug, "under": tree.path(page) if page else None,
                    "pages": tree.outline(s, user, d, page, depth)}
        return run(ctx, go)

    @server.tool()
    def get_writing_guide(ctx: Context) -> dict:
        """How this user likes documents written. Read it before writing."""
        def go(s, user, actor):
            text, default = styleguide.get(s, user)
            return {"writing_guide": text, "is_default": default}
        return run(ctx, go)

    @server.tool()
    def list_documents(ctx: Context, domain: str | None = None, under: str | None = None,
                       kind: Kind | None = None, tag: str | None = None,
                       title_contains: str | None = None, trashed: bool = False,
                       limit: Annotated[int, Field(ge=1, le=200)] = 50,
                       offset: Annotated[int, Field(ge=0)] = 0) -> dict:
        """Documents and notes you can read, newest first, without their bodies.
        Filter by domain (id or slug), under (a page id or title path, needs domain:
        everything below that page), kind, tag or a piece of the title. trashed lists
        the trash instead. list_tree shows the structure."""
        def go(s, user, actor):
            page = None
            if domain:
                d = find_domain(s, user, domain)
                ids = [d.id]
                if under:
                    page = tree.resolve(s, d, under)
            else:
                if under:
                    raise ToolError("under needs domain")
                ids = documents.readable_domain_ids(s, user)
            rows, total = documents.listing(s, user, domain_ids=ids, under=page, kind=kind,
                                            tag=tag, q=title_contains, trashed=trashed,
                                            limit=limit, offset=offset)
            return {"total": total, "documents": [doc_brief(d) for d in rows]}
        return run(ctx, go)

    def search_filters(s: Session, user: User, domain: str | None, under: str | None,
                       kind: str | None, type_: str | None, tags: list[str] | None,
                       source: str | None, author: str | None,
                       updated_after: str | None) -> searching.Filters:
        f = searching.Filters(source=source, kind=kind, type=type_, tags=tags or [],
                              author=author)
        if domain:
            d = find_domain(s, user, domain)
            f.domain_ids = [d.id]
            if under:
                f.under_id = tree.resolve(s, d, under).id
        elif under:
            raise ToolError("under needs domain")
        if updated_after:
            try:
                f.updated_after = datetime.fromisoformat(updated_after)
            except ValueError:
                raise ToolError("updated_after must be an ISO date, e.g. 2026-10-01") from None
        return f

    @server.tool()
    def search(ctx: Context, query: Annotated[str, Field(min_length=1, max_length=500)],
               domain: str | None = None, under: str | None = None, kind: Kind | None = None,
               type: str | None = None, tags: list[str] | None = None,
               source: Source | None = None, author: str | None = None,
               updated_after: str | None = None,
               limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict:
        """Find documents by meaning across everything you can read: public domains,
        the user's own and those shared with them. Each result has its source and
        domain, the best-matching section (heading) and a snippet; get_document with
        that heading_path reads the section. Filters narrow it: domain (id or slug),
        under (a page id or title path, needs domain: that page and everything below
        it), kind, type (document type), tags (all must match),
        source, author (username), updated_after (ISO date)."""
        def go(s, user, actor):
            f = search_filters(s, user, domain, under, kind, type, tags, source, author,
                               updated_after)
            emb = indexer.semantic if indexer is not None else None
            out = searching.search(s, user, emb, query, f, limit=limit)
            for r in out["results"]:
                r["url"] = url(r["id"])
            if not out["semantic"]:
                out["note"] = "semantic search is unavailable; these are keyword matches"
            if out["pending"]:
                out["note_pending"] = (f"{out['pending']} recently changed documents are still "
                                       "being indexed and may be missing or out of date")
            return out
        return run(ctx, go)

    @server.tool()
    def find(ctx: Context, text: Annotated[str, Field(min_length=1, max_length=200)],
             whole_word: bool = True, case_sensitive: bool = False,
             domain: str | None = None, under: str | None = None, kind: Kind | None = None,
             type: str | None = None, tags: list[str] | None = None,
             source: Source | None = None, author: str | None = None,
             updated_after: str | None = None,
             limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict:
        """Literal lookup: every document containing this exact text (an error
        message, identifier, part number, phrase), with the matching lines, their
        line numbers and sections. whole_word (default) won't match inside longer
        words. Same filters as search. No embeddings, so nothing is missed or blurred."""
        def go(s, user, actor):
            f = search_filters(s, user, domain, under, kind, type, tags, source, author,
                               updated_after)
            out = searching.find(s, user, text, f, whole_word=whole_word,
                                 case_sensitive=case_sensitive, limit=limit)
            for r in out["results"]:
                r["url"] = url(r["id"])
            return out
        return run(ctx, go)

    @server.tool()
    def whats_new(ctx: Context, since: str | None = None, domain: str | None = None,
                  include_own: bool = False) -> dict:
        """What changed since you (this app) last called whats_new: documents created,
        edited, moved or deleted, domain and template changes, proposals decided.
        Grouped per thing changed, with who changed it and through what. The first
        call looks back 7 days. Pass since (ISO time) to look from a fixed point
        without moving your place. include_own adds your own changes."""
        def go(s, user, actor):
            start = None
            if since:
                try:
                    start = datetime.fromisoformat(since)
                except ValueError:
                    raise ToolError("since must be an ISO time, e.g. 2026-10-01T09:00") from None
            did = find_domain(s, user, domain).id if domain else None
            out = feed.whats_new(s, user, actor.client, since=start, domain_id=did,
                                 include_own=include_own)
            for c in out["changes"]:
                if c["target_kind"] == "document" and c["exists"]:
                    c["url"] = url(c["target_id"])
            return out
        return run(ctx, go)

    @server.tool()
    def get_document(ctx: Context, document_id: str,
                     heading_path: Annotated[list[str] | None, Field(
                         description="Read just this section, e.g. [\"Architecture\", \"Auth\"].")
                     ] = None,
                     revision: int | None = None) -> dict:
        """A document's markdown, metadata, outline (its heading paths), place in the
        tree (path, child_pages) and attached files. Pass heading_path for one
        section, or revision for an earlier version."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, trashed=revision is not None)
            o = documents.out(doc, role=access.role_on(s, user, doc.domain))
            o["url"] = url(doc.id)
            lease = leases.describe(leases.active(s, doc.id))
            if lease is not None:
                o["being_edited"] = lease["message"]
            if revision is not None and revision != doc.revision:
                r = documents.revision(s, doc, revision)
                o.update(title=r.title, body=r.body, tags=list(r.tags or []), revision=r.revision)
            o["outline"] = sections.outline(o["body"])
            o["child_pages"] = [{"id": c.id, "title": c.title}
                                for c in tree.siblings(s, doc.domain_id, doc.id)
                                if access.can_see(user, c, o.get("role"))]
            files = s.scalars(select(Attachment).where(Attachment.document_id == doc.id))
            o["attachments"] = [{"name": a.name, "content_type": a.content_type, "size": a.size,
                                 "url": settings.public_url + attachments.url(a)} for a in files]
            if doc.template_type:
                try:
                    spec = templates.resolve(s, doc.domain, doc.template_type)
                    o["template_check"] = templates.check(spec, o["body"], o["fields"])
                except templates.TemplateError:
                    pass
            if doc.kind == "contribution":
                o["status"] = doc.status
                o["contribution"] = contributions.thread(s, user, doc)
                del o["contribution"]["can"]
            if heading_path:
                o["section"] = sections.get(o["body"], heading_path)
                del o["body"]
            return o
        return run(ctx, go)

    def create(ctx: Context, kind: str, title: str, domain: str, body: str,
               tags: list[str] | None, parent: str | None, type_: str | None = None,
               fields: dict | None = None) -> dict:
        def go(s, user, actor):
            d = find_domain(s, user, domain, access.WRITE)
            p = tree.resolve(s, d, parent) if parent else None
            spec = templates.resolve(s, d, type_) if type_ else None
            hints = tag_hints(s, d, tags)
            doc = documents.create(s, actor, d, kind=kind, title=title, body=body, tags=tags,
                                   parent=p, spec=spec, fields=fields, max_bytes=limit)
            return {"id": doc.id, "url": url(doc.id), "revision": doc.revision,
                    "path": tree.path(doc), **hints}
        return run(ctx, go)

    @server.tool()
    def create_document(ctx: Context, title: Annotated[str, Field(min_length=1, max_length=300)],
                        domain: str, body: str = "", tags: list[str] | None = None,
                        parent: str | None = None,
                        type: Annotated[str | None, Field(description=(
                            "A document type from list_templates, e.g. architecture. The body must "
                            "then contain the type's required sections (leave it empty to start "
                            "from the template's headings)."))] = None,
                        fields: Annotated[dict[str, str] | None, Field(description=(
                            "Values for the type's fields, e.g. {\"status\": \"draft\"}."))] = None
                        ) -> dict:
        """Create a document: longer-lived, organised under markdown headings.
        domain is an id or slug from list_domains; parent the page to put it under
        (an id or title path such as "Infra/k8s"; omit for the top of the domain)."""
        return create(ctx, "document", title, domain, body, tags, parent, type, fields)

    @server.tool()
    def create_note(ctx: Context, title: Annotated[str, Field(min_length=1, max_length=300)],
                    domain: str, body: str = "", tags: list[str] | None = None,
                    parent: str | None = None) -> dict:
        """Create a note: one short, self-contained fact, decision or finding.
        parent is the page to put it under, as for create_document."""
        return create(ctx, "note", title, domain, body, tags, parent)

    @server.tool()
    def edit_document(ctx: Context, document_id: str,
                      ops: Annotated[list[dict], Field(description=(
                          "Applied in order to the latest revision. Each is one of: "
                          "{op: replace_section, heading_path: [...], content} (replaces everything "
                          "under the heading, subsections included; the heading stays); "
                          "{op: append_to_section, heading_path, content}; "
                          "{op: insert_section, heading, content, after: [path] | parent: [path], "
                          "level?} (no after/parent = end of document); "
                          "{op: delete_section, heading_path}; "
                          "{op: replace_text, find, replace} (find must occur exactly once); "
                          "{op: append, content} (end of document). heading_path entries match "
                          "heading text, outer to inner; skipping levels is fine."),
                          min_length=1)]) -> dict:
        """Edit a document by section. Prefer this over rewriting the body."""
        def go(s, user, actor):
            doc = writable(s, user, actor, document_id, access.WRITE)
            done = documents.edit(s, actor, doc, ops, limit)
            return {"revision": doc.revision, "applied": done,
                    "outline": sections.outline(doc.body)}
        return run(ctx, go)

    @server.tool()
    def update_document(ctx: Context, document_id: str, title: str | None = None,
                        tags: list[str] | None = None,
                        fields: Annotated[dict[str, str | None] | None, Field(description=(
                            "Fields to set; merged with the current ones, null removes one."))] = None,
                        parent: str | None = None,
                        to_top: bool = False, domain: str | None = None,
                        body: str | None = None, base_revision: int | None = None) -> dict:
        """Change a document's title, tags (replaces the list), fields, place in the
        tree (parent: a page id or title path, in the target domain if moving; to_top
        moves it to the top), or domain (moving needs edit rights on both; its child
        pages go with it). Replacing the whole body needs the base_revision you read,
        and fails if someone wrote since."""
        def go(s, user, actor):
            doc = writable(s, user, actor, document_id, access.WRITE)
            if body is not None and base_revision is None:
                raise ToolError("replacing the body needs base_revision; or use edit_document")
            target = find_domain(s, user, domain, access.WRITE) if domain else None
            kw: dict = {}
            if to_top:
                kw["parent"] = None
            elif parent:
                kw["parent"] = tree.resolve(s, target or doc.domain, parent)
            hints = tag_hints(s, target or doc.domain, tags)
            changed = documents.write(s, actor, doc, title=title, tags=tags, body=body,
                                      fields=fields,
                                      domain=target, base_revision=base_revision,
                                      max_bytes=limit, **kw)
            return {"changed": changed, "revision": doc.revision, **hints}
        return run(ctx, go)

    @server.tool()
    def delete_document(ctx: Context, document_id: str) -> dict:
        """Move a document to the trash. It keeps its history and can be restored."""
        def go(s, user, actor):
            doc = writable(s, user, actor, document_id, access.DELETE)
            documents.trash(s, actor, doc)
            return {"trashed": doc.id}
        return run(ctx, go)

    @server.tool()
    def restore_document(ctx: Context, document_id: str) -> dict:
        """Bring a document back from the trash."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, access.DELETE, trashed=True)
            documents.untrash(s, actor, doc)
            return {"restored": doc.id, "url": url(doc.id)}
        return run(ctx, go)

    @server.tool()
    def lock_document(ctx: Context, document_id: str,
                      minutes: Annotated[int, Field(ge=1, le=15)] = 5) -> dict:
        """Hold a document's edit lock for a series of edits, so the user doesn't
        start editing underneath you. Calling it again renews it. The user sees
        who holds it and can still take over from the editor."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, access.WRITE)
            row = leases.take(s, doc.id, holder(actor), timedelta(minutes=minutes))
            return {"lock": leases.describe(row)}
        return run(ctx, go)

    @server.tool()
    def unlock_document(ctx: Context, document_id: str) -> dict:
        """Release your edit lock when you're done."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id)
            return {"released": leases.release(s, doc.id, holder(actor))}
        return run(ctx, go)

    @server.tool()
    def draft_contribution(ctx: Context, domain: str,
                           title: Annotated[str, Field(min_length=1, max_length=300)],
                           body: str = "", type: str | None = None,
                           fields: dict[str, str] | None = None, tags: list[str] | None = None,
                           checked_duplicates: bool = False) -> dict:
        """Draft a contribution to a public contribution domain. Only a draft: the user
        reads it and publishes it in the web UI. The first call looks for existing
        contributions on the same thing and, if it finds any, returns them without
        creating anything: confirm or reply to the one that matches. If none does, call
        again with checked_duplicates=true. If the domain takes typed contributions,
        pass type (get_template shows its sections) and its fields."""
        def go(s, user, actor):
            d = find_domain(s, user, domain)
            contributions.check_draft(d, user, type)
            if not checked_duplicates:
                emb = indexer.semantic if indexer is not None else None
                dups = contributions.possible_duplicates(s, emb, d, title, body)
                if dups:
                    for x in dups:
                        x["url"] = url(x["id"])
                    return {"created": False, "possible_duplicates": dups,
                            "next": "if one of these is the same issue, confirm or reply to it; "
                                    "otherwise call again with checked_duplicates=true"}
            doc = contributions.draft(s, actor, d, title=title, body=body, type=type,
                                      fields=fields, tags=tags, max_bytes=limit)
            return {"created": True, "id": doc.id, "status": "draft", "url": url(doc.id),
                    "next": "tell the user it's waiting for them to review and publish at "
                            f"{settings.public_url}/drafts"}
        return run(ctx, go)

    @server.tool()
    def reply(ctx: Context, document_id: str,
              kind: Literal["solution", "didnt-work", "comment"],
              body: Annotated[str, Field(min_length=1, max_length=20000)]) -> dict:
        """Draft a reply to a published contribution: solution (another way to fix it),
        didnt-work (you tried it; say what happened and in what environment), or
        comment. The user reviews and publishes it in the web UI."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id)
            r = contributions.add_reply(s, actor, doc, kind, body)
            return {"id": r.id, "status": "draft",
                    "next": "tell the user it's waiting for them to review and publish at "
                            f"{settings.public_url}/drafts"}
        return run(ctx, go)

    @server.tool()
    def confirm(ctx: Context, document_id: str, reply_id: str | None = None,
                environment: Annotated[str, Field(max_length=300)] = "") -> dict:
        """Record that a contribution's fix (or one reply's) worked for the user: the
        quality signal others look for. Only when the user says it worked. environment is
        optional and generic (OS, versions, hardware), never hosts or paths."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id)
            r = contributions.reply_for(s, user, reply_id)[0] if reply_id else None
            contributions.confirm(s, actor, doc, r, environment)
            return {"confirmed": True}
        return run(ctx, go)

    @server.tool()
    def list_drafts(ctx: Context) -> dict:
        """The user's contributions and replies still waiting to be published."""
        def go(s, user, actor):
            out = contributions.drafts(s, user)
            for c in out["contributions"]:
                c["url"] = url(c["id"])
            out["review_url"] = f"{settings.public_url}/drafts"
            return out
        return run(ctx, go)

    @server.tool()
    def list_versions(ctx: Context, document_id: str, named_only: bool = False,
                      before: int | None = None) -> dict:
        """A document's revisions, newest first: who made each, through which app,
        and what it changed. Named versions carry a label."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, trashed=True)
            rows, more = documents.revisions(s, doc, before, named_only)
            return {"current": doc.revision, "more": more,
                    "revisions": [documents.revision_out(r) for r in rows]}
        return run(ctx, go)

    @server.tool()
    def diff_versions(ctx: Context, document_id: str, from_revision: int,
                      to_revision: int | None = None) -> dict:
        """Unified diff between two revisions (to_revision defaults to the latest)."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, trashed=True)
            return {"diff": documents.diff(s, doc, from_revision, to_revision) or "(no changes)"}
        return run(ctx, go)

    @server.tool()
    def save_version(ctx: Context, document_id: str,
                     label: Annotated[str, Field(min_length=1, max_length=200)]) -> dict:
        """Name the current revision so it is easy to find and restore later."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, access.WRITE)
            documents.label(s, actor, doc, doc.revision, label)
            return {"revision": doc.revision, "label": label}
        return run(ctx, go)

    @server.tool()
    def restore_version(ctx: Context, document_id: str, revision: int) -> dict:
        """Bring back an earlier revision's title, body and tags, as a new revision."""
        def go(s, user, actor):
            doc = writable(s, user, actor, document_id, access.WRITE)
            changed = documents.restore_version(s, actor, doc, revision, limit)
            return {"changed": changed, "revision": doc.revision}
        return run(ctx, go)

    # --- document types and proposals (design §5, §6) --------------------------

    @server.tool()
    def list_templates(ctx: Context, domain: str) -> dict:
        """The document types usable in a domain (the domain's own, its owner's and
        built-ins), with their sections and fields but not the full skeleton."""
        def go(s, user, actor):
            d = find_domain(s, user, domain)
            return {"templates": [templates.out(x, with_skeleton=False)
                                  for x in templates.available(s, d)]}
        return run(ctx, go)

    @server.tool()
    def get_template(ctx: Context, type: str, domain: str) -> dict:
        """One document type for a domain: its sections with the guidance for each
        (write every required section, following the guidance), its fields, and
        `starter`, the headings to begin from."""
        def go(s, user, actor):
            d = find_domain(s, user, domain)
            return templates.out(templates.resolve(s, d, type))
        return run(ctx, go)

    @server.tool()
    def propose_domain(ctx: Context, name: Annotated[str, Field(min_length=1, max_length=100)],
                       description: str, rationale: str,
                       scope: Literal["personal", "public"] = "personal",
                       slug: str | None = None) -> dict:
        """Ask the user to add a domain to their taxonomy (or an admin, for a public
        one). Nothing changes until a person approves it in the web UI; tell the
        user it is waiting. rationale: why no existing domain fits."""
        def go(s, user, actor):
            p = proposals.propose_domain(s, actor, scope=scope, name=name, description=description,
                                         rationale=rationale, slug=slug)
            return {"proposal_id": p.id, "status": p.status,
                    "review_url": f"{settings.public_url}/proposals"}
        return run(ctx, go)

    @server.tool()
    def propose_domain_change(ctx: Context, domain: str, rationale: str, name: str | None = None,
                              slug: str | None = None, description: str | None = None) -> dict:
        """Ask the domain's owner to rename it or change its slug or description."""
        def go(s, user, actor):
            d = find_domain(s, user, domain, access.WRITE)
            p = proposals.propose_domain_change(s, actor, d, rationale=rationale, name=name,
                                                slug=slug, description=description)
            return {"proposal_id": p.id, "status": p.status,
                    "review_url": f"{settings.public_url}/proposals"}
        return run(ctx, go)

    @server.tool()
    def propose_template(ctx: Context, type: str,
                         name: Annotated[str, Field(min_length=1, max_length=100)],
                         skeleton: Annotated[str, Field(description=(
                             "Markdown: one heading per section (## Section); put guidance for a "
                             "section in an HTML comment under its heading; end a heading with "
                             "\"(optional)\" if it may be left out."))],
                         rationale: str, description: str | None = None,
                         fields: Annotated[list[dict] | None, Field(description=(
                             "[{name, description, required}] frontmatter fields the type needs. "
                             "When changing an existing type, omit to keep its current fields."))
                         ] = None,
                         default_tags: list[str] | None = None,
                         domain: Annotated[str | None, Field(description=(
                             "Only for this domain; omit for all of the user's own domains."))
                         ] = None) -> dict:
        """Propose a new document type, or a change to an existing one of the same
        type (give its full new skeleton; description, fields and default tags you
        omit stay as they are). A person approves it in the web UI."""
        def go(s, user, actor):
            d = find_domain(s, user, domain, access.WRITE) if domain else None
            p = proposals.propose_template(s, actor, d, type=type, name=name, skeleton=skeleton,
                                           rationale=rationale, description=description,
                                           fields=fields, default_tags=default_tags)
            return {"proposal_id": p.id, "kind": p.kind, "status": p.status,
                    "review_url": f"{settings.public_url}/proposals"}
        return run(ctx, go)

    @server.tool()
    def list_proposals(ctx: Context,
                       status: Literal["pending", "approved", "rejected", "withdrawn"] | None = None
                       ) -> dict:
        """Proposals made through your user's account, newest first, with the decision
        and any note from whoever decided."""
        def go(s, user, actor):
            return {"proposals": [proposals.out(s, p) for p in proposals.mine(s, user, status)]}
        return run(ctx, go)

    host = settings.public_url.split("://", 1)[-1].split("/", 1)[0]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, "localhost", "localhost:*", "127.0.0.1:*", "testserver"],
        allowed_origins=[settings.public_url.rstrip("/"), "http://localhost:*",
                         "http://127.0.0.1:*"])
    server.streamable_http_app(streamable_http_path="/mcp", json_response=True,
                               stateless_http=True, transport_security=security)
    return server, McpAuth(StreamableHTTPASGIApp(server.session_manager), db,
                           settings.public_url)

