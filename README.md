---
tier: system
domain: apps
---

# webdoc

A multi-user markdown knowledge base for notes and documents, shared between people in a browser and
agents over an OAuth MCP server, so web chats and local agents read and write the same store. Each
owner keeps a closed taxonomy of domains that agents can use but not change; documents carry
attributed revision history with diffs, named versions and a trash.

Status: **0.3.0** at https://webdoc.danwils.com (phases 1-3). Domains (personal and public),
projects, tags, reader/editor/moderator grants with admins from an authentik group, signed-out
reading of public domains, notes and documents with a Toast UI markdown editor, section-level
edits for agents, history (who and through which app, diffs, named versions, restore), trash,
markdown export, document types with per-section guidance, and a proposal queue through which
agents ask for new domains and types, semantic search and literal find across every readable
source (embeddings computed in-process), and an activity feed with `whats_new` for agents.
Design and roadmap: [`docs/design.md`](docs/design.md).

## Connect an agent

- Claude Code: `claude mcp add --transport http webdoc https://webdoc.danwils.com/mcp`
- `mcpServers`-style clients: `{"mcpServers": {"webdoc": {"type": "http", "url": "https://webdoc.danwils.com/mcp"}}}`

On first use the client opens a browser to sign in (authentik) and **Allow** it. Clients that can't
do that use a personal access token from **Agents**, sent as `Authorization: Bearer wdp_…`.

## Layout

- `api/` — FastAPI (`webdoc/`), SQLAlchemy + Alembic on Postgres (SQLite locally).
  `access.py` every permission decision, `domains.py` domains/projects/grants, `documents.py`
  documents and their history, `sections.py` markdown section edits, `activity.py` the event log,
  `export.py` markdown export, `templates.py` document types, `proposals.py` the proposal
  queue, `mcp_server.py` the MCP tools. Sign-in (`oidc.py`, `auth.py`), MCP
  OAuth (`oauth.py`) and tokens (`tokens.py`) are copied from nodewright.
- `ui/` — Vite + React 19, Toast UI Editor (`src/markdown.tsx`). Built into the image and served by the API.
- `helm/webdoc/` — chart for Argo.

## Develop

```bash
cd api && uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements.txt pytest
.venv/bin/python -m pytest -q
DEV_LOGIN=1 .venv/bin/uvicorn --factory webdoc.main:create_app --port 8080   # SQLite, dev sign-in
cd ../ui && npm ci && npm run dev    # proxies /api to :8080
```

`DEV_LOGIN` is refused when `PUBLIC_URL` is https; its sign-in form has an admin checkbox. Other
settings: `api/webdoc/config.py`. After changing a model, see the header of `api/webdoc/models.py`
for generating a migration; test new ones against a throwaway `postgres:16-alpine`.

## Release

Bump `api/webdoc/__init__.py` and `helm/webdoc/{Chart,values}.yaml` together. CI (`.github/workflows/ci.yml`)
runs the tests and UI build on every push and, on `main`, publishes the image.
