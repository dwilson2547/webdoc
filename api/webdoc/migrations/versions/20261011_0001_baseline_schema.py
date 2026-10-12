"""baseline schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-11 22:13:45.517191
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('events',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.String(length=32), nullable=True),
    sa.Column('actor_name', sa.String(length=64), nullable=False),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=True),
    sa.Column('target_kind', sa.String(length=20), nullable=False),
    sa.Column('target_id', sa.String(length=32), nullable=False),
    sa.Column('summary', sa.String(length=300), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_actor_id'), ['actor_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_events_at'), ['at'], unique=False)
        batch_op.create_index('ix_events_domain_at', ['domain_id', 'at'], unique=False)

    op.create_table('oauth_clients',
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('info', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('client_id')
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_username'), ['username'], unique=True)

    op.create_table('access_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('access_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_access_tokens_token_hash'), ['token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_access_tokens_user_id'), ['user_id'], unique=False)

    op.create_table('domains',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('owner_key', sa.String(length=48), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('owner_user_id', sa.String(length=32), nullable=True),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_key', 'slug')
    )
    with op.batch_alter_table('domains', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_domains_owner_key'), ['owner_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_domains_owner_user_id'), ['owner_user_id'], unique=False)

    op.create_table('oauth_codes',
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('code_hash')
    )
    op.create_table('oauth_grants',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('client_name', sa.String(length=100), nullable=False),
    sa.Column('scopes', sa.String(length=200), nullable=False),
    sa.Column('resource', sa.String(length=300), nullable=True),
    sa.Column('access_hash', sa.String(length=64), nullable=False),
    sa.Column('access_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('refresh_hash', sa.String(length=64), nullable=False),
    sa.Column('refresh_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('oauth_grants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_oauth_grants_access_hash'), ['access_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_oauth_grants_client_id'), ['client_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_oauth_grants_refresh_hash'), ['refresh_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_oauth_grants_user_id'), ['user_id'], unique=False)

    op.create_table('oauth_requests',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('nonce', sa.String(length=64), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('session_tokens',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash')
    )
    with op.batch_alter_table('session_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_session_tokens_user_id'), ['user_id'], unique=False)

    op.create_table('style_guides',
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('user_identities',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('issuer', sa.String(length=255), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('issuer', 'subject')
    )
    with op.batch_alter_table('user_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_identities_user_id'), ['user_id'], unique=False)

    op.create_table('domain_grants',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('granted_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('domain_id', 'user_id')
    )
    with op.batch_alter_table('domain_grants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_domain_grants_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_domain_grants_user_id'), ['user_id'], unique=False)

    op.create_table('domain_revisions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('domain_id', 'revision')
    )
    with op.batch_alter_table('domain_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_domain_revisions_domain_id'), ['domain_id'], unique=False)

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

    op.create_table('documents',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('project_id', sa.String(length=32), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('template_type', sa.String(length=64), nullable=True),
    sa.Column('template_version', sa.Integer(), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_by', sa.String(length=32), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['deleted_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['domain_id'], ['domains.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documents_domain_id'), ['domain_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_project_id'), ['project_id'], unique=False)

    op.create_table('document_revisions',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('domain_id', sa.String(length=32), nullable=False),
    sa.Column('project_id', sa.String(length=32), nullable=True),
    sa.Column('actor_id', sa.String(length=32), nullable=True),
    sa.Column('client', sa.String(length=100), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('summary', sa.String(length=300), nullable=False),
    sa.Column('base_revision', sa.Integer(), nullable=True),
    sa.Column('label', sa.String(length=200), nullable=True),
    sa.Column('restored_from', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'revision')
    )
    with op.batch_alter_table('document_revisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_revisions_document_id'), ['document_id'], unique=False)

    op.create_table('document_tags',
    sa.Column('document_id', sa.String(length=32), nullable=False),
    sa.Column('tag', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id', 'tag')
    )
    with op.batch_alter_table('document_tags', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_tags_tag'), ['tag'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('document_tags', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_tags_tag'))

    op.drop_table('document_tags')
    with op.batch_alter_table('document_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_revisions_document_id'))

    op.drop_table('document_revisions')
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_project_id'))
        batch_op.drop_index(batch_op.f('ix_documents_domain_id'))

    op.drop_table('documents')
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_domain_id'))

    op.drop_table('projects')
    with op.batch_alter_table('domain_revisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_domain_revisions_domain_id'))

    op.drop_table('domain_revisions')
    with op.batch_alter_table('domain_grants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_domain_grants_user_id'))
        batch_op.drop_index(batch_op.f('ix_domain_grants_domain_id'))

    op.drop_table('domain_grants')
    with op.batch_alter_table('user_identities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_identities_user_id'))

    op.drop_table('user_identities')
    op.drop_table('style_guides')
    with op.batch_alter_table('session_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_session_tokens_user_id'))

    op.drop_table('session_tokens')
    op.drop_table('oauth_requests')
    with op.batch_alter_table('oauth_grants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_oauth_grants_user_id'))
        batch_op.drop_index(batch_op.f('ix_oauth_grants_refresh_hash'))
        batch_op.drop_index(batch_op.f('ix_oauth_grants_client_id'))
        batch_op.drop_index(batch_op.f('ix_oauth_grants_access_hash'))

    op.drop_table('oauth_grants')
    op.drop_table('oauth_codes')
    with op.batch_alter_table('domains', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_domains_owner_user_id'))
        batch_op.drop_index(batch_op.f('ix_domains_owner_key'))

    op.drop_table('domains')
    with op.batch_alter_table('access_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_access_tokens_user_id'))
        batch_op.drop_index(batch_op.f('ix_access_tokens_token_hash'))

    op.drop_table('access_tokens')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_username'))

    op.drop_table('users')
    op.drop_table('oauth_clients')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_domain_at')
        batch_op.drop_index(batch_op.f('ix_events_at'))
        batch_op.drop_index(batch_op.f('ix_events_actor_id'))

    op.drop_table('events')
