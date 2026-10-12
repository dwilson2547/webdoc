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

## 4. Roles and permissions ✅ (0.1.0, `access.py`, `domains.py`; orgs 📐)

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
- **How admins are set** ✅: membership of an authentik group (`ADMIN_GROUP`, chart value
  `oidc.adminGroup`, default `webdoc-admins`), read from the ID token's `groups` claim at every
  sign-in, so removing someone from the group demotes them at their next sign-in. Verified 2026-10-11: the
  webdoc provider uses authentik's managed profile scope mapping, whose expression returns
  `groups` (the user's group names); group `webdoc-admins` comes from the blueprint
  (`infra/cluster-config/authentik/blueprints/webdoc.yaml`), membership by hand.
- **Admins and personal domains** ✅ (0.2.0): admins have no access to anyone's personal domains;
  their control covers public domains (and orgs, later). Reviewing a reported personal document is a
  separate flow, not built yet (§15).
- **Permissions as built** ✅: actions are read / write / delete (trash and restore) / purge /
  manage (rename, describe, delete the domain) / grants. Owner and admin: all. Moderator: read,
  write, delete, grants (reader and editor only). Editor: read, write, delete. Reader: read.
  Only an empty domain (trash included) can be deleted, so deleting one never takes documents
  with it.

## 5. Domain governance ✅ (0.2.0, `proposals.py`)

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
- As built ✅: proposals need a rationale, are capped at 25 pending per user, and appear on a
  Proposals page (with a count in the header) for whoever may decide: the owner for personal
  domains and templates, admins for public ones, and a public domain's moderators for that domain's
  templates. The approver may edit a new domain's name, slug or description before approving;
  approving applies the change as the approver. Rejections carry an optional note the agent reads
  with `list_proposals`; the proposer can withdraw a pending one. A template change proposal keeps
  the description, fields and default tags it doesn't mention.

## 6. Templates (document types) ✅ (0.2.0, `templates.py`; public-domain contribution types 📐 phase 5)

A template has: `type` (e.g. `architecture`), a markdown skeleton, **guidance** per section (shown to
agents and in the editor's side panel, never stored in the document), required frontmatter fields,
default tags, and a version.

- Skeleton format ✅: each heading is a section; an HTML comment under a heading is that section's
  guidance; a heading ending in "(optional)" may be left out, every other one is required. A new
  document starts from the skeleton with comments and markers stripped.
- Built-in types ✅: `architecture`, `decision`, `issue-resolution` (sections from the
  `issue-documentation` skill's template). A user's own template of the same type replaces the
  built-in in their domains; the Types page "Customize" starts from the built-in.
- After creation, a typed document may drift from its template; `get_document` (`template_check`)
  and the document page show what is missing rather than blocking edits.

- `get_template(type)` returns skeleton + guidance + required fields.
- `create_document(type=…)` validates required sections and fields; it rejects with the missing
  list rather than silently accepting a shape the template doesn't allow.
- Documents record `template_type` + `template_version`; a template change does not rewrite existing
  documents.
- Resolution order when an agent asks for a type: the domain's own template → the owner's → built-in.
- Public/org domains declare which types they accept; the contribution template is how a forum's
  quality bar is expressed (e.g. an *issue resolution* type modelled on the `issue-documentation`
  skill: symptoms, environment, root cause, resolution, verification).

## 7. Editing and concurrency ✅ (0.1.0, `sections.py`, `documents.py`; leases and SSE 0.4.0)

Like nodewright: edits are operations applied to the latest revision, so an agent never overwrites
concurrent human work.

- Ops ✅: `replace_section(heading_path)` (everything under the heading, subsections included),
  `append_to_section`, `insert_section` (after / as last child of / at end), `delete_section`,
  `replace_text` (exact text that must occur once), `append`. A heading path matches heading text
  outer to inner and may skip levels; an ambiguous or unknown path fails with the candidates.
  Headings inside fenced code don't count. Whole-body replacement is `update_document` with the
  caller's `base_revision`, which fails on mismatch; the web editor saves the same way and offers
  "save anyway" on a conflict (the other write stays in the history).
- Title, tags and fields are columns, not frontmatter; they become frontmatter on export (§3).
- Short per-document edit lease (adapted from nodewright `leases.py`) while a human has the editor open
  or an agent holds `lock_document`. ✅ 0.4.0: the editor's tab holds it (45 s, renewed every 15 s,
  dropped on leaving); `lock_document` 1-15 min. Writes by anyone else are refused (HTTP 423 /
  tool error naming the holder); anyone who may edit can take over from the editor. Unlike
  nodewright, writes respect a lease but don't take one: section ops already apply to the latest
  revision, so the lease only prevents editing under someone, and a single agent edit shouldn't
  lock the user out.
- Every write is a revision (history in §9).
- Open browser tabs update live over SSE (nodewright `events.py`). ✅ 0.4.0: per-document stream
  of revisions (who, which client, summary), lease changes, trash/restore/purge, published after
  commit; in-process broker, so one replica (LISTEN/NOTIFY needed for more).

## 8. Search ✅ 0.3.0

- **Embeddings run locally, in-process**: fastembed (ONNX, CPU) with
  `sentence-transformers/all-MiniLM-L6-v2` — the model doc-indexer settled on (its README records why
  bge-small was dropped). No external API, no GPU, no separate embedding service to deploy.
  Built ✅: embedding runs in a background indexer thread (`indexer.py`), so writes never wait on
  it; the model ships in the image (`/srv/models`). Measured locally: ~40 chunks/s on one thread,
  ~245Mi resident in a read-only container with the model loaded.
  ⚠ unverified: throughput in the cluster pod.
- Chunking by markdown heading (a chunk per stretch of text between headings, long ones split at
  paragraphs to ~1000 chars, each prefixed with title and heading path); re-embed only chunks
  whose text changed. Documents carry `indexed_revision`; a stale one is re-chunked.
- Semantic matches below cosine similarity 0.18 are dropped (calibrated on a small hand-made
  sample 2026-10-12; tune with real data).
- **Hybrid**: pgvector cosine + Postgres full-text (`tsvector`), merged by reciprocal rank fusion.
  pgvector ✅ is installed in webdoc's database (§13). SQLite dev mode does brute-force cosine in Python.
- **`find`: strict literal lookup** alongside semantic `search` — whole-word or exact-phrase match
  over document text (Postgres `~*` with word boundaries over the stored markdown, or `LIKE` for
  phrases), no embeddings involved, so it finds every occurrence of an identifier, error string or
  part number that semantic search would blur. Same filters and permission scoping as `search`.
- Permission filtering happens in the query (owner, membership, domain visibility), never after.
- Both `search` and `find` cover **all sources the caller can read** by default — public, org and
  personal — and every result is labelled with its source and domain so results never mix
  silently. Filters narrow it (§11).

## 9. History and activity log ✅ (§9.4 in 0.3.0)

### 9.1 Document history

- Every write creates a revision storing the full markdown, the **actor** (user) and **client**
  (OAuth client name, "web", or "token:<name>"), a short summary of what changed (sections touched),
  and the base revision it applied to. A document's history reads "Claude Code changed §Auth"
  without consulting the activity log.
- Named versions and restore, as in nodewright §14. Restore is itself a new revision, never a
  rewrite of history.
- **Diffs** between any two revisions: a side-by-side/inline view in the history panel and a
  `diff_versions(document_id, from, to=latest)` MCP tool returning a unified diff, so an agent can
  see exactly what changed since it last read a document.
- **Deletes go to trash.** A deleted document keeps its revisions and can be restored by anyone who
  could edit it. Permanent purge is a separate action for the domain owner or an admin.
- **Retention**: everything is kept. Markdown revisions are small; pruning is added only if storage
  says so.

### 9.2 History beyond documents

Domains, templates, projects, role grants and moderation actions (hide, lock, merge, purge) are
recorded too — templates and domains as revisions, the rest as events — so it is always visible who
changed the domain list, who granted a role, or who hid a post.

### 9.3 Visibility

History follows read access: anyone who can read a document can read its history, signed-out
visitors included for public documents. Content hidden by moderation is excluded from public
history; moderators and admins see it.

### 9.4 Activity log

- `Event` rows for every create/update/delete/propose/approve, with actor (user), client (OAuth client
  name, e.g. "Claude Code", "claude.ai", or "web") and a short summary.
- `whats_new(since=None, domain=None)`: with no `since`, returns changes since **this client's** last
  `whats_new` call (cursor stored per user+client server-side). Agents call it on request; nothing is
  injected at session start.
- Web UI: a feed per user and per domain.

## 10. MCP tools (v1) ✅ except backlinks and contributions

Built in 0.1.0 ✅: `list_domains` (with projects), `create_project`, `get_writing_guide`,
`list_documents`, `get_document`, `create_document`, `create_note`, `edit_document`,
`update_document` (title, tags, project, domain move, or whole body with `base_revision`; replaces
the planned `set_tags`), `delete_document`, `restore_document`, `list_versions`, `diff_versions`,
`save_version`, `restore_version` (`get_document` takes `revision`, replacing `get_version`).
Built in 0.2.0 ✅: `list_templates`, `get_template`, typed `create_document` (`type`, `fields`),
`update_document` `fields`, `propose_domain`, `propose_domain_change`, `propose_template`,
`list_proposals`.
Built in 0.4.0 ✅: `lock_document`, `unlock_document`. Share links have no tool on purpose: a link
hands read access outside the domain, so a person makes it.
Built in 0.3.0 ✅: `search`, `find`, `whats_new` (excludes the calling app's own writes unless
`include_own`; `since` reads without moving the cursor).

| Area | Tools (full plan) |
|---|---|
| Orient | `list_domains`, `list_templates`, `get_template`, `get_writing_guide` |
| Read | `search` (semantic), `find` (literal), `get_document` (optionally one section), `list_documents` (filters), `get_backlinks` |
| Write | `create_document`, `create_note`, `edit_document` (ops, §7), `update_document`, `create_project`, `delete_document` (to trash), `restore_document`, `lock_document`, `unlock_document` |
| History | `list_versions`, `get_version`, `diff_versions`, `save_version`, `restore_version`, `whats_new` |
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
  - Built ✅ 0.3.0: header search box, Semantic/Find toggle, filters for source, domain, kind,
    updated (week/month/year), type, tag, author. Org and project filters wait for orgs / UI need.
- Document view: rendered markdown with the Toast UI editor in markdown mode on edit, template
  guidance in a side panel, backlinks, version history.
- Activity feed per user and per domain (✅ 0.3.0: home page, /activity, domain Activity tab); the proposal approval queue for whoever controls a
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
- `ui/` ✅ — Vite + React 19. Editor: **Toast UI Editor** 3.2.2, as in dan-wiki, driven directly
  (`ui/src/markdown.tsx`) rather than through `@toast-ui/react-editor`, whose wrapper predates React
  19; markdown mode with live preview, WYSIWYG tab hidden, Prism highlighting as in dan-wiki.
  Toast UI's package hides its own types from bundler resolution, hence `ui/src/toastui.d.ts`.
- `helm/webdoc/` — chart deployed by Argo; served at `webdoc.danwils.com` behind authentik, like
  nodewright.
- Database ✅: `webdoc` on the cluster's pgvector postgres (`postgres-pgvector`, pg16), created by
  `infra/cluster-config/postgres/init-webdoc.sql` with the `vector` extension (0.8.2) already
  installed for §8; the schema is the app's Alembic migrations.
- Embedding model weights are baked into the image so pods don't download them at start.

## 14. Phases 📐

1. ✅ 0.1.0 **Core**: scaffold (copy nodewright auth/OAuth/tokens), roles model with admin + per-domain
   reader/editor grants, domains (UI-only creation), projects, tags, notes + documents, document
   history (§9.1: attributed revisions, named versions, diffs, trash), Toast UI editor, home page (personal section first), markdown export. MCP read/write tools.
2. ✅ 0.2.0 **Templates + governance**: template CRUD in UI, `get_template`/typed `create_document`
   validation, `propose_*` tools and the approval queue.
3. ✅ 0.3.0 **Search + activity**: chunking, fastembed, hybrid `search`, literal `find`, central search UI
   with filters, events, `whats_new`, feed UI.
4. ✅ 0.4.0 **Sharing**: sharing personal domains (reader/editor; built in 0.1.0), read-only share
   links per document (created by whoever may grant roles on the domain; not for public
   domains), live updates over SSE, leases.
5. **Contributions**: public domains (home page Public section), drafts → human publish, replies, confirmations, moderation,
   rate limits, secret scan.
6. **Orgs**: orgs with moderators, their own closed taxonomies and member-only contribution
   domains; home page Org section (aligned with nodewright's orgs/roles work).

## 15. Open questions

- Should a public contribution keep a link back to a private source document, visible only to the
  author?
- Does an org domain inherit templates from the org, or only from itself?
- Default role for new users: the user wants everyone to start as a **viewer** (browse only, no
  create) with editors able to add; today any signed-in user gets a personal domain and full
  control of it. To refine.
- Reports: anyone can report a document; an admin can open a reported document for review, even in
  a personal domain, and nothing else there. Not designed in detail yet.
