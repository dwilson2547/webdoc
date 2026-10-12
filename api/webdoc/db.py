"""Engine and sessions."""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str, broker=None):
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        self.engine = create_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite"):
            # SQLite ignores foreign keys (and so ON DELETE CASCADE) unless asked.
            event.listen(self.engine, "connect",
                         lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        self._session = sessionmaker(self.engine, expire_on_commit=False)
        # Live updates (events.py) go out only once their write has committed.
        self.broker = broker
        event.listen(self._session, "after_commit", self._publish)
        event.listen(self._session, "after_rollback", lambda s: s.info.pop("wd_events", None))

    def _publish(self, s: Session) -> None:
        pending = s.info.pop("wd_events", None)
        if pending and self.broker is not None:
            for doc_id, ev in pending:
                self.broker.publish(doc_id, ev)

    def migrate(self) -> None:
        """Bring the schema to the latest migration."""
        cfg = Config()
        cfg.set_main_option("script_location", str(files("webdoc") / "migrations"))
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    @contextmanager
    def session(self):
        s: Session = self._session()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()
