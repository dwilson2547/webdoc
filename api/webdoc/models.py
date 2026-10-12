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
from array import array
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String,
                        Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, UserDefinedType

Json = JSON().with_variant(JSONB(), "postgresql")
EMBED_DIM = 384


class PgVector(UserDefinedType):
    """pgvector's `vector(n)`, for the Postgres side of Embedding."""
    cache_ok = True

    def __init__(self, dim: int = EMBED_DIM):
        self.dim = dim

    def get_col_spec(self, **_kw) -> str:
        return f"vector({self.dim})"


class Embedding(TypeDecorator):
    """A list of floats: pgvector on Postgres (searched in SQL), float32
    bytes elsewhere (SQLite dev mode compares in Python, design §8)."""
    impl = LargeBinary
    cache_ok = True

    def __init__(self, dim: int = EMBED_DIM):
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PgVector(self.dim))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return "[" + ",".join(f"{float(x):.7g}" for x in value) + "]"
        return array("f", value).tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return [float(x) for x in str(value).strip("[]").split(",")]
        a = array("f")
        a.frombytes(value)
        return list(a)


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
    is_editor: Mapped[bool] = mapped_column(Boolean, default=False)
    """May create and write (design §4); set from the authentik editor group at
    sign-in. Everyone else is a viewer: they read, search and report, and every
    role they hold is capped at reader. Admins are editors too."""
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
    accepts_contributions: Mapped[bool] = mapped_column(Boolean, default=False,
                                                        server_default="0")
    """Public domains only (design §12): any signed-in user may draft and,
    after review, publish contributions here."""
    contribution_types: Mapped[list] = mapped_column(Json, default=list)
    """Document types a contribution must use; empty means any (or none)."""
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
    status: Mapped[str] = mapped_column(String(16), default="published",
                                        server_default="published", index=True)
    """published | draft (contributions until a person publishes them; only
    the author sees a draft) | hidden (by a moderator)."""
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    """Moderator lock on a contribution: no edits, replies or confirmations."""
    hidden_reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    merged_into_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """A contribution marked as a duplicate of another; search skips it."""
    accepted_reply_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    indexed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The revision the search chunks were built from; the indexer picks up
    documents where it differs from `revision` (indexer.py)."""

    domain: Mapped[Domain] = relationship()
    project: Mapped[Project | None] = relationship()
    author: Mapped[User | None] = relationship(foreign_keys=[created_by])
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


class DocumentChunk(Base):
    """One searchable piece of a document's current revision (design §8): a
    section, or part of a long one. `content` is what is embedded and
    full-text indexed: the title, the heading path, then the section text.
    `domain_id` is copied from the document so search can filter without a join
    to domains. Rebuilt by indexer.py whenever the document changes."""
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[str] = mapped_column(String(32), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(500), default="")
    """Heading path joined with " > "; empty for text before the first heading."""
    line: Mapped[int] = mapped_column(Integer, default=0)
    """Line of the body where the chunk starts."""
    content: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100), default="")
    embedding: Mapped[list | None] = mapped_column(Embedding(), nullable=True)


class DocumentLease(Base):
    """Who is editing a document right now (design §7): a person with the
    editor open, or an agent that called lock_document. Writes by anyone
    else are refused until it expires or is released (leases.py)."""
    __tablename__ = "document_leases"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    holder: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(8))            # ui | agent
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShareLink(Base):
    """A read-only link to one document for people without an account
    (design §14 phase 4). The token is the credential; revoking deletes it."""
    __tablename__ = "share_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    views: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creator: Mapped[User | None] = relationship()


class Reply(Base):
    """A reply to a contribution (design §12): another fix, a report that it
    didn't work, or a comment. Agents draft replies; a person publishes them.
    Published replies aren't edited, only withdrawn by their author or hidden
    by a moderator, so a thread reads as it happened."""
    __tablename__ = "replies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    client: Mapped[str] = mapped_column(String(100), default="web")
    kind: Mapped[str] = mapped_column(String(16))           # solution | didnt-work | comment
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    """draft | published | withdrawn | hidden"""
    hidden_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    author: Mapped[User | None] = relationship()


class Confirmation(Base):
    """"This worked for me" on a contribution or one of its replies: the
    quality signal instead of votes (design §12). One per person per target."""
    __tablename__ = "confirmations"
    __table_args__ = (UniqueConstraint("document_id", "user_id", "target"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    reply_id: Mapped[str | None] = mapped_column(
        ForeignKey("replies.id", ondelete="CASCADE"), nullable=True)
    target: Mapped[str] = mapped_column(String(32), default="")
    """The reply id, or "" for the contribution itself (NULLs aren't unique)."""
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    client: Mapped[str] = mapped_column(String(100), default="web")
    environment: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class Report(Base):
    """Someone flagging a contribution or reply for moderators (design §12)."""
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"),
                                           index=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    reply_id: Mapped[str | None] = mapped_column(
        ForeignKey("replies.id", ondelete="CASCADE"), nullable=True)
    reporter_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    """open | dismissed | actioned"""
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The document's revision when reported: what an admin reviews."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    reporter: Mapped[User | None] = relationship(foreign_keys=[reporter_id])


class FeedCursor(Base):
    """Where whats_new left off, per user and client (design §9.4)."""
    __tablename__ = "feed_cursors"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         primary_key=True)
    client: Mapped[str] = mapped_column(String(100), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Template(Base):
    """A document type (design §6): a markdown skeleton whose headings are the
    sections (guidance for each in <!-- comments -->, "(optional)" after a
    heading for a section that may be left out), the frontmatter fields it
    requires, and default tags. Owned like domains (`owner_key`); `domain_id`
    narrows it to one domain. Built-in types live in templates.py, not here."""
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_key: Mapped[str] = mapped_column(String(48), index=True)
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    skeleton: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[list] = mapped_column(Json, default=list)
    """[{name, description, required}]"""
    default_tags: Mapped[list] = mapped_column(Json, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TemplateRevision(Base):
    """Every version of a template (design §9.2)."""
    __tablename__ = "template_revisions"
    __table_args__ = (UniqueConstraint("template_id", "version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    skeleton: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[list] = mapped_column(Json, default=list)
    default_tags: Mapped[list] = mapped_column(Json, default=list)
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Proposal(Base):
    """An agent's (or person's) request to change a taxonomy or a template
    (design §5). Only a person approves, in the web UI; approving applies
    `payload`. kind: domain.create | domain.update | template.create |
    template.update. status: pending | approved | rejected | withdrawn."""
    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_owner_status", "owner_key", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    owner_key: Mapped[str] = mapped_column(String(48))
    """Whose taxonomy or templates it would change."""
    domain_id: Mapped[str | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=True, index=True)
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """The template being changed, for template.update."""
    payload: Mapped[dict] = mapped_column(Json, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    proposed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    client: Mapped[str] = mapped_column(String(100), default="web")
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    result_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """What approving created or changed (domain or template id)."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    proposer: Mapped[User | None] = relationship(foreign_keys=[proposed_by])
    decider: Mapped[User | None] = relationship(foreign_keys=[decided_by])
