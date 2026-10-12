"""The MCP server: what an agent sees (design §10).

Streamable HTTP at /mcp, stateless, JSON responses. Every request carries a
bearer token, either a personal access token or one issued through OAuth
(oauth.py); McpAuth rejects anything else before the SDK sees it. Agents act
with their user's role and never more. There is deliberately no tool that
creates, renames or deletes a domain (design §5).
"""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from pydantic import Field
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, access, documents, domains, sections, styleguide, tags as tagmod, tokens
from .activity import Actor
from .config import Settings
from .db import Database
from .models import Domain, User

INSTRUCTIONS = """\
webdoc is a shared knowledge base of markdown notes and documents. You are acting for
the signed-in user who connected you, with their permissions.

- Every document lives in exactly one domain. Domains are the user's closed taxonomy:
  call list_domains before writing, and only use domains it returns. You cannot create
  or rename domains; if nothing fits, ask the user to add one in the web UI.
- Projects (optional, inside a domain) and tags are open: create_project makes a project,
  and tags are free text. When you introduce a new tag, check the tag_hints in the reply:
  reuse an existing tag if one means the same thing.
- Call get_writing_guide before writing: it says how this user likes documents written.
- Look before you create: list_documents (filter by domain, project, tag or title) to
  find an existing document and add to it rather than starting a near-duplicate.
- Notes are short and atomic (create_note); documents are longer and sectioned
  (create_document).
- Edit by section with edit_document (replace_section, append_to_section, insert_section,
  delete_section, replace_text). They apply to the latest revision, so you never overwrite
  the user's concurrent edits. Use get_document with heading_path to read one section.
  Rewriting a whole body needs update_document with the base_revision you read.
- Every write is a revision the user can see, attributed to you. Before a large rewrite,
  save_version names the current state so it can be restored. diff_versions shows what
  changed between revisions, e.g. since you last read a document.
- delete_document moves a document to the trash (restore_document brings it back).
  Don't delete what the user wrote unless asked.
"""

SCHEME = "Bearer"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
Kind = Literal["note", "document"]


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


def build(db: Database, settings: Settings) -> tuple[MCPServer, ASGIApp]:
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
            except (documents.DocError, domains.DomainError, sections.SectionError,
                    tagmod.TagError) as exc:
                raise ToolError(str(exc)) from None

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
                "project": doc.project.name if doc.project else None, "tags": doc.tags,
                "revision": doc.revision, "updated_at": doc.updated_at.isoformat()}

    def tag_hints(s: Session, d: Domain, new: list[str] | None) -> dict:
        if not new:
            return {}
        hints = tagmod.near_matches(tagmod.clean(new), tagmod.in_domain(s, d.id))
        return {"tag_hints": hints} if hints else {}

    @server.tool()
    def list_domains(ctx: Context) -> dict:
        """The domains you can use (the user's closed taxonomy, plus public and
        shared ones), each with your role, its projects and document count."""
        def go(s, user, actor):
            ds = access.visible_domains(s, user)
            stats = domains.counts(s, [d.id for d in ds])
            out = []
            for d in ds:
                o = domains.out(s, d, user, stats)
                o["projects"] = [p.name for p in domains.projects(s, d)]
                out.append(o)
            return {"domains": out}
        return run(ctx, go)

    @server.tool()
    def create_project(ctx: Context, domain: str,
                       name: Annotated[str, Field(min_length=1, max_length=100)],
                       description: str = "") -> dict:
        """Add a project (an optional grouping) inside a domain you can edit."""
        def go(s, user, actor):
            d = find_domain(s, user, domain, access.WRITE)
            return domains.project_out(domains.create_project(s, actor, d, name, description))
        return run(ctx, go)

    @server.tool()
    def get_writing_guide(ctx: Context) -> dict:
        """How this user likes documents written. Read it before writing."""
        def go(s, user, actor):
            text, default = styleguide.get(s, user)
            return {"writing_guide": text, "is_default": default}
        return run(ctx, go)

    @server.tool()
    def list_documents(ctx: Context, domain: str | None = None, project: str | None = None,
                       kind: Kind | None = None, tag: str | None = None,
                       title_contains: str | None = None, trashed: bool = False,
                       limit: Annotated[int, Field(ge=1, le=200)] = 50,
                       offset: Annotated[int, Field(ge=0)] = 0) -> dict:
        """Documents and notes you can read, newest first, without their bodies.
        Filter by domain (id or slug), project (name, needs domain), kind, tag or
        a piece of the title. trashed lists the trash instead."""
        def go(s, user, actor):
            pid = None
            if domain:
                d = find_domain(s, user, domain)
                ids = [d.id]
                if project:
                    pid = domains.find_project(s, d, project).id
            else:
                if project:
                    raise ToolError("project needs domain")
                ids = documents.readable_domain_ids(s, user)
            rows, total = documents.listing(s, user, domain_ids=ids, project_id=pid, kind=kind,
                                            tag=tag, q=title_contains, trashed=trashed,
                                            limit=limit, offset=offset)
            return {"total": total, "documents": [doc_brief(d) for d in rows]}
        return run(ctx, go)

    @server.tool()
    def get_document(ctx: Context, document_id: str,
                     heading_path: Annotated[list[str] | None, Field(
                         description="Read just this section, e.g. [\"Architecture\", \"Auth\"].")
                     ] = None,
                     revision: int | None = None) -> dict:
        """A document's markdown, metadata and outline (its heading paths). Pass
        heading_path for one section, or revision for an earlier version."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, trashed=revision is not None)
            o = documents.out(doc, role=access.role_on(s, user, doc.domain))
            o["url"] = url(doc.id)
            if revision is not None and revision != doc.revision:
                r = documents.revision(s, doc, revision)
                o.update(title=r.title, body=r.body, tags=list(r.tags or []), revision=r.revision)
            o["outline"] = sections.outline(o["body"])
            if heading_path:
                o["section"] = sections.get(o["body"], heading_path)
                del o["body"]
            return o
        return run(ctx, go)

    def create(ctx: Context, kind: str, title: str, domain: str, body: str,
               tags: list[str] | None, project: str | None) -> dict:
        def go(s, user, actor):
            d = find_domain(s, user, domain, access.WRITE)
            p = domains.find_project(s, d, project) if project else None
            hints = tag_hints(s, d, tags)
            doc = documents.create(s, actor, d, kind=kind, title=title, body=body, tags=tags,
                                   project=p, max_bytes=limit)
            return {"id": doc.id, "url": url(doc.id), "revision": doc.revision, **hints}
        return run(ctx, go)

    @server.tool()
    def create_document(ctx: Context, title: Annotated[str, Field(min_length=1, max_length=300)],
                        domain: str, body: str = "", tags: list[str] | None = None,
                        project: str | None = None) -> dict:
        """Create a document: longer-lived, organised under markdown headings.
        domain is an id or slug from list_domains; project a project name in it."""
        return create(ctx, "document", title, domain, body, tags, project)

    @server.tool()
    def create_note(ctx: Context, title: Annotated[str, Field(min_length=1, max_length=300)],
                    domain: str, body: str = "", tags: list[str] | None = None,
                    project: str | None = None) -> dict:
        """Create a note: one short, self-contained fact, decision or finding."""
        return create(ctx, "note", title, domain, body, tags, project)

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
            doc = access.document(s, user, document_id, access.WRITE)
            done = documents.edit(s, actor, doc, ops, limit)
            return {"revision": doc.revision, "applied": done,
                    "outline": sections.outline(doc.body)}
        return run(ctx, go)

    @server.tool()
    def update_document(ctx: Context, document_id: str, title: str | None = None,
                        tags: list[str] | None = None, project: str | None = None,
                        no_project: bool = False, domain: str | None = None,
                        body: str | None = None, base_revision: int | None = None) -> dict:
        """Change a document's title, tags (replaces the list), project, or domain
        (moving needs edit rights on both). Replacing the whole body needs the
        base_revision you read, and fails if someone wrote since."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, access.WRITE)
            if body is not None and base_revision is None:
                raise ToolError("replacing the body needs base_revision; or use edit_document")
            target = find_domain(s, user, domain, access.WRITE) if domain else None
            kw: dict = {}
            if no_project:
                kw["project"] = None
            elif project:
                kw["project"] = domains.find_project(s, target or doc.domain, project)
            hints = tag_hints(s, target or doc.domain, tags)
            changed = documents.write(s, actor, doc, title=title, tags=tags, body=body,
                                      domain=target, base_revision=base_revision,
                                      max_bytes=limit, **kw)
            return {"changed": changed, "revision": doc.revision, **hints}
        return run(ctx, go)

    @server.tool()
    def delete_document(ctx: Context, document_id: str) -> dict:
        """Move a document to the trash. It keeps its history and can be restored."""
        def go(s, user, actor):
            doc = access.document(s, user, document_id, access.DELETE)
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
            doc = access.document(s, user, document_id, access.WRITE)
            changed = documents.restore_version(s, actor, doc, revision, limit)
            return {"changed": changed, "revision": doc.revision}
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

