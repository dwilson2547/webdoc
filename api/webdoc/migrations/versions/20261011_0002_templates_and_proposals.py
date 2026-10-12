"""templates and proposals

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11 23:12:37.993854
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('proposals',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('owner_key', sa.String(length=48), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.String(length=32), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('proposed_by', sa.String(length=32), nullable=True),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('decided_by', sa.String(length=32), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=False),
    sa.Column('result_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['proposed_by'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_proposals_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index('ix_proposals_owner_status', ['owner_key', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_proposals_proposed_by'), ['proposed_by'], unique=False)

    op.create_table('templates',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('owner_key', sa.String(length=48), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=True),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('skeleton', sa.Text(), nullable=False),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('default_tags', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_templates_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_templates_owner_key'), ['owner_key'], unique=False)

    op.create_table('template_revisions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('template_id', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('skeleton', sa.Text(), nullable=False),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('default_tags', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('actor_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['template_id'], ['templates.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('template_id', 'version')
    )
    with op.batch_alter_table('template_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_template_revisions_template_id'), ['template_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('template_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_template_revisions_template_id'))

    op.drop_table('template_revisions')
    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_templates_owner_key'))
        batch_op.drop_index(batch_op.f('ix_templates_domain_id'))

    op.drop_table('templates')
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_proposals_proposed_by'))
        batch_op.drop_index('ix_proposals_owner_status')
        batch_op.drop_index(batch_op.f('ix_proposals_domain_id'))

    op.drop_table('proposals')
