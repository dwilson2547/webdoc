"""Per-user writing guide handed to agents: how this user likes documents written."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import StyleGuide, User

MAX_CHARS = 20_000

DEFAULT = """\
# Writing guide (webdoc default)

- Write for a reader who arrives cold: lead each document with what it is and why it
  exists, then the detail.
- One topic per note. A note that grows past a screen or two wants to be a document.
- Prefer short sections under clear headings; agents and people edit by section.
- Mark anything you have not verified as unverified rather than stating it as fact.
- Search before creating, and add to an existing document rather than starting a
  near-duplicate.
- Don't rewrite or delete what the user wrote unless asked; edit the section you mean to
  change, not the whole document.
"""


def get(s: Session, user: User) -> tuple[str, bool]:
    row = s.get(StyleGuide, user.id)
    return (row.text, False) if row is not None else (DEFAULT, True)
