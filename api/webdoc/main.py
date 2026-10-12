"""HTTP app: sign-in, domains, projects, documents and their history, agents, and the built UI.

Sign-in, the OAuth consent page and token routes are copied from nodewright.
Reads of public domains work signed out (design §4); everything else needs a
session, and every write the X-Webdoc header (auth.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.routing import Route

from . import (__version__, access, auth, contributions, documents, domains, embeddings, events,
               export, feed, scan,
               indexer as indexing, leases, mcp_server, oauth, oidc, proposals, search, sections,
               shares, styleguide, tags as tagmod, templates, tokens)
from .activity import Actor
from .config import Settings
from .db import Database
from .models import (PLATFORM, AccessToken, Document, DocumentTag, Domain, DomainRevision,
                     OAuthGrant, Report, ShareLink, StyleGuide, Template, TemplateRevision, User, utcnow)

log = logging.getLogger("webdoc")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
NEXT_COOKIE = "wd_next"
TAB_HEADER = "X-Webdoc-Tab"      # a browser tab's id, so leases tell tabs apart
SSE_PING_S = 20
ANON_SEARCHES_PER_MINUTE = 30


class IpLimiter:
    """Sliding one-minute window per client address, in memory (one replica).
    Signed-out search is limited because each semantic query costs a CPU
    embedding (design §4)."""

    def __init__(self, per_minute: int):
        self.per_minute, self.hits = per_minute, {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 10_000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
            q = [x for x in self.hits.get(key, []) if now - x < 60]
            if len(q) >= self.per_minute:
                self.hits[key] = q
                return False
            q.append(now)
            self.hits[key] = q
            return True


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DevLoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    admin: bool = False
    editor: bool = True


class DomainIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=2000)
    scope: str = Field(default="personal", pattern="^(personal|public)$")


class DomainPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


class GrantIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: str = Field(max_length=16)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class DocumentIn(BaseModel):
    domain_id: str = Field(max_length=32)
    kind: str = Field(default="document", pattern="^(note|document|contribution)$")
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    tags: list[str] = []
    project_id: str | None = Field(default=None, max_length=32)
    type: str | None = Field(default=None, max_length=64)
    fields: dict[str, str] = {}


class DocumentPut(BaseModel):
    """Only the fields that are sent change. project_id: null means no project."""
    model_config = ConfigDict(extra="forbid")
    base_revision: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = None
    tags: list[str] | None = None
    fields: dict[str, str | None] | None = None
    project_id: str | None = Field(default=None, max_length=32)
    domain_id: str | None = Field(default=None, max_length=32)
    ack_warnings: bool = False
    """Publish an edit to a public contribution despite scan warnings."""


class ContribSettingsIn(BaseModel):
    accepts: bool
    types: list[str] = []


class AckIn(BaseModel):
    ack_warnings: bool = False


class ReplyIn(BaseModel):
    kind: str = Field(max_length=16)
    body: str = Field(min_length=1, max_length=contributions.MAX_REPLY)
    ack_warnings: bool = False


class ReplyPatch(BaseModel):
    kind: str | None = Field(default=None, max_length=16)
    body: str | None = Field(default=None, max_length=contributions.MAX_REPLY)


class AcceptIn(BaseModel):
    reply_id: str | None = Field(default=None, max_length=32)


class ConfirmIn(BaseModel):
    reply_id: str | None = Field(default=None, max_length=32)
    environment: str = Field(default="", max_length=contributions.MAX_ENV)


class ModerateIn(BaseModel):
    action: str = Field(pattern="^(hide|unhide|lock|unlock|duplicate|unduplicate)$")
    reason: str = Field(default="", max_length=contributions.MAX_REASON)
    of: str | None = Field(default=None, max_length=32)


class ReplyModerateIn(BaseModel):
    hidden: bool
    reason: str = Field(default="", max_length=contributions.MAX_REASON)


class ReportIn(BaseModel):
    document_id: str = Field(max_length=32)
    reply_id: str | None = Field(default=None, max_length=32)
    reason: str = Field(min_length=1, max_length=contributions.MAX_REASON)


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(dismiss|hide)$")
    note: str = Field(default="", max_length=contributions.MAX_REASON)


class TemplateIn(BaseModel):
    scope: str = Field(default="personal", pattern="^(personal|public)$")
    domain_id: str | None = Field(default=None, max_length=32)
    type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    skeleton: str = Field(max_length=templates.MAX_SKELETON)
    fields: list[dict] = []
    default_tags: list[str] = []


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    skeleton: str | None = Field(default=None, max_length=templates.MAX_SKELETON)
    fields: list[dict] | None = None
    default_tags: list[str] | None = None


class DecisionIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    edits: dict | None = None


class LabelIn(BaseModel):
    label: str | None = Field(default=None, max_length=200)


class GuideIn(BaseModel):
    text: str = Field(max_length=styleguide.MAX_CHARS)


class _Issuer(BaseModel):
    # RFC 8414 clients compare the issuer exactly; keep it free of a trailing slash.
    model_config = ConfigDict(url_preserve_empty_path=True)
    u: AnyHttpUrl


def create_app(settings: Settings | None = None, provider: oidc.Provider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    broker = events.Broker()
    db = Database(settings.database_url, broker)
    db.migrate()
    if provider is None and settings.oidc_enabled:
        provider = oidc.Provider(settings)
    if settings.dev_login and not settings.dev_login_enabled:
        log.warning("DEV_LOGIN ignored: PUBLIC_URL is https")
    limit = settings.max_document_bytes

    indexer = indexing.Indexer(db, embeddings.make(settings))
    mcp, mcp_app = mcp_server.build(db, settings, indexer)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if settings.background_index:
            indexer.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            indexer.stop()

    app = FastAPI(title="webdoc", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.indexer = settings, db, indexer
    app.state.broker = broker

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.path.startswith(("/s/", "/api/s/")):
            # Share links are private by obscurity; keep them out of search engines.
            response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response

    def session():
        with db.session() as s:
            yield s

    def user_dep(request: Request, s: Session = Depends(session)) -> User:
        return auth.require_user(s, request, settings)

    def maybe_user(request: Request, s: Session = Depends(session)) -> User | None:
        """Reads that signed-out visitors may make (public domains)."""
        return auth.user_for_request(s, request, settings)

    def actor_for(user: User) -> Actor:
        return Actor(user, "web", "ui")

    def ui_holder(request: Request, user: User) -> leases.Holder:
        tab = re.sub(r"[^A-Za-z0-9_-]", "", request.headers.get(TAB_HEADER, ""))[:40]
        return leases.Holder("ui", user, tab or "browser", user.username)

    def guard(fn, *args, **kw):
        """Run a domain/document operation, mapping its errors to HTTP."""
        try:
            return fn(*args, **kw)
        except access.Denied as exc:
            raise access.http(exc) from None
        except documents.Conflict as exc:
            raise HTTPException(409, {"message": str(exc), "current": exc.current}) from None
        except leases.Locked as exc:
            raise HTTPException(423, {"message": str(exc),
                                      "lease": leases.describe(exc.lease)}) from None
        except scan.Blocked as exc:
            raise HTTPException(422, {"message": str(exc), "scan": exc.result,
                                      "needs_ack": exc.needs_ack}) from None
        except contributions.RateLimited as exc:
            raise HTTPException(429, str(exc)) from None
        except (documents.DocError, domains.DomainError, sections.SectionError,
                tagmod.TagError, templates.TemplateError, proposals.ProposalError,
                search.SearchError, shares.ShareError, contributions.ContribError) as exc:
            raise HTTPException(422, str(exc)) from None

    # --- meta and auth ------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/config")
    def public_config():
        return {"oidc": provider is not None, "dev_login": settings.dev_login_enabled,
                "version": __version__, "max_document_bytes": limit}

    @app.get("/api/me")
    def me(user: User = Depends(user_dep)):
        return {"id": user.id, "username": user.username, "is_admin": user.is_admin,
                "is_editor": access.is_editor(user)}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, s: Session = Depends(session)):
        if request.headers.get(auth.CSRF_HEADER) != "1":
            raise HTTPException(403, "missing X-Webdoc header")
        auth.end_session(s, request, settings, response)
        return {"ok": True}

    def safe_next(value: str) -> str:
        # Only the OAuth consent page needs a way back after sign-in.
        return value if value.startswith(oauth.CONSENT_PATH + "?") and len(value) < 300 else ""

    @app.get(oidc.FLOW_PATH + "/login")
    def oidc_login(next: str = ""):
        if provider is None:
            raise HTTPException(404, "sign-in is not set up on this server")
        try:
            url, flow = oidc.begin(provider, settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("oidc: discovery failed: %s", exc)
            raise HTTPException(502, "cannot reach the sign-in server") from None
        res = RedirectResponse(url, status_code=302)
        res.set_cookie(oidc.FLOW_COOKIE, flow, max_age=oidc.FLOW_TTL_S, path=oidc.FLOW_PATH,
                       httponly=True, secure=settings.secure, samesite="lax")
        if safe_next(next):
            res.set_cookie(NEXT_COOKIE, next, max_age=oidc.FLOW_TTL_S, path=oidc.FLOW_PATH,
                           httponly=True, secure=settings.secure, samesite="lax")
        return res

    @app.get(oidc.FLOW_PATH + "/callback")
    def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                      s: Session = Depends(session)):
        if provider is None:
            raise HTTPException(404, "sign-in is not set up on this server")
        res = RedirectResponse(safe_next(request.cookies.get(NEXT_COOKIE, "")) or "/",
                               status_code=302)
        res.delete_cookie(oidc.FLOW_COOKIE, path=oidc.FLOW_PATH)
        res.delete_cookie(NEXT_COOKIE, path=oidc.FLOW_PATH)
        if error:
            return RedirectResponse("/?signin_error=" + re.sub(r"[^a-z_]", "", error)[:40], 302)
        try:
            claims = oidc.finish(provider, settings, request.cookies.get(oidc.FLOW_COOKIE, ""),
                                 code, state)
            user = oidc.resolve_user(s, claims, settings.admin_group, settings.editor_group)
            auth.start_session(s, user, settings, res)
        except oidc.SignInRefused:
            return RedirectResponse("/?signin_error=refused", 302)
        except Exception:  # noqa: BLE001
            log.exception("oidc: callback failed")
            return RedirectResponse("/?signin_error=failed", 302)
        return res

    if settings.dev_login_enabled:
        @app.post("/api/auth/dev-login")
        def dev_login(body: DevLoginIn, request: Request, response: Response,
                      s: Session = Depends(session)):
            if request.headers.get(auth.CSRF_HEADER) != "1":
                raise HTTPException(403, "missing X-Webdoc header")
            name = auth.USERNAME_RE.sub("", body.name.lower())[:60] or "dev"
            user = s.scalar(select(User).where(User.username == name)) or auth.create_user(s, name)
            user.is_admin, user.is_editor = body.admin, body.editor or body.admin
            if user.is_editor:
                auth.ensure_personal_domain(s, user)
            auth.start_session(s, user, settings, response)
            return {"id": user.id, "username": user.username, "is_admin": user.is_admin,
                    "is_editor": access.is_editor(user)}

    # --- home and domains ---------------------------------------------------

    @app.get("/api/home")
    def home(user: User | None = Depends(maybe_user), s: Session = Depends(session)):
        """Domains grouped the way the home page shows them (design §11)."""
        ds = access.visible_domains(s, user)
        stats = domains.counts(s, [d.id for d in ds])
        groups: dict[str, list] = {"public": [], "personal": [], "shared": []}
        for d in ds:
            key = ("public" if d.scope == "public"
                   else "personal" if user is not None and d.owner_user_id == user.id else "shared")
            groups[key].append(domains.out(s, d, user, stats))
        return groups

    # --- search and activity -------------------------------------------------

    anon_search = IpLimiter(ANON_SEARCHES_PER_MINUTE)

    def filters(source: str | None = None, domain_id: list[str] = Query([]),
                project_id: str | None = None, kind: str | None = None, type: str | None = None,
                tag: list[str] = Query([]), author: str | None = None,
                updated_after: datetime | None = None,
                updated_before: datetime | None = None) -> search.Filters:
        return search.Filters(source=source or None, domain_ids=domain_id or None,
                              project_id=project_id or None, kind=kind or None,
                              type=type or None, tags=tag or [], author=author or None,
                              updated_after=updated_after, updated_before=updated_before)

    @app.get("/api/search")
    def run_search(request: Request, q: str = Query(min_length=1, max_length=500),
                   mode: str = Query("semantic", pattern="^(semantic|find)$"),
                   whole_word: bool = True, case_sensitive: bool = False,
                   limit: int = Query(20, ge=1, le=100),
                   f: search.Filters = Depends(filters),
                   user: User | None = Depends(maybe_user), s: Session = Depends(session)):
        """Everything the caller can read (design §8, §11): semantic search, or
        find for literal text."""
        if user is None and not anon_search.allow(request.client.host if request.client else "?"):
            raise HTTPException(429, "too many searches; sign in or wait a minute")
        if mode == "find":
            return guard(search.find, s, user, q, f, whole_word=whole_word,
                         case_sensitive=case_sensitive, limit=limit)
        out = guard(search.search, s, user, indexer.semantic, q, f, limit=limit)
        out["index_error"] = indexer.error
        return out

    @app.get("/api/activity")
    def activity(domain_id: str | None = None, before: datetime | None = None,
                 limit: int = Query(50, ge=1, le=200), user: User | None = Depends(maybe_user),
                 s: Session = Depends(session)):
        return guard(feed.feed, s, user, domain_id=domain_id, before=before, limit=limit)

    @app.post("/api/domains", status_code=201)
    def create_domain(body: DomainIn, user: User = Depends(user_dep), s: Session = Depends(session)):
        if body.scope == "public" and not user.is_admin:
            raise HTTPException(403, "only admins create public domains")
        if not access.is_editor(user):
            raise HTTPException(403, "viewers can't create domains; an admin can make you an editor")
        owner = PLATFORM if body.scope == "public" else domains.personal_key(user)
        d = guard(domains.create, s, actor_for(user), owner, body.name, body.slug, body.description)
        return domains.out(s, d, user)

    def domain_detail(s: Session, d, user: User | None) -> dict:
        o = domains.out(s, d, user, domains.counts(s, [d.id]))
        o["projects"] = [domains.project_out(p) for p in domains.projects(s, d)]
        o["can"] = sorted(access.ROLES.get(o["role"] or "", ()))
        o["assignable"] = access.assignable(s, user, d)
        o["contribution_type_names"] = (
            {t: templates.resolve(s, d, t).name for t in d.contribution_types or []}
            if d.scope == "public" else {})
        o["tags"] = sorted(tagmod.in_domain(s, d.id))
        return o

    @app.get("/api/domains/{domain_id}")
    def get_domain(domain_id: str, user: User | None = Depends(maybe_user),
                   s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id)
        return domain_detail(s, d, user)

    @app.patch("/api/domains/{domain_id}")
    def patch_domain(domain_id: str, body: DomainPatch, user: User = Depends(user_dep),
                     s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.MANAGE)
        guard(domains.update, s, actor_for(user), d, body.name, body.slug, body.description)
        return domain_detail(s, d, user)

    @app.delete("/api/domains/{domain_id}", status_code=204)
    def delete_domain(domain_id: str, user: User = Depends(user_dep),
                      s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.MANAGE)
        guard(domains.delete, s, actor_for(user), d)
        return Response(status_code=204)

    @app.get("/api/domains/{domain_id}/revisions")
    def domain_history(domain_id: str, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.MANAGE)
        rows = s.scalars(select(DomainRevision).where(DomainRevision.domain_id == d.id)
                         .order_by(DomainRevision.revision.desc()))
        names = {u.id: u.username for u in s.scalars(select(User))}
        return {"revisions": [{"revision": r.revision, "name": r.name, "slug": r.slug,
                               "description": r.description, "by": names.get(r.actor_id),
                               "created_at": r.created_at.isoformat()} for r in rows]}

    @app.get("/api/domains/{domain_id}/export")
    def export_domain(domain_id: str, user: User | None = Depends(maybe_user),
                      s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id)
        return Response(export.domain_zip(s, d), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{d.slug}.zip"'})

    # --- grants -------------------------------------------------------------

    @app.get("/api/domains/{domain_id}/grants")
    def list_grants(domain_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id)
        return {"grants": [domains.grant_out(g) for g in domains.grants(s, d)],
                "assignable": access.assignable(s, user, d),
                "owner": d.owner.username if d.owner else None}

    @app.put("/api/domains/{domain_id}/grants")
    def set_grant(domain_id: str, body: GrantIn, user: User = Depends(user_dep),
                  s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.GRANTS)
        target = s.scalar(select(User).where(User.username == body.username.strip().lower()))
        if target is None:
            raise HTTPException(404, f"no user {body.username!r}; they need to sign in once first")
        g = guard(domains.set_grant, s, actor_for(user), d, target, body.role)
        return domains.grant_out(g)

    @app.delete("/api/domains/{domain_id}/grants/{user_id}", status_code=204)
    def remove_grant(domain_id: str, user_id: str, user: User = Depends(user_dep),
                     s: Session = Depends(session)):
        # Anyone may drop their own role; otherwise it takes GRANTS.
        d = guard(access.domain, s, user, domain_id,
                  access.READ if user_id == user.id else access.GRANTS)
        guard(domains.remove_grant, s, actor_for(user), d, user_id)
        return Response(status_code=204)

    # --- projects -----------------------------------------------------------

    @app.post("/api/domains/{domain_id}/projects", status_code=201)
    def create_project(domain_id: str, body: ProjectIn, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.WRITE)
        return domains.project_out(guard(domains.create_project, s, actor_for(user), d,
                                         body.name, body.description))

    @app.patch("/api/projects/{project_id}")
    def rename_project(project_id: str, body: NameIn, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        p = guard(access.project, s, user, project_id, access.WRITE)
        return domains.project_out(guard(domains.rename_project, s, actor_for(user), p, body.name))

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        p = guard(access.project, s, user, project_id, access.MANAGE)
        domains.delete_project(s, actor_for(user), p)
        return Response(status_code=204)

    # --- documents ----------------------------------------------------------

    @app.get("/api/documents")
    def list_documents(domain: str | None = None, project: str | None = None,
                       kind: str | None = None, tag: str | None = None, q: str | None = None,
                       trashed: bool = False, limit: int = 50, offset: int = 0,
                       user: User | None = Depends(maybe_user), s: Session = Depends(session)):
        ids = guard(documents.readable_domain_ids, s, user, [domain] if domain else None)
        rows, total = documents.listing(s, user, domain_ids=ids, project_id=project, kind=kind,
                                        tag=tag, q=q, trashed=trashed,
                                        limit=max(1, min(limit, 200)), offset=max(0, offset))
        return {"total": total, "documents": [documents.out(d, body=False) for d in rows]}

    @app.post("/api/documents", status_code=201)
    def create_document(body: DocumentIn, user: User = Depends(user_dep),
                        s: Session = Depends(session)):
        if body.kind == "contribution":
            d = guard(access.domain, s, user, body.domain_id)
            doc = guard(contributions.draft, s, actor_for(user), d, title=body.title,
                        body=body.body, type=body.type, fields=body.fields, tags=body.tags,
                        max_bytes=limit)
            return doc_full(s, doc, user)
        d = guard(access.domain, s, user, body.domain_id, access.WRITE)
        p = guard(access.project, s, user, body.project_id) if body.project_id else None
        spec = guard(templates.resolve, s, d, body.type) if body.type else None
        doc = guard(documents.create, s, actor_for(user), d, kind=body.kind, title=body.title,
                    body=body.body, tags=body.tags, project=p, spec=spec, fields=body.fields,
                    max_bytes=limit)
        return documents.out(doc, role=access.role_on(s, user, d))

    def doc_full(s: Session, doc: Document, user: User | None) -> dict:
        role = access.role_on(s, user, doc.domain)
        o = documents.out(doc, role=role)
        o["can_delete"] = access.DELETE in access.ROLES.get(role or "", ())
        o["can_purge"] = access.PURGE in access.ROLES.get(role or "", ())
        o["domain_scope"] = doc.domain.scope
        o["lease"] = leases.describe(leases.active(s, doc.id))
        o["can_share"] = (doc.domain.scope != "public"
                          and access.GRANTS in access.ROLES.get(role or "", ()))
        # The author edits their own contribution without a role on the domain.
        o["can_edit"] = access.doc_allows(user, doc, role, access.WRITE) and doc.deleted_at is None
        o["can_delete"] = access.doc_allows(user, doc, role, access.DELETE)
        o["status"] = doc.status
        # Readers may report what they can see, except their own work.
        o["can_report"] = (user is not None and doc.status == "published"
                           and doc.created_by != user.id
                           and not (doc.domain.scope != "public"
                                    and doc.domain.owner_user_id == user.id))
        o["hidden_reason"] = doc.hidden_reason if doc.status == "hidden" else None
        if doc.kind == "contribution":
            o["contribution"] = contributions.thread(s, user, doc)
        if doc.template_type:
            try:
                spec = templates.resolve(s, doc.domain, doc.template_type)
                o["template"] = {**templates.out(spec, with_skeleton=False),
                                 "check": templates.check(spec, doc.body, doc.fields or {})}
            except templates.TemplateError:
                o["template"] = None
        return o

    @app.get("/api/documents/{doc_id}")
    def get_document(doc_id: str, user: User | None = Depends(maybe_user),
                     s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, trashed=True)
        return doc_full(s, doc, user)

    @app.put("/api/documents/{doc_id}")
    def put_document(doc_id: str, body: DocumentPut, request: Request,
                     user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        guard(leases.check, s, doc.id, ui_holder(request, user))
        guard(contributions.check_edit, doc, body.body, body.title, body.ack_warnings)
        target = guard(access.domain, s, user, body.domain_id, access.WRITE) \
            if body.domain_id and body.domain_id != doc.domain_id else None
        kw: dict = {}
        if "project_id" in body.model_fields_set:
            kw["project"] = (guard(access.project, s, user, body.project_id)
                             if body.project_id else None)
        guard(documents.write, s, actor_for(user), doc, title=body.title, body=body.body,
              tags=body.tags, fields=body.fields, domain=target, base_revision=body.base_revision, max_bytes=limit,
              **kw)
        return doc_full(s, doc, user)

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def trash_document(doc_id: str, request: Request, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.DELETE)
        guard(leases.check, s, doc.id, ui_holder(request, user))
        documents.trash(s, actor_for(user), doc)
        return Response(status_code=204)

    @app.post("/api/documents/{doc_id}/restore")
    def untrash_document(doc_id: str, user: User = Depends(user_dep),
                         s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.DELETE, trashed=True)
        guard(documents.untrash, s, actor_for(user), doc)
        return doc_full(s, doc, user)

    @app.delete("/api/documents/{doc_id}/purge", status_code=204)
    def purge_document(doc_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.PURGE, trashed=True)
        guard(documents.purge, s, actor_for(user), doc)
        return Response(status_code=204)

    @app.get("/api/documents/{doc_id}/export")
    def export_document(doc_id: str, user: User | None = Depends(maybe_user),
                        s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        name = export.filename(doc.title)
        return Response(export.markdown(doc), media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})

    # --- history ------------------------------------------------------------

    @app.get("/api/documents/{doc_id}/revisions")
    def list_revisions(doc_id: str, before: int | None = None, named: bool = False,
                       user: User | None = Depends(maybe_user), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, trashed=True)
        rows, more = documents.revisions(s, doc, before, named)
        return {"current": doc.revision, "more": more,
                "revisions": [documents.revision_out(r) for r in rows]}

    @app.get("/api/documents/{doc_id}/revisions/{number}")
    def get_revision(doc_id: str, number: int, user: User | None = Depends(maybe_user),
                     s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, trashed=True)
        r = guard(documents.revision, s, doc, number)
        return {**documents.revision_out(r), "title": r.title, "body": r.body,
                "tags": list(r.tags or [])}

    @app.patch("/api/documents/{doc_id}/revisions/{number}")
    def name_revision(doc_id: str, number: int, body: LabelIn, user: User = Depends(user_dep),
                      s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        return documents.revision_out(guard(documents.label, s, actor_for(user), doc, number,
                                            body.label))

    @app.post("/api/documents/{doc_id}/revisions/{number}/restore")
    def restore_revision(doc_id: str, number: int, request: Request,
                         user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        guard(leases.check, s, doc.id, ui_holder(request, user))
        guard(documents.restore_version, s, actor_for(user), doc, number, limit)
        return doc_full(s, doc, user)

    @app.get("/api/documents/{doc_id}/diff")
    def diff(doc_id: str, a: int, b: int | None = None, user: User | None = Depends(maybe_user),
             s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, trashed=True)
        return {"diff": guard(documents.diff, s, doc, a, b)}

    # --- edit lease and live updates (design §7) -----------------------------

    @app.get("/api/documents/{doc_id}/lease")
    def get_lease(doc_id: str, user: User | None = Depends(maybe_user),
                  s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        return {"lease": leases.describe(leases.active(s, doc.id))}

    @app.post("/api/documents/{doc_id}/lease")
    def take_lease(doc_id: str, request: Request, force: bool = False,
                   user: User = Depends(user_dep), s: Session = Depends(session)):
        """Take or renew this tab's lease while its editor is open; force takes
        over from whoever holds it (anyone who may edit can)."""
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        row = guard(leases.take, s, doc.id, ui_holder(request, user), force=force)
        return {"lease": leases.describe(row)}

    @app.delete("/api/documents/{doc_id}/lease")
    def drop_lease(doc_id: str, request: Request, user: User = Depends(user_dep),
                   s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        return {"released": leases.release(s, doc.id, ui_holder(request, user))}

    @app.get("/api/documents/{doc_id}/events")
    async def document_events(doc_id: str, request: Request):
        """Server-sent events for one document: new revisions (with who made
        them), lease changes, trash and restore. Whoever can read the document
        can listen, signed-out visitors of public domains included."""
        with db.session() as s:
            user = auth.user_for_request(s, request, settings)
            doc = guard(access.document, s, user, doc_id)
            hello = {"type": "hello", "revision": doc.revision,
                     "lease": leases.describe(leases.active(s, doc.id))}
        q = broker.subscribe(doc_id)

        async def stream():
            try:
                yield f"data: {json.dumps(hello)}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), SSE_PING_S)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(ev)}\n\n"
            finally:
                broker.unsubscribe(doc_id, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # --- read-only share links (shares.py) ------------------------------------

    @app.get("/api/documents/{doc_id}/shares")
    def list_shares(doc_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        guard(access.domain, s, user, doc.domain_id, access.GRANTS)
        return {"shares": [shares.out(x, settings.public_url)
                           for x in shares.for_document(s, doc.id)]}

    @app.post("/api/documents/{doc_id}/shares", status_code=201)
    def create_share(doc_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        guard(access.domain, s, user, doc.domain_id, access.GRANTS)
        return shares.out(guard(shares.create, s, actor_for(user), doc), settings.public_url)

    @app.delete("/api/shares/{share_id}", status_code=204)
    def revoke_share(share_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        link = s.get(ShareLink, share_id)
        if link is None:
            raise HTTPException(404, "no such share link")
        doc = guard(access.document, s, user, link.document_id, trashed=True)
        guard(access.domain, s, user, doc.domain_id, access.GRANTS)
        shares.revoke(s, actor_for(user), link)
        return Response(status_code=204)

    # --- contributions (contributions.py, design §12) -------------------------
    # Publishing is here and only here: these routes take browser sessions, and
    # no MCP tool publishes, so a person always reads what goes public.

    @app.patch("/api/domains/{domain_id}/contributions")
    def contribution_settings(domain_id: str, body: ContribSettingsIn,
                              user: User = Depends(user_dep), s: Session = Depends(session)):
        d = guard(access.domain, s, user, domain_id, access.MODERATE)
        guard(contributions.configure, s, actor_for(user), d, body.accepts, body.types)
        return domain_detail(s, d, user)

    @app.get("/api/documents/{doc_id}/scan")
    def scan_document(doc_id: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        return scan.scan(contributions.scan_text(doc))

    @app.post("/api/documents/{doc_id}/publish")
    def publish_document(doc_id: str, body: AckIn, user: User = Depends(user_dep),
                         s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id, access.WRITE)
        guard(contributions.publish, s, actor_for(user), doc, body.ack_warnings)
        return doc_full(s, doc, user)

    @app.post("/api/documents/{doc_id}/replies", status_code=201)
    def post_reply(doc_id: str, body: ReplyIn, user: User = Depends(user_dep),
                   s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        guard(contributions.add_reply, s, actor_for(user), doc, body.kind, body.body,
              publish_now=True, ack_warnings=body.ack_warnings)
        return doc_full(s, doc, user)

    @app.patch("/api/replies/{reply_id}")
    def edit_reply(reply_id: str, body: ReplyPatch, user: User = Depends(user_dep),
                   s: Session = Depends(session)):
        r, doc = guard(contributions.reply_for, s, user, reply_id)
        guard(contributions.edit_reply_draft, r, actor_for(user), body.kind, body.body)
        return doc_full(s, doc, user)

    @app.post("/api/replies/{reply_id}/publish")
    def publish_reply(reply_id: str, body: AckIn, user: User = Depends(user_dep),
                      s: Session = Depends(session)):
        r, doc = guard(contributions.reply_for, s, user, reply_id)
        guard(contributions.publish_reply, s, actor_for(user), r, doc, body.ack_warnings)
        return doc_full(s, doc, user)

    @app.delete("/api/replies/{reply_id}")
    def withdraw_reply(reply_id: str, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        r, doc = guard(contributions.reply_for, s, user, reply_id)
        guard(contributions.withdraw_reply, s, actor_for(user), r, doc)
        return doc_full(s, doc, user)

    @app.post("/api/replies/{reply_id}/moderate")
    def moderate_reply(reply_id: str, body: ReplyModerateIn, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        r, doc = guard(contributions.reply_for, s, user, reply_id)
        guard(contributions.hide_reply, s, actor_for(user), r, doc, body.hidden, body.reason)
        return doc_full(s, doc, user)

    @app.post("/api/documents/{doc_id}/accept")
    def accept_reply(doc_id: str, body: AcceptIn, user: User = Depends(user_dep),
                     s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        r = guard(contributions.reply_for, s, user, body.reply_id)[0] if body.reply_id else None
        guard(contributions.accept, s, actor_for(user), doc, r)
        return doc_full(s, doc, user)

    @app.post("/api/documents/{doc_id}/confirm")
    def confirm(doc_id: str, body: ConfirmIn, user: User = Depends(user_dep),
                s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        r = guard(contributions.reply_for, s, user, body.reply_id)[0] if body.reply_id else None
        guard(contributions.confirm, s, actor_for(user), doc, r, body.environment)
        return doc_full(s, doc, user)

    @app.delete("/api/documents/{doc_id}/confirm")
    def unconfirm(doc_id: str, reply_id: str | None = None, user: User = Depends(user_dep),
                  s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        contributions.unconfirm(s, actor_for(user), doc, reply_id)
        return doc_full(s, doc, user)

    @app.post("/api/documents/{doc_id}/moderate")
    def moderate(doc_id: str, body: ModerateIn, user: User = Depends(user_dep),
                 s: Session = Depends(session)):
        doc = guard(access.document, s, user, doc_id)
        a, actor = body.action, actor_for(user)
        if a in ("hide", "unhide"):
            guard(contributions.hide, s, actor, doc, a == "hide", body.reason)
        elif a in ("lock", "unlock"):
            guard(contributions.lock, s, actor, doc, a == "lock")
        else:
            of = None
            if a == "duplicate":
                if not body.of:
                    raise HTTPException(422, "say which contribution it duplicates (of)")
                of = guard(access.document, s, user, body.of)
            guard(contributions.mark_duplicate, s, actor, doc, of)
        return doc_full(s, doc, user)

    @app.post("/api/reports", status_code=201)
    def file_report(body: ReportIn, user: User = Depends(user_dep), s: Session = Depends(session)):
        doc = guard(access.document, s, user, body.document_id)
        r = guard(contributions.reply_for, s, user, body.reply_id)[0] if body.reply_id else None
        rep = guard(contributions.report, s, actor_for(user), doc, body.reason, r)
        return {"id": rep.id, "status": rep.status}

    @app.get("/api/reports")
    def list_reports(user: User = Depends(user_dep), s: Session = Depends(session)):
        return {"reports": [contributions.report_out(s, r)
                            for r in contributions.open_reports(s, user)],
                "recent": [contributions.report_out(s, r)
                           for r in contributions.recent_decisions(s, user)],
                "moderates": contributions.handles_reports(s, user)}

    @app.get("/api/reports/{report_id}/review")
    def review_report(report_id: str, user: User = Depends(user_dep),
                      s: Session = Depends(session)):
        """The reported document, read-only, while the report is open: for a
        personal document, an admin's only way to see it (design §4)."""
        rep = guard(contributions.report_for, s, user, report_id)
        return guard(contributions.review, s, actor_for(user), rep)

    @app.post("/api/reports/{report_id}/resolve")
    def resolve_report(report_id: str, body: ResolveIn, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        rep = guard(contributions.report_for, s, user, report_id)
        guard(contributions.resolve_report, s, actor_for(user), rep, body.action, body.note)
        return contributions.report_out(s, rep)

    @app.post("/api/reports/{report_id}/undo")
    def undo_report(report_id: str, user: User = Depends(user_dep),
                    s: Session = Depends(session)):
        rep = guard(contributions.report_for, s, user, report_id)
        guard(contributions.undo_report, s, actor_for(user), rep)
        return contributions.report_out(s, rep)

    @app.get("/api/drafts")
    def my_drafts(user: User = Depends(user_dep), s: Session = Depends(session)):
        return contributions.drafts(s, user)

    @app.get("/api/counts")
    def counts(user: User = Depends(user_dep), s: Session = Depends(session)):
        """Header badges: drafts waiting to be reviewed, proposals to decide,
        open reports to handle."""
        return {"drafts": contributions.draft_count(s, user),
                "proposals": len(proposals.to_decide(s, user)),
                "reports": len(contributions.open_reports(s, user)),
                "moderates": contributions.handles_reports(s, user)}

    @app.get("/api/s/{token}")
    def open_share(token: str, s: Session = Depends(session)):
        return shares.shared_out(guard(shares.resolve, s, token))

    @app.get("/api/tags")
    def list_tags(domain: str | None = None, user: User | None = Depends(maybe_user),
                  s: Session = Depends(session)):
        ids = guard(documents.readable_domain_ids, s, user, [domain] if domain else None)
        rows = s.execute(select(DocumentTag.tag, func.count()).join(Document)
                         .where(Document.domain_id.in_(ids), Document.deleted_at.is_(None))
                         .group_by(DocumentTag.tag).order_by(func.count().desc(), DocumentTag.tag))
        return {"tags": [{"tag": t, "count": n} for t, n in rows]}

    # --- templates (design §6) -----------------------------------------------

    @app.get("/api/domains/{domain_id}/templates")
    def domain_templates(domain_id: str, user: User | None = Depends(maybe_user),
                         s: Session = Depends(session)):
        """Types usable in a domain, resolved (domain, owner, built-in)."""
        d = guard(access.domain, s, user, domain_id)
        return {"templates": [templates.out(x) for x in templates.available(s, d)]}

    @app.get("/api/templates")
    def list_templates(user: User = Depends(user_dep), s: Session = Depends(session)):
        """Templates this user can change, plus the built-ins."""
        return {"templates": [templates.stored_out(s, t) for t in templates.managed(s, user)],
                "builtin": [templates.out(x) for x in templates.BUILTINS.values()]}

    def template_for(s: Session, user: User, tid: str) -> Template:
        t = s.get(Template, tid)
        d = s.get(Domain, t.domain_id) if t and t.domain_id else None
        if t is None or not templates.can_manage(s, user, t.owner_key, d):
            raise HTTPException(404, "template not found")
        return t

    @app.post("/api/templates", status_code=201)
    def create_template(body: TemplateIn, user: User = Depends(user_dep),
                        s: Session = Depends(session)):
        owner = PLATFORM if body.scope == "public" else domains.personal_key(user)
        d = None
        if body.domain_id:
            d = guard(access.domain, s, user, body.domain_id)
            owner = d.owner_key
        if not templates.can_manage(s, user, owner, d):
            raise HTTPException(403, "you can't manage templates there")
        t = guard(templates.create, s, actor_for(user), owner, d, type=body.type, name=body.name,
                  description=body.description, skeleton=body.skeleton, fields=body.fields,
                  default_tags=body.default_tags)
        return templates.stored_out(s, t)

    @app.patch("/api/templates/{tid}")
    def patch_template(tid: str, body: TemplatePatch, user: User = Depends(user_dep),
                       s: Session = Depends(session)):
        t = template_for(s, user, tid)
        guard(templates.update, s, actor_for(user), t, name=body.name, description=body.description,
              skeleton=body.skeleton, fields=body.fields, default_tags=body.default_tags)
        return templates.stored_out(s, t)

    @app.delete("/api/templates/{tid}", status_code=204)
    def delete_template(tid: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        templates.delete(s, actor_for(user), template_for(s, user, tid))
        return Response(status_code=204)

    @app.get("/api/templates/{tid}/revisions")
    def template_history(tid: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        t = template_for(s, user, tid)
        rows = s.scalars(select(TemplateRevision).where(TemplateRevision.template_id == t.id)
                         .order_by(TemplateRevision.version.desc()))
        names = {u.id: u.username for u in s.scalars(select(User))}
        return {"revisions": [{"version": r.version, "name": r.name, "skeleton": r.skeleton,
                               "fields": r.fields, "by": names.get(r.actor_id),
                               "created_at": r.created_at.isoformat()} for r in rows]}

    # --- proposals (design §5) -----------------------------------------------

    @app.get("/api/proposals")
    def list_proposals(user: User = Depends(user_dep), s: Session = Depends(session)):
        return {"to_decide": [proposals.out(s, p, user) for p in proposals.to_decide(s, user)],
                "mine": [proposals.out(s, p, user) for p in proposals.mine(s, user)]}

    @app.get("/api/proposals/count")
    def count_proposals(user: User = Depends(user_dep), s: Session = Depends(session)):
        return {"pending": len(proposals.to_decide(s, user))}

    @app.post("/api/proposals/{pid}/approve")
    def approve_proposal(pid: str, body: DecisionIn, user: User = Depends(user_dep),
                         s: Session = Depends(session)):
        p = guard(proposals.get, s, user, pid)
        guard(proposals.approve, s, actor_for(user), p, body.edits)
        return proposals.out(s, p, user)

    @app.post("/api/proposals/{pid}/reject")
    def reject_proposal(pid: str, body: DecisionIn, user: User = Depends(user_dep),
                        s: Session = Depends(session)):
        p = guard(proposals.get, s, user, pid)
        guard(proposals.reject, s, actor_for(user), p, body.note)
        return proposals.out(s, p, user)

    @app.post("/api/proposals/{pid}/withdraw")
    def withdraw_proposal(pid: str, user: User = Depends(user_dep), s: Session = Depends(session)):
        p = guard(proposals.get, s, user, pid)
        guard(proposals.withdraw, s, actor_for(user), p)
        return proposals.out(s, p, user)

    # --- agents: tokens, connected apps, writing guide (cookie session only) --

    @app.get("/api/tokens")
    def list_tokens(user: User = Depends(user_dep), s: Session = Depends(session)):
        rows = s.scalars(select(AccessToken).where(AccessToken.user_id == user.id)
                         .order_by(AccessToken.created_at))
        return [tokens.out(r) for r in rows]

    @app.post("/api/tokens", status_code=201)
    def create_token(body: NameIn, user: User = Depends(user_dep), s: Session = Depends(session)):
        if tokens.count(s, user) >= tokens.MAX_PER_USER:
            raise HTTPException(409, f"at most {tokens.MAX_PER_USER} tokens; revoke one first")
        row, raw = tokens.create(s, user, body.name)
        return {**tokens.out(row), "token": raw,
                "mcp_url": settings.public_url.rstrip("/") + "/mcp"}

    @app.delete("/api/tokens/{token_id}", status_code=204)
    def revoke_token(token_id: str, user: User = Depends(user_dep),
                     s: Session = Depends(session)):
        row = s.get(AccessToken, token_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(404, "token not found")
        s.delete(row)
        return Response(status_code=204)

    @app.get("/api/writing-guide")
    def get_guide(user: User = Depends(user_dep), s: Session = Depends(session)):
        text, default = styleguide.get(s, user)
        return {"text": text, "is_default": default}

    @app.put("/api/writing-guide")
    def put_guide(body: GuideIn, user: User = Depends(user_dep), s: Session = Depends(session)):
        row = s.get(StyleGuide, user.id)
        if not body.text.strip():
            if row is not None:
                s.delete(row)
            return {"text": styleguide.DEFAULT, "is_default": True}
        if row is None:
            s.add(StyleGuide(user_id=user.id, text=body.text))
        else:
            row.text, row.updated_at = body.text, utcnow()
        return {"text": body.text, "is_default": False}

    @app.get("/api/oauth/grants")
    def list_oauth_grants(user: User = Depends(user_dep), s: Session = Depends(session)):
        rows = s.scalars(select(OAuthGrant).where(OAuthGrant.user_id == user.id)
                         .order_by(OAuthGrant.created_at.desc()))
        return {"grants": [oauth.grant_out(g) for g in rows]}

    @app.delete("/api/oauth/grants/{grant_id}", status_code=204)
    def revoke_oauth_grant(grant_id: str, user: User = Depends(user_dep),
                           s: Session = Depends(session)):
        g = s.get(OAuthGrant, grant_id)
        if g is None or g.user_id != user.id:
            raise HTTPException(404, "not found")
        s.delete(g)
        return Response(status_code=204)

    # --- OAuth consent for MCP clients (oauth.py) ----------------------------

    def consent_page(body: str, status: int = 200, redirect_to: str | None = None) -> HTMLResponse:
        csp = CSP
        if redirect_to:
            csp = CSP.replace("form-action 'self'",
                              f"form-action 'self' {oauth.form_action_source(redirect_to)}")
        return HTMLResponse(body, status_code=status, headers={
            "Content-Security-Policy": csp, "Cache-Control": "no-store",
            "X-Frame-Options": "DENY"})

    @app.get(oauth.CONSENT_PATH)
    def consent(request: Request, req: str = "", s: Session = Depends(session)):
        try:
            row, info = oauth.pending(s, req)
        except oauth.ConsentError as exc:
            return consent_page(oauth.error_html(str(exc)), 400)
        user = auth.user_for_request(s, request, settings)
        if user is None:
            if provider is None:
                return consent_page(oauth.error_html(
                    "Sign in to webdoc in this browser first, then reload this page."), 401)
            back = f"{oauth.CONSENT_PATH}?req={req}"
            return RedirectResponse(f"{oidc.FLOW_PATH}/login?next={quote(back, safe='')}", 302)
        try:
            nonce = oauth.show(s, row, user)
        except oauth.ConsentError as exc:
            return consent_page(oauth.error_html(str(exc)), 403)
        return consent_page(oauth.consent_html(info, row.params, user, req, nonce),
                            redirect_to=row.params["redirect_uri"])

    @app.post(oauth.CONSENT_PATH)
    async def consent_decide(request: Request):
        form = await request.form()
        req, nonce = str(form.get("req") or ""), str(form.get("nonce") or "")
        approve = form.get("decision") == "allow"
        with db.session() as s:
            user = auth.user_for_request(s, request, settings)
            try:
                row, _ = oauth.pending(s, req)
                if user is None:
                    raise oauth.ConsentError("You are no longer signed in. Start again.")
                url = oauth.decide(s, row, user, nonce, approve)
            except oauth.ConsentError as exc:
                return consent_page(oauth.error_html(str(exc)), 400)
        return RedirectResponse(url, status_code=303)

    # RFC 8414 wants an https issuer; plain http is only allowed on loopback.
    if settings.secure or urlsplit(settings.public_url).hostname in ("localhost", "127.0.0.1"):
        issuer = _Issuer(u=settings.public_url).u
        app.router.routes.extend(create_auth_routes(
            oauth.Provider(db, settings.public_url), issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[oauth.SCOPE], default_scopes=[oauth.SCOPE]),
            revocation_options=RevocationOptions(enabled=True)))
        resource_routes = create_protected_resource_routes(
            resource_url=AnyHttpUrl(settings.public_url + "/mcp"), authorization_servers=[issuer],
            scopes_supported=[oauth.SCOPE], resource_name="webdoc")
        app.router.routes.extend(resource_routes)
        # Some clients look for the metadata without the resource path.
        app.router.routes.append(Route("/.well-known/oauth-protected-resource",
                                       endpoint=resource_routes[0].endpoint,
                                       methods=["GET", "OPTIONS"]))
    else:
        log.warning("MCP OAuth disabled: PUBLIC_URL is neither https nor localhost")

    # MCP endpoint (bearer tokens, not cookies); must come before the catch-alls.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]))

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(rest: str):
        raise HTTPException(404, "not found")

    # --- the built UI -------------------------------------------------------

    static = settings.static_dir
    if static and os.path.isdir(static):
        root = os.path.realpath(static)

        @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
        def ui(path: str):
            target = os.path.realpath(os.path.join(root, path))
            if path and target.startswith(root + os.sep) and os.path.isfile(target):
                cache = ("public, max-age=31536000, immutable" if path.startswith("assets/")
                         else "no-cache")
                return FileResponse(target, headers={"Cache-Control": cache})
            # Client-side routes fall back to the app shell.
            return FileResponse(os.path.join(root, "index.html"),
                                headers={"Cache-Control": "no-cache"})

    return app

