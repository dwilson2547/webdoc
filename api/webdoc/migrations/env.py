"""Alembic environment. Run from the app at start-up (db.Database.migrate),
which hands over its connection; or from the CLI in api/ for authoring:
DATABASE_URL=sqlite:////tmp/x.db alembic revision --autogenerate -m "..."."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

from webdoc.models import Base

config = context.config
target_metadata = Base.metadata


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite",
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    with create_engine(url).connect() as conn:
        run(conn)
