"""Accounts and browser sessions.

Who someone is comes from authentik (oidc.py). This module owns the local
account and the session: a random token in an HttpOnly cookie, stored hashed.
Random rather than a JWT so signing out, or deleting the row, ends it at once.

CSRF: state-changing requests must carry the X-Webdoc header. A browser
only sends a custom header cross-origin after a CORS preflight, which this app
never approves, so a page on another origin (including another *.danwils.com
site, which SameSite=Lax would treat as same-site) cannot forge a write.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import SessionToken, User, utcnow

CSRF_HEADER = "x-webdoc"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REFRESH_EVERY = timedelta(days=1)
USERNAME_RE = re.compile(r"[^a-z0-9_.-]")


def cookie_name(settings: Settings) -> str:
    # __Host- binds the cookie to this exact host, https only, path /.
    return "__Host-wd_session" if settings.secure else "wd_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def unique_username(s: Session, wanted: str) -> str:
    base = USERNAME_RE.sub("", (wanted or "").strip().lower())[:60] or "user"
    name, n = base, 1
    while s.scalar(select(User.id).where(User.username == name)):
        n += 1
        name = f"{base}{n}"
    return name


def create_user(s: Session, wanted: str) -> User:
    """A new account with a first personal domain, so there is somewhere to write."""
    from . import domains
    from .activity import Actor
    user = User(username=unique_username(s, wanted))
    s.add(user)
    s.flush()
    domains.create(s, Actor(user), domains.personal_key(user), "Notes", "notes",
                   "General notes; rename or add domains as your taxonomy grows.")
    return user


def start_session(s: Session, user: User, settings: Settings, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(days=settings.session_ttl_days)
    s.add(SessionToken(token_hash=_hash(token), user_id=user.id, expires_at=expires))
    response.set_cookie(cookie_name(settings), token, max_age=settings.session_ttl_days * 86400,
                        path="/", httponly=True, secure=settings.secure, samesite="lax")


def end_session(s: Session, request: Request, settings: Settings, response: Response) -> None:
    token = request.cookies.get(cookie_name(settings))
    if token:
        row = s.get(SessionToken, _hash(token))
        if row is not None:
            s.delete(row)
    response.delete_cookie(cookie_name(settings), path="/", secure=settings.secure,
                           httponly=True, samesite="lax")


def user_for_request(s: Session, request: Request, settings: Settings) -> User | None:
    token = request.cookies.get(cookie_name(settings))
    if not token:
        return None
    row = s.get(SessionToken, _hash(token))
    if row is None:
        return None
    now = utcnow()
    if _aware(row.expires_at) <= now:
        s.delete(row)
        return None
    # Sliding expiry, written at most once a day.
    fresh = now + timedelta(days=settings.session_ttl_days)
    if fresh - _aware(row.expires_at) >= REFRESH_EVERY:
        row.expires_at = fresh
    return row.user


def require_user(s: Session, request: Request, settings: Settings) -> User:
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "missing X-Webdoc header")
    user = user_for_request(s, request, settings)
    if user is None:
        raise HTTPException(401, "not signed in")
    return user
