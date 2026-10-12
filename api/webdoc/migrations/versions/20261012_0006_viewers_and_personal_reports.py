"""viewers and personal reports

Users get is_editor (design §4): new accounts start as viewers unless the
authentik editor group says otherwise. Everyone who exists now becomes an
editor, so nobody loses access until their next sign-in syncs it from their
groups. Reports record the revision that was reported.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-12 09:13:50.381863
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('revision', sa.Integer(), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_editor', sa.Boolean(), server_default=sa.true(), nullable=False))



    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('is_editor', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('is_editor')

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('revision')

