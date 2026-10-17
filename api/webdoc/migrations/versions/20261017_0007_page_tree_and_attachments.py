"""page tree and attachments

Documents get a parent (any page can have child pages) and a position among
their siblings. Projects are folded into the tree: each project becomes a
top-level page in its domain, titled after it with its description as the
body, and the project's documents move under it. Attachments are files
uploaded to a document.

Downgrade restores the schema only: projects come back empty and the tree is
dropped.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 10:00:00.000000
"""
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table('attachments',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('uploader_id', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploader_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('attachments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_attachments_document_id'), ['document_id'], unique=False)

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('position', sa.Integer(), server_default='0', nullable=False))
        batch_op.create_index(batch_op.f('ix_documents_parent_id'), ['parent_id'], unique=False)
        batch_op.create_foreign_key('fk_documents_parent_id', 'documents', ['parent_id'], ['id'],
                                    ondelete='SET NULL')

    with op.batch_alter_table('document_revisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.String(length=32), nullable=True))

    _projects_to_pages()

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_project_id'))
        batch_op.drop_column('project_id')

    with op.batch_alter_table('document_revisions', schema=None) as batch_op:
        batch_op.drop_column('project_id')

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_domain_id'))
    op.drop_table('projects')


def _projects_to_pages() -> None:
    conn = op.get_bind()
    docs = sa.table('documents', sa.column('id', sa.String), sa.column('domain_id', sa.String),
                    sa.column('parent_id', sa.String), sa.column('project_id', sa.String),
                    sa.column('position', sa.Integer), sa.column('kind', sa.String),
                    sa.column('title', sa.String), sa.column('body', sa.Text),
                    sa.column('fields', Json), sa.column('revision', sa.Integer),
                    sa.column('created_by', sa.String),
                    sa.column('created_at', sa.DateTime(timezone=True)),
                    sa.column('updated_at', sa.DateTime(timezone=True)),
                    sa.column('status', sa.String),
                    sa.column('published_at', sa.DateTime(timezone=True)),
                    sa.column('locked', sa.Boolean), sa.column('hidden_reason', sa.Text))
    revs = sa.table('document_revisions', sa.column('id', sa.String),
                    sa.column('document_id', sa.String), sa.column('revision', sa.Integer),
                    sa.column('title', sa.String), sa.column('body', sa.Text),
                    sa.column('tags', Json), sa.column('fields', Json),
                    sa.column('domain_id', sa.String), sa.column('parent_id', sa.String),
                    sa.column('project_id', sa.String), sa.column('actor_id', sa.String),
                    sa.column('client', sa.String), sa.column('source', sa.String),
                    sa.column('summary', sa.String),
                    sa.column('created_at', sa.DateTime(timezone=True)))
    proj = sa.table('projects', sa.column('id', sa.String), sa.column('domain_id', sa.String),
                    sa.column('slug', sa.String), sa.column('name', sa.String),
                    sa.column('description', sa.Text), sa.column('created_by', sa.String),
                    sa.column('created_at', sa.DateTime(timezone=True)))
    projects = conn.execute(sa.select(proj).order_by(proj.c.domain_id, proj.c.name)).all()
    now = datetime.now(timezone.utc)
    for p in projects:
        page = uuid.uuid4().hex
        created = p.created_at or now
        conn.execute(docs.insert().values(
            id=page, domain_id=p.domain_id, parent_id=None, position=0, kind='document',
            title=p.name, body=p.description or '', fields={}, revision=1,
            created_by=p.created_by, created_at=created, updated_at=now, status='published',
            published_at=created, locked=False, hidden_reason=''))
        conn.execute(revs.insert().values(
            id=uuid.uuid4().hex, document_id=page, revision=1, title=p.name,
            body=p.description or '', tags=[], fields={}, domain_id=p.domain_id, parent_id=None,
            actor_id=p.created_by, client='migration', source='import',
            summary=f'created from project {p.slug}', created_at=now))
        conn.execute(docs.update().where(docs.c.project_id == p.id).values(parent_id=page))
        conn.execute(revs.update().where(revs.c.project_id == p.id).values(parent_id=page))


def downgrade() -> None:
    op.create_table('projects',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('domain_id', 'slug')
    )
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_projects_domain_id'), ['domain_id'], unique=False)

    with op.batch_alter_table('document_revisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=32), nullable=True))
        batch_op.drop_column('parent_id')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_documents_project_id'), ['project_id'], unique=False)
        batch_op.create_foreign_key('fk_documents_project_id', 'projects', ['project_id'], ['id'],
                                    ondelete='SET NULL')
        batch_op.drop_constraint('fk_documents_parent_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_documents_parent_id'))
        batch_op.drop_column('position')
        batch_op.drop_column('parent_id')

    with op.batch_alter_table('attachments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_attachments_document_id'))
    op.drop_table('attachments')
