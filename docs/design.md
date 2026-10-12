# webdoc — design

Status markers (CONVENTIONS §10): ✅ verified · 📐 planned (a decision, not yet built) ·
⚠ unverified (believed, not checked — confirm before building on it).

## 1. Goals

1. A multi-user markdown knowledge base for **notes** and **documents**, readable and writable by
   people in a browser and by agents over an OAuth MCP server — the same way nodewright works — so
   web chats (claude.ai) and local agents (Claude Code) share one store. 📐
2. Semantic + keyword search, scoped by what the caller can see. 📐
3. A recent-activity log that agents can query ("what's new since I last asked"). 📐
4. User-defined **document types** (templates with per-section instructions), so "add this to webdoc
   as an architecture document" pulls the user's architecture template. 📐
5. **Closed domain taxonomies**: each owner (user, later org) defines its own domains; agents can
   only propose changes, never make them. 📐
6. **Public and org domains with contribution templates** — a structured, agent-friendly Stack
   Overflow (or an internal equivalent): problems and their resolutions written to a template,
   searchable before anyone posts a duplicate. Not limited to code fixes. 📐

Non-goals for v1: real-time co-editing (CRDT), WYSIWYG-first editing, rendering anything other than
markdown, any paid embedding API.

## 2. Positioning

webdoc is a **product**, like nodewright, not the workspace's agent knowledge tier
(CONVENTIONS §5 still holds for repo knowledge). How local agents use it — e.g. as a transport for
decisions made in web chats — is controlled by skills on the local side, not hard-coded here. The
server stays workflow-neutral: it stores, searches, logs and enforces taxonomy and templates.

## 3. Data model 📐

```
Owner (Platform | Org* | User)              * orgs land in a later phase (§14)
 │                Platform owns public domains, Org owns org domains, User owns personal ones
 ├─ Domain        closed taxonomy; visibility private | members | public
 │   ├─ Project   optional, flat, agent-creatable
 │   └─ allowed document types (by template)
 ├─ Template      a document type: skeleton + section guidance + required fields
 └─ Document      kind note | document | contribution
      ├─ exactly one Domain, optional Project, free Tags
      ├─ markdown body (source of truth) + frontmatter
      ├─ Revisions (every write)
      ├─ Replies   (contributions only, templated: "also fixed by", "didn't work", …)
      └─ Confirmations ("this worked for me" — the quality signal for contributions)
Event   append-only activity log (actor, client, action, target, summary, time)
Chunk   heading-scoped text + embedding + tsvector, for search
```

- **No folders.** Domain (required, closed) + project (optional, flat) + tags (open) + saved views
  replace them. `[[links]]` between documents with backlinks give the cross-references folders can't.
- **Notes vs documents**: one table, `kind` column. Notes are atomic and untemplated; documents are
  templated, sectioned, long-lived; contributions are documents in a domain that accepts outside
  contributions.
- **Tags** are open, but the server normalises (case, whitespace, plural) and returns near matches of
  existing tags when an agent introduces a new one, so tag sprawl is visible.
- **Storage**: markdown is the source of truth so a domain exports as a folder of `.md` files with
  frontmatter (Obsidian-openable), the way nodewright keeps JSON Canvas.

## 4. Roles and permissions 📐

Everyone who writes signs in through the cluster's authentik, outside contributors included;
signed-out visitors can read public domains (below). Four roles:

| Role | Scope | Can |
|---|---|---|
| **admin** | platform | Everything: create/edit public domains and their templates, grant any role, moderate anything, approve proposals for public domains. |
| **moderator** | one org, or one public domain | Admin *within that scope*. Org: manage org members and their roles, org domains and templates, approve org proposals, hide/lock/merge org content. Public domain: grant editor on it, manage its templates, hide/lock/merge its content (creating or deleting public domains stays admin-only). |
| **editor** | org, or a shared domain | Create and edit documents and notes; create projects and tags. |
| **reader** | org, or a shared domain | Read and search only. |

- **Personal domains**: the owning user has full control over them and may share a domain with other
  users as reader or editor.
- **Public domains**: anyone, signed in or not, can browse, read and search them in the web UI.
  Every signed-in user may also *contribute* — publish their own contributions, reply, confirm.
  Contributing is not editing: changing someone else's contribution needs editor on that domain
  (granted by an admin or that domain's moderator). Admins moderate all public domains and may
  appoint moderators for individual ones.
- **Signed-out visitors** get the public home section, public documents and public-only search
  (`search` and `find`), rate-limited per IP since each semantic query costs a CPU embedding. They
  cannot contribute, reply, confirm, or see activity beyond public documents. The MCP server always
  requires sign-in.
- **Agents** act as the user who connected them, with that user's role and never more. The domain
  rule in §5 binds agents of admins and moderators too: they can propose, not approve.
- One role per user per scope; the effective permission on a document is the strongest grant that
  covers its domain.

## 5. Domain governance 📐

The rule — domains change only with explicit user permission — is enforced by the tool surface, not
by instructions:

- The MCP server has **no** create/rename/delete domain tool. It has
  `propose_domain(name, description, rationale)` (and `propose_domain_change` for edits), which
  creates a pending request that whoever controls the taxonomy approves or rejects in the web UI:
  the user for personal domains, an org moderator for org domains, an admin for public domains.
  Approval is a human action in the browser, never an MCP call.
- A write naming an unknown domain fails with the list of valid domains and a hint to propose one.
- Templates follow the same pattern: agents may `propose_template`; only the owner publishes.
- Projects and tags are open to agents (they are the low-stakes axes).

## 6. Templates (document types) 📐

A template has: `type` (e.g. `architecture`), a markdown skeleton, **guidance** per section (shown to
agents and in the editor's side panel, never stored in the document), required frontmatter fields,
default domain/tags, and a version.

- `get_template(type)` returns skeleton + guidance + required fields.
- `create_document(type=…)` validates required sections and fields; it rejects with the missing
  list rather than silently accepting a shape the template doesn't allow.
- Documents record `template_type` + `template_version`; a template change does not rewrite existing
  documents.
- Resolution order when an agent asks for a type: the domain's own template → the owner's → built-in.
- Public/org domains declare which types they accept; the contribution template is how a forum's
  quality bar is expressed (e.g. an *issue resolution* type modelled on the `issue-documentation`
  skill: symptoms, environment, root cause, resolution, verification).

## 7. Editing and concurrency 📐

Like nodewright: edits are operations applied to the latest revision, so an agent never overwrites
concurrent human work.

- Ops: `replace_section(heading_path)`, `append_to_section`, `insert_section`, `delete_section`,
  `set_frontmatter`, `replace_body` (whole-document; requires the caller's base revision and fails on
  mismatch).
- Short per-document edit lease (copied from nodewright `leases.py`) while a human has the editor open
  or an agent holds `lock_document`.
- Every write is a revision; named versions and restore as in nodewright §14.
- Open browser tabs update live over SSE (nodewright `events.py`).

## 8. Search 📐

- **Embeddings run locally, in-process**: fastembed (ONNX, CPU) with
  `sentence-transformers/all-MiniLM-L6-v2` — the model doc-indexer settled on (its README records why
  bge-small was dropped). No external API, no GPU, no separate embedding service to deploy.
  ⚠ unverified: CPU embed throughput in the cluster pod is enough for write-time embedding; fall back
  to a background worker if not.
- Chunking by markdown heading; re-embed only chunks whose text changed.
- **Hybrid**: pgvector cosine + Postgres full-text (`tsvector`), merged by reciprocal rank fusion.
  ⚠ unverified: pgvector is available on the Postgres instance webdoc will use (dan-wiki uses
  pgvector, so some instance has it). SQLite dev mode does brute-force cosine in Python.
- **`find`: strict literal lookup** alongside semantic `search` — whole-word or exact-phrase match
  over document text (Postgres `~*` with word boundaries over the stored markdown, or `LIKE` for
  phrases), no embeddings involved, so it finds every occurrence of an identifier, error string or
  part number that semantic search would blur. Same filters and permission scoping as `search`.
- Permission filtering happens in the query (owner, membership, domain visibility), never after.
- Both `search` and `find` cover **all sources the caller can read** by default — public, org and
  personal — and every result is labelled with its source and domain so results never mix
  silently. Filters narrow it (§11).

## 9. Activity log 📐

- `Event` rows for every create/update/delete/propose/approve, with actor (user), client (OAuth client
  name, e.g. "Claude Code", "claude.ai", or "web") and a short summary.
- `whats_new(since=None, domain=None)`: with no `since`, returns changes since **this client's** last
  `whats_new` call (cursor stored per user+client server-side). Agents call it on request; nothing is
  injected at session start.
- Web UI: a feed per user and per domain.

## 10. MCP tools (v1) 📐

| Area | Tools |
|---|---|
| Orient | `list_domains`, `list_projects`, `list_templates`, `get_template`, `get_style_guide` |
| Read | `search` (semantic), `find` (literal), `get_document` (optionally one section), `list_documents` (filters), `get_backlinks` |
| Write | `create_document`, `create_note`, `edit_document` (ops, §7), `set_tags`, `create_project`, `lock_document`, `unlock_document` |
| History | `list_versions`, `save_version`, `whats_new` |
| Governance | `propose_domain`, `propose_domain_change`, `propose_template` |
| Contributions | `search` with `scope=public`, `draft_contribution`, `reply`, `confirm` |

Server instructions tell agents to call `list_domains` before writing and `search` before creating, to
avoid duplicates.

## 11. Web UI 📐

- **Home** has three sections, in this order: **Public** domains, **Org** domains (one group per org
  the user belongs to), **Personal** domains. Each domain card shows its description, document
  count and last activity; opening a domain lists its projects, documents and notes.
- **Central search**, always in the header, queries every source the user can read. Results show
  source (public / org name / personal), domain, project, type and a matched snippet.
  - Mode toggle: **Semantic** (default) or **Find** (literal, §8).
  - Filters: source, org, domain, project, kind (note / document / contribution), document type,
    tags, author, updated date range.
- Document view: rendered markdown with the Toast UI editor in markdown mode on edit, template
  guidance in a side panel, backlinks, version history.
- Activity feed per user and per domain; the proposal approval queue for whoever controls a
  taxonomy (§5); admin and org-moderator screens for roles (§4).

## 12. Public and org contribution domains 📐

The automated-Stack-Overflow idea. A domain with visibility `public` (or `members` for an org)
declares accepted contribution types and moderators.

- **Search before post**: `draft_contribution` runs a similarity search first and returns likely
  duplicates; the agent should reply or confirm instead of posting a duplicate.
- **Human gate on publishing.** An agent can only *draft*; a person publishes from the web UI after
  reading it. The main risk is an agent pasting private context (hostnames, internal paths, private
  project names, secrets) into a public post — the gate is the control. A server-side secret scan
  on publish is a second layer.
- **Quality signal**: confirmations ("worked for me", with optional environment) rather than up/down
  votes; the author can mark a reply as the resolution.
- **Moderation**: domain moderators can hide, lock, or merge duplicates; users can report.
- **Rate limits** per user and per OAuth client on drafts and replies.
- Outside contributors sign in through authentik like everyone else (§4).

## 13. Architecture 📐

Same shape as nodewright, and much of the code is copied from it rather than shared as a library (it
can be extracted later once both copies have settled):

- `api/` — FastAPI, SQLAlchemy, Alembic, Postgres (SQLite locally). Copied from nodewright:
  `oidc.py`, `auth.py`, `oauth.py` (MCP OAuth), `tokens.py` (personal access tokens), `members.py`,
  `shares.py`, `leases.py`, `events.py`, `storage.py` + `attachments.py`, `versions.py` patterns.
  New: `domains.py` (governance), `templates.py`, `sections.py` (markdown section ops),
  `search.py` (chunking, fastembed, hybrid query), `activity.py`, `contributions.py`, `mcp_server.py`.
- `ui/` — Vite + React. Editor: **Toast UI Editor**, as in dan-wiki
  (`ai/dan-wiki/frontend/src/components/MarkdownEditor.jsx` ✅ uses `@toast-ui/react-editor` 3.2.x on
  React 18), in markdown mode with live preview; the WYSIWYG tab is hidden. ⚠ unverified: the React
  wrapper works on React 19 (nodewright is on 19) — if not, wrap the vanilla `@toast-ui/editor`
  directly, which is a small component. Prism syntax highlighting carried over from dan-wiki.
- `helm/webdoc/` — chart deployed by Argo; served at `webdoc.danwils.com` behind authentik, like
  nodewright.
- Embedding model weights are baked into the image so pods don't download them at start.

## 14. Phases 📐

1. **Core**: scaffold (copy nodewright auth/OAuth/tokens), roles model with admin + per-domain
   reader/editor grants, domains (UI-only creation), projects, tags, notes + documents, revisions,
   Toast UI editor, home page (personal section first), markdown export. MCP read/write tools.
2. **Templates + governance**: template CRUD in UI, `get_template`/typed `create_document`
   validation, `propose_*` tools and the approval queue.
3. **Search + activity**: chunking, fastembed, hybrid `search`, literal `find`, central search UI
   with filters, events, `whats_new`, feed UI.
4. **Sharing**: sharing personal domains (reader/editor), share links, live updates over SSE, leases.
5. **Contributions**: public domains (home page Public section), drafts → human publish, replies, confirmations, moderation,
   rate limits, secret scan.
6. **Orgs**: orgs with moderators, their own closed taxonomies and member-only contribution
   domains; home page Org section (aligned with nodewright's orgs/roles work).

## 15. Open questions

- Should a public contribution keep a link back to a private source document, visible only to the
  author?
- Does an org domain inherit templates from the org, or only from itself?
