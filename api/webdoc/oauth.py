"""OAuth for MCP clients: sign in from the browser, no token to paste.

Copied from nodewright (apps/nodewright/api/nodewright/oauth.py).

webdoc is the authorization server its MCP clients talk to. The SDK's
handlers (mcp.server.auth) do the protocol: metadata, dynamic client
registration, /authorize parameter checks, PKCE, /token, /revoke. This module
is the storage behind them and the one decision they leave to the app: the
consent page, where the signed-in user approves a client. Who the user is
still comes from authentik, through the normal browser session.

Clients register themselves without any credentials, so anyone can create
one with any name. The consent page therefore shows where the user will be
sent back to, and approving needs the user's own session plus a one-time
nonce that only the rendered page carries.
"""

from __future__ import annotations

import hashlib
import html
import secrets
from dataclasses import dataclass
from datetime import timedelta, timezone
from urllib.parse import urlencode, urlsplit

import anyio
from pydantic import AnyUrl
from sqlalchemy import delete, select

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, RegistrationError,
                                      TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import Database
from .models import OAuthClient, OAuthCode, OAuthGrant, OAuthRequest, User, utcnow

SCOPE = "docs"
ACCESS_PREFIX = "wdo_"
REFRESH_PREFIX = "wdr_"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(minutes=5)
REQUEST_TTL = timedelta(minutes=10)
UNUSED_CLIENT_TTL = timedelta(days=1)
CONSENT_PATH = "/oauth/consent"
MAX_GRANTS_PER_USER = 50
TOUCH_EVERY = timedelta(minutes=1)
LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def redirect_ok(uri: str) -> bool:
    """https anywhere, http only to this machine (CLI clients listen on a
    loopback port), or an app's own scheme (vscode://, cursor://)."""
    parts = urlsplit(uri)
    if parts.fragment:
        return False
    if parts.scheme == "https":
        return bool(parts.hostname)
    if parts.scheme == "http":
        return parts.hostname in LOOPBACK or f"[{parts.hostname}]" in LOOPBACK
    return parts.scheme not in ("", "javascript", "data", "file", "blob", "about")


def form_action_source(uri: str) -> str:
    """The CSP source that lets the consent form's redirect reach `uri`;
    browsers apply form-action to the redirect after a form POST."""
    parts = urlsplit(uri)
    if parts.scheme in ("http", "https"):
        return f"{parts.scheme}://{parts.netloc}"
    return f"{parts.scheme}:"


def client_label(info: dict) -> str:
    return " ".join(str(info.get("client_name") or "MCP client").split())[:100]


def grant_for_access(s, raw: str) -> OAuthGrant | None:
    if not raw.startswith(ACCESS_PREFIX) or len(raw) > 100:
        return None
    g = s.scalar(select(OAuthGrant).where(OAuthGrant.access_hash == _hash(raw)))
    if g is None or _aware(g.access_expires_at) <= utcnow():
        return None
    now = utcnow()
    if g.last_used_at is None or _aware(g.last_used_at) < now - TOUCH_EVERY:
        g.last_used_at = now
    return g


@dataclass
class Principal:
    """Who an MCP request acts for, from either kind of bearer token."""
    user: User
    client: str          # lease holder id: token or grant id
    label: str
    client_name: str     # recorded on revisions and events: the app's name or "token:<name>"


class Provider:
    """OAuthAuthorizationServerProvider backed by the app database."""

    def __init__(self, db: Database, public_url: str):
        self.db = db
        self.public_url = public_url.rstrip("/")
        self.resource = self.public_url + "/mcp"

    async def _run(self, fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    # --- clients ----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def go():
            with self.db.session() as s:
                row = s.get(OAuthClient, client_id[:64])
                return None if row is None else OAuthClientInformationFull.model_validate(row.info)
        return await self._run(go)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > 10 or not all(redirect_ok(u) and len(u) <= 500 for u in uris):
            raise RegistrationError("invalid_redirect_uri",
                                    "redirect URIs must be https, http on loopback, or an app scheme")

        def go():
            with self.db.session() as s:
                # Registration is open, so drop clients nobody ever approved.
                cutoff = utcnow() - UNUSED_CLIENT_TTL
                used = select(OAuthGrant.client_id)
                s.execute(delete(OAuthClient).where(OAuthClient.created_at < cutoff,
                                                    OAuthClient.client_id.not_in(used)))
                s.add(OAuthClient(client_id=client_info.client_id,
                                  info=client_info.model_dump(mode="json")))
        await self._run(go)

    # --- authorize: hand over to the consent page ---------------------------

    def resource_ok(self, resource: str | None) -> bool:
        return resource is None or resource.rstrip("/") in (self.public_url, self.resource)

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        if not self.resource_ok(params.resource):
            raise AuthorizeError("invalid_target", "this server only issues tokens for "
                                 + self.resource)
        req_id = secrets.token_urlsafe(32)

        def go():
            with self.db.session() as s:
                s.execute(delete(OAuthRequest).where(OAuthRequest.expires_at < utcnow()))
                s.add(OAuthRequest(id=req_id, client_id=client.client_id,
                                   params=params.model_dump(mode="json"),
                                   expires_at=utcnow() + REQUEST_TTL))
        await self._run(go)
        return f"{self.public_url}{CONSENT_PATH}?{urlencode({'req': req_id})}"

    # --- codes ------------------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        def go():
            with self.db.session() as s:
                row = s.get(OAuthCode, _hash(authorization_code))
                if row is None or row.client_id != client.client_id:
                    return None
                p = row.params
                return AuthorizationCode(
                    code=authorization_code, scopes=p.get("scopes") or [SCOPE],
                    expires_at=_aware(row.expires_at).timestamp(), client_id=row.client_id,
                    code_challenge=p["code_challenge"], redirect_uri=AnyUrl(p["redirect_uri"]),
                    redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                    resource=p.get("resource"), subject=row.user_id)
        return await self._run(go)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        def go():
            with self.db.session() as s:
                # Single use: deleting it is what makes a replayed code fail.
                n = s.execute(delete(OAuthCode).where(
                    OAuthCode.code_hash == _hash(authorization_code.code))).rowcount
                if not n:
                    raise TokenError("invalid_grant", "authorization code already used")
                user = s.get(User, authorization_code.subject)
                if user is None:
                    raise TokenError("invalid_grant", "user no longer exists")
                grants = list(s.scalars(select(OAuthGrant).where(OAuthGrant.user_id == user.id)
                                        .order_by(OAuthGrant.created_at)))
                for old in grants[:max(0, len(grants) - MAX_GRANTS_PER_USER + 1)]:
                    s.delete(old)
                grant = OAuthGrant(user_id=user.id, client_id=client.client_id,
                                   client_name=client_label(client.model_dump()),
                                   scopes=" ".join(authorization_code.scopes),
                                   resource=authorization_code.resource)
                return self._issue(s, grant)
        return await self._run(go)

    def _issue(self, s, grant: OAuthGrant) -> OAuthToken:
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        now = utcnow()
        grant.access_hash, grant.refresh_hash = _hash(access), _hash(refresh)
        grant.access_expires_at = now + ACCESS_TTL
        grant.refresh_expires_at = now + REFRESH_TTL
        s.add(grant)
        s.flush()
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=int(ACCESS_TTL.total_seconds()),
                          refresh_token=refresh, scope=grant.scopes or None)

    # --- refresh ----------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        def go():
            with self.db.session() as s:
                g = s.scalar(select(OAuthGrant).where(
                    OAuthGrant.refresh_hash == _hash(refresh_token)))
                if g is None or g.client_id != client.client_id:
                    return None
                return RefreshToken(token=refresh_token, client_id=g.client_id,
                                    scopes=g.scopes.split() or [SCOPE],
                                    expires_at=int(_aware(g.refresh_expires_at).timestamp()),
                                    resource=g.resource, subject=g.user_id)
        return await self._run(go)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        def go():
            with self.db.session() as s:
                g = s.scalar(select(OAuthGrant).where(
                    OAuthGrant.refresh_hash == _hash(refresh_token.token)))
                if g is None:
                    raise TokenError("invalid_grant", "refresh token is no longer valid")
                g.scopes = " ".join(scopes) or g.scopes
                return self._issue(s, g)
        return await self._run(go)

    # --- access tokens ----------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        def go():
            with self.db.session() as s:
                g = grant_for_access(s, token)
                if g is None:
                    return None
                return AccessToken(token=token, client_id=g.client_id, scopes=g.scopes.split(),
                                   expires_at=int(_aware(g.access_expires_at).timestamp()),
                                   resource=g.resource, subject=g.user_id)
        return await self._run(go)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)

        def go():
            with self.db.session() as s:
                s.execute(delete(OAuthGrant).where(
                    (OAuthGrant.access_hash == h) | (OAuthGrant.refresh_hash == h)))
        await self._run(go)

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "not supported")


# --- consent page (sync; called from FastAPI routes) -------------------------

class ConsentError(Exception):
    pass


def pending(s, req_id: str) -> tuple[OAuthRequest, dict]:
    row = s.get(OAuthRequest, (req_id or "")[:64])
    if row is None or _aware(row.expires_at) <= utcnow():
        raise ConsentError("This sign-in request has expired. Start the connection again "
                           "from your MCP client.")
    client = s.get(OAuthClient, row.client_id)
    if client is None:
        raise ConsentError("The app that asked to connect is no longer registered.")
    return row, client.info


def show(s, row: OAuthRequest, user: User) -> str:
    """Bind the request to the user viewing it; returns the form nonce."""
    if row.user_id and row.user_id != user.id:
        raise ConsentError("This sign-in request belongs to another account.")
    row.user_id = user.id
    row.nonce = secrets.token_urlsafe(24)
    return row.nonce


def decide(s, row: OAuthRequest, user: User, nonce: str, approve: bool) -> str:
    """Approve or deny; returns the URL to send the browser back to."""
    if not (row.user_id == user.id and row.nonce and nonce
            and secrets.compare_digest(row.nonce, nonce)):
        raise ConsentError("This approval did not come from the page webdoc showed you. "
                           "Start the connection again.")
    p = row.params
    s.delete(row)
    if not approve:
        return construct_redirect_uri(p["redirect_uri"], error="access_denied",
                                      error_description="the user declined", state=p.get("state"))
    code = secrets.token_urlsafe(32)
    s.add(OAuthCode(code_hash=_hash(code), client_id=row.client_id, user_id=user.id, params=p,
                    expires_at=utcnow() + CODE_TTL))
    return construct_redirect_uri(p["redirect_uri"], code=code, state=p.get("state"))


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · webdoc</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;background:#f4f5f7;color:#1d2330;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
main{{background:#fff;border:1px solid #d8dce3;border-radius:10px;padding:28px 32px;max-width:440px;
box-shadow:0 4px 18px rgba(0,0,0,.06)}}
h1{{font-size:19px;margin:0 0 12px}} p{{margin:8px 0}} code{{background:#eef0f4;padding:1px 5px;
border-radius:4px;word-break:break-all}} .row{{display:flex;gap:10px;margin-top:22px}}
button{{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid #c5cad3;background:#fff;
cursor:pointer}} button.primary{{background:#2f6fde;border-color:#2f6fde;color:#fff}}
.muted{{color:#5d6675;font-size:13px}}
</style></head><body><main>{body}</main></body></html>"""


def consent_html(info: dict, params: dict, user: User, req_id: str, nonce: str) -> str:
    name = html.escape(client_label(info))
    dest = urlsplit(params["redirect_uri"])
    where = html.escape(dest.netloc or dest.scheme + ":")
    e = html.escape
    return page("Connect an app", f"""
<h1>Allow <strong>{name}</strong> to use webdoc?</h1>
<p>Signed in as <strong>{e(user.username)}</strong>.</p>
<p>It will be able to read and search everything you can read, create and edit documents and
notes wherever you are an editor, and propose new domains for you to approve. It cannot change
your domains, roles, settings or tokens.</p>
<p class="muted">After you answer you will be sent to <code>{where}</code>. Only allow it if you
just started connecting this app yourself; the name above is chosen by the app.</p>
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="req" value="{e(req_id)}"><input type="hidden" name="nonce" value="{e(nonce)}">
<div class="row"><button class="primary" name="decision" value="allow" type="submit">Allow</button>
<button name="decision" value="deny" type="submit">Deny</button></div></form>
<p class="muted">You can disconnect it later under <strong>Agents</strong>.</p>""")


def error_html(message: str) -> str:
    return page("Cannot connect", f"<h1>Cannot connect</h1><p>{html.escape(message)}</p>"
                '<p><a href="/">Open webdoc</a></p>')


def grant_out(g: OAuthGrant) -> dict:
    return {"id": g.id, "client_name": g.client_name, "created_at": g.created_at.isoformat(),
            "last_used_at": g.last_used_at.isoformat() if g.last_used_at else None}

