"""contributions

Public domains can accept contributions: documents that start as drafts only
their author sees, replies, confirmations and reports (design §12). Existing
documents are published, with their creation time as published_at.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12 01:37:27.942090
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('replies',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('author_id', sa.String(length=32), nullable=True),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('hidden_reason', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('replies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_replies_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_replies_document_id'), ['document_id'], unique=False)

    op.create_table('confirmations',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('reply_id', sa.String(length=32), nullable=True),
    sa.Column('target', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('environment', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reply_id'], ['replies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'user_id', 'target')
    )
    with op.batch_alter_table('confirmations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_confirmations_document_id'), ['document_id'], unique=False)

    op.create_table('reports',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('reply_id', sa.String(length=32), nullable=True),
    sa.Column('reporter_id', sa.String(length=32), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_by', sa.String(length=32), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('note', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reply_id'], ['replies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reporter_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reports_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_reports_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_reports_status'), ['status'], unique=False)

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=16), server_default='published', nullable=False))
        batch_op.add_column(sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('locked', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('hidden_reason', sa.Text(), server_default='', nullable=False))
        batch_op.add_column(sa.Column('merged_into_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('accepted_reply_id', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_documents_status'), ['status'], unique=False)
    op.execute("UPDATE documents SET published_at = created_at")

    with op.batch_alter_table('domains', schema=None) as batch_op:
        batch_op.add_column(sa.Column('accepts_contributions', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('contribution_types', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default='[]', nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('domains', schema=None) as batch_op:
        batch_op.drop_column('contribution_types')
        batch_op.drop_column('accepts_contributions')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_status'))
        batch_op.drop_column('accepted_reply_id')
        batch_op.drop_column('merged_into_id')
        batch_op.drop_column('hidden_reason')
        batch_op.drop_column('locked')
        batch_op.drop_column('published_at')
        batch_op.drop_column('status')

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reports_status'))
        batch_op.drop_index(batch_op.f('ix_reports_domain_id'))
        batch_op.drop_index(batch_op.f('ix_reports_document_id'))

    op.drop_table('reports')
    with op.batch_alter_table('confirmations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_confirmations_document_id'))

    op.drop_table('confirmations')
    with op.batch_alter_table('replies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_replies_document_id'))
        batch_op.drop_index(batch_op.f('ix_replies_author_id'))

    op.drop_table('replies')
