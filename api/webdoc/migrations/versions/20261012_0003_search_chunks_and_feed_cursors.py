"""search chunks and feed cursors

On Postgres the embedding column is pgvector's vector(384) with an HNSW
cosine index, and chunk content gets a GIN full-text index (design §8). SQLite
stores embeddings as float32 bytes and searches in Python.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12 00:48:12.089343
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    pg = op.get_bind().dialect.name == "postgresql"
    if pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('feed_cursors',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'client')
    )
    op.create_table('document_chunks',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('ord', sa.Integer(), nullable=False),
    sa.Column('heading', sa.String(length=500), nullable=False),
    sa.Column('line', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    *([] if pg else [sa.Column('embedding', sa.LargeBinary(), nullable=True)]),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('document_chunks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_chunks_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_chunks_domain_id'), ['domain_id'], unique=False)
    if pg:
        op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(384)")
        op.execute("CREATE INDEX ix_document_chunks_embedding ON document_chunks "
                   "USING hnsw (embedding vector_cosine_ops)")
        op.execute("CREATE INDEX ix_document_chunks_fts ON document_chunks "
                   "USING gin (to_tsvector('english', content))")

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('indexed_revision', sa.Integer(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('indexed_revision')

    with op.batch_alter_table('document_chunks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_chunks_domain_id'))
        batch_op.drop_index(batch_op.f('ix_document_chunks_document_id'))

    op.drop_table('document_chunks')
    op.drop_table('feed_cursors')
