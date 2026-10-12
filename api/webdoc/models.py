"""Database schema (design §3).

Changes go through Alembic (webdoc/migrations; the app upgrades itself at
start-up, db.Database.migrate). After editing a model, from api/:
  DATABASE_URL=sqlite:////tmp/wd-mig.db alembic upgrade head
  DATABASE_URL=sqlite:////tmp/wd-mig.db alembic revision --autogenerate --rev-id NNNN -m "..."
then read the generated file before committing it.

Accounts, sessions, tokens and OAuth tables are copied from nodewright.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    """Platform admin (design §4); set from the authentik admin group at sign-in."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    identities: Mapped[list["UserIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    sessions: Mapped[list["SessionToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)


class UserIdentity(Base):
    """An authentik identity (issuer + sub) linked to a local user."""
    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="identities")


class SessionToken(Base):
    """A browser session. Only the SHA-256 of the token is stored, so a
    database leak hands out nothing usable, and signing out is a row delete."""
    __tablename__ = "session_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class AccessToken(Base):
    """A personal access token for MCP clients. Stored hashed like sessions;
    `prefix` is the visible start of the token so a user can tell them apart."""
    __tablename__ = "access_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class StyleGuide(Base):
    """A user's "how I like documents written" notes, handed to agents."""
    __tablename__ = "style_guides"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthClient(Base):
    """An MCP client that registered itself (RFC 7591). `info` is the SDK's
    OAuthClientInformationFull as JSON, secret included: the SDK compares it
    in plain text, and PKCE is what actually protects the code exchange."""
    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    info: Mapped[dict] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthRequest(Base):
    """An /authorize request waiting on the consent page. `user_id` and
    `nonce` are filled when the page is shown, so only that signed-in user's
    form submission can approve it."""
    __tablename__ = "oauth_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"))
    params: Mapped[dict] = mapped_column(Json)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    params: Mapped[dict] = mapped_column(Json)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthGrant(Base):
    """One connected app for one user: the current access and refresh token,
    rotated in place on refresh. Deleting the row disconnects the app."""
    __tablename__ = "oauth_grants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), index=True)
    client_name: Mapped[str] = mapped_column(String(100))
    scopes: Mapped[str] = mapped_column(String(200), default="")
    resource: Mapped[str | None] = mapped_column(String(300), nullable=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


# --- knowledge base -----------------------------------------------------------

PLATFORM = "platform"


class Domain(Base):
    """One entry in an owner's closed taxonomy (design §3, §5). `owner_key` is
    "platform" for public domains, "user:<id>" for personal ones (and
    "org:<id>" once orgs exist); slugs are unique per owner. Domains are
    created and changed only from the web UI, never over MCP."""
    __tablename__ = "domains"
    __table_args__ = (UniqueConstraint("owner_key", "slug"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_key: Mapped[str] = mapped_column(String(48), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    """public | personal (| org later); follows from owner_key."""
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    owner: Mapped[User | None] = relationship()


class DomainRevision(Base):
    """Every change to a domain's name, slug or description (design §9.2)."""
    __tablename__ = "domain_revisions"
    __table_args__ = (UniqueConstraint("domain_id", "revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainGrant(Base):
    """A role for someone other than the owner on one domain (design §4):
    reader or editor on a personal or public domain, or moderator on a public
    one. Owners and admins have no row; access.py derives their role."""
    __tablename__ = "domain_grants"
    __table_args__ = (UniqueConstraint("domain_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    granted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])


class Project(Base):
    """Optional, flat grouping inside a domain; agents may create them."""
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("domain_id", "slug"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Document(Base):
    """A note or a document. `body` is markdown without frontmatter; title,
    tags and fields live in columns and become frontmatter on export.
    `revision` increments on every write and every write is kept in
    document_revisions. A deleted document is in the trash (deleted_at set)
    until purged."""
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    """note | document (| contribution later)."""
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[dict] = mapped_column(Json, default=dict)
    """Template-required frontmatter fields (design §6)."""
    template_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    domain: Mapped[Domain] = relationship()
    project: Mapped[Project | None] = relationship()
    tag_rows: Mapped[list["DocumentTag"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="DocumentTag.tag")

    @property
    def tags(self) -> list[str]:
        return [t.tag for t in self.tag_rows]


class DocumentTag(Base):
    __tablename__ = "document_tags"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class DocumentRevision(Base):
    """One write to a document (design §9.1): the full state after it, who made
    it and through which client, and what it touched."""
    __tablename__ = "document_revisions"
    __table_args__ = (UniqueConstraint("document_id", "revision"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(Json, default=list)
    fields: Mapped[dict] = mapped_column(Json, default=dict)
    domain_id: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    client: Mapped[str] = mapped_column(String(100), default="web")
    """"web", an OAuth client's name, or "token:<name>"."""
    source: Mapped[str] = mapped_column(String(16), default="ui")
    """ui | mcp | restore | import"""
    summary: Mapped[str] = mapped_column(String(300), default="")
    base_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    """A named version."""
    restored_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped[User | None] = relationship()


class Event(Base):
    """Append-only activity log (design §9.2, §9.4). No foreign keys on the
    targets, so the record outlives what it describes."""
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_domain_at", "domain_id", "at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    client: Mapped[str] = mapped_column(String(100), default="web")
    action: Mapped[str] = mapped_column(String(40))
    domain_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_kind: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(String(300), default="")
