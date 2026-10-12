"""leases and share links

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12 01:19:38.424338
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('document_leases',
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('holder', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('label', sa.String(length=160), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id')
    )
    op.create_table('share_links',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('token', sa.String(length=64), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('views', sa.Integer(), nullable=False),
    sa.Column('last_viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('share_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_share_links_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_share_links_token'), ['token'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('share_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_share_links_token'))
        batch_op.drop_index(batch_op.f('ix_share_links_document_id'))

    op.drop_table('share_links')
    op.drop_table('document_leases')
