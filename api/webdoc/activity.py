"""Who is acting, and the append-only activity log (design §9.4).

Every write in domains.py and documents.py goes through an Actor, so revisions
and events always record the user and the client (web UI, an OAuth app's
name, or a personal access token) that made them.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .models import Event, User


@dataclass(frozen=True)
class Actor:
    user: User
    client: str = "web"
    source: str = "ui"           # ui | mcp

    @property
    def label(self) -> str:
        return self.user.username if self.client == "web" else f"{self.user.username} via {self.client}"


def record(s: Session, actor: Actor, action: str, target_kind: str, target_id: str,
           summary: str = "", domain_id: str | None = None) -> None:
    s.add(Event(actor_id=actor.user.id, actor_name=actor.user.username, client=actor.client[:100],
                action=action, domain_id=domain_id, target_kind=target_kind,
                target_id=target_id, summary=summary[:300]))
