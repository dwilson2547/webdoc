"""Sign-in through authentik: OIDC authorization code flow with PKCE.

authentik answers "who is this"; webdoc keeps its own users and sessions
(auth.py). Copied from nodewright, which took the pattern from dxf-converter,
except the result is a session cookie set on the callback redirect, so no
token ever passes through page script or localStorage.

State, nonce and the PKCE verifier ride in a short-lived HttpOnly cookie
scoped to the callback path.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import User, UserIdentity
from . import auth

log = logging.getLogger("webdoc.oidc")

FLOW_COOKIE = "wd_oidc"
FLOW_PATH = "/api/auth/oidc"
FLOW_TTL_S = 600


class SignInRefused(Exception):
    pass


class Provider:
    """authentik's endpoints and signing keys, fetched on first use."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._meta: dict | None = None
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        with self._lock:
            if self._meta is None:
                url = self.settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
                r = httpx.get(url, timeout=10)
                r.raise_for_status()
                self._meta = r.json()
                self._jwks = jwt.PyJWKClient(self._meta["jwks_uri"], cache_keys=True)
            return self._meta

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict:
        r = httpx.post(self.metadata()["token_endpoint"], timeout=15,
                       auth=(self.settings.oidc_client_id, self.settings.oidc_client_secret),
                       data={"grant_type": "authorization_code", "code": code,
                             "redirect_uri": redirect_uri, "code_verifier": verifier})
        if r.status_code != 200:
            log.warning("oidc: token endpoint %s: %s", r.status_code, r.text[:300])
            raise SignInRefused("authentik refused the sign-in code")
        return r.json()

    def verify(self, id_token: str, nonce: str) -> dict:
        try:
            self.metadata()
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(
                id_token, key, algorithms=["RS256", "ES256"],
                audience=self.settings.oidc_client_id, issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as exc:
            log.warning("oidc: ID token rejected: %s", exc)
            raise SignInRefused("the sign-in could not be verified") from None
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInRefused("the sign-in could not be verified")
        return claims


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_url}{FLOW_PATH}/callback"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def begin(provider: Provider, settings: Settings) -> tuple[str, str]:
    """Return (authorization URL, flow cookie value)."""
    state, nonce, verifier = (secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                              secrets.token_urlsafe(48))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    url = provider.metadata()["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri(settings), "scope": "openid email profile",
        "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return url, f"{state}.{nonce}.{verifier}"


def finish(provider: Provider, settings: Settings, flow_cookie: str, code: str,
           state: str) -> dict:
    parts = (flow_cookie or "").split(".")
    if len(parts) != 3 or not state or not secrets.compare_digest(parts[0], state):
        raise SignInRefused("the sign-in expired or came from another window")
    _, nonce, verifier = parts
    tokens = provider.exchange(code, verifier, redirect_uri(settings))
    return provider.verify(tokens.get("id_token") or "", nonce)


def resolve_user(s: Session, claims: dict, admin_group: str | None = None,
                 editor_group: str | None = None) -> User:
    """The local account for an authentik identity, with admin and editor
    status synced from its groups at every sign-in (design §4)."""
    issuer, subject = claims["iss"], claims["sub"]
    # authentik puts group names in the `groups` claim (profile scope).
    groups = claims.get("groups") or []
    admin = bool(admin_group) and admin_group in groups
    editor = admin or not editor_group or editor_group in groups
    user = s.scalar(select(User).join(UserIdentity).where(
        UserIdentity.issuer == issuer, UserIdentity.subject == subject))
    if user is None:
        wanted = (claims.get("preferred_username")
                  or (claims.get("email") or "").split("@")[0] or "user")
        user = auth.create_user(s, wanted, editor)
        s.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
        log.warning("oidc: created user %s", user.username)
    if admin_group and admin != user.is_admin:
        log.warning("oidc: %s admin=%s", user.username, admin)
        user.is_admin = admin
    if editor != user.is_editor:
        log.warning("oidc: %s editor=%s", user.username, editor)
        user.is_editor = editor
    if editor:
        auth.ensure_personal_domain(s, user)
    return user
