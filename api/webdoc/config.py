"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./webdoc.db"
    public_url: str = "http://localhost:8080"
    """Where the app is reached. The OIDC redirect URI derives from it, and an
    https URL turns on Secure / __Host- cookies."""
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    admin_group: str | None = None
    """authentik group whose members are webdoc admins (design §4). Checked at
    every sign-in, so removing someone from the group demotes them next time."""
    session_ttl_days: int = 30
    max_document_bytes: int = 1024 * 1024
    static_dir: str | None = None
    dev_login: bool = False
    """Local-only sign-in without authentik, for development and UI tests.
    Refused when public_url is https, so it can't be switched on in prod."""

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def dev_login_enabled(self) -> bool:
        return self.dev_login and not self.secure

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        return cls(
            database_url=env.get("DATABASE_URL") or cls.database_url,
            public_url=(env.get("PUBLIC_URL") or cls.public_url).rstrip("/"),
            # authentik's issuer ends in "/" and the ID token's iss must match it
            # exactly, so it is kept as configured.
            oidc_issuer=(env.get("OIDC_ISSUER") or "").strip() or None,
            oidc_client_id=env.get("OIDC_CLIENT_ID") or None,
            oidc_client_secret=env.get("OIDC_CLIENT_SECRET") or None,
            admin_group=(env.get("ADMIN_GROUP") or "").strip() or None,
            session_ttl_days=int(env.get("SESSION_TTL_DAYS") or cls.session_ttl_days),
            max_document_bytes=int(env.get("MAX_DOCUMENT_BYTES") or cls.max_document_bytes),
            static_dir=env.get("STATIC_DIR") or None,
            dev_login=(env.get("DEV_LOGIN") or "") == "1",
        )
