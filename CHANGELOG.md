# Changelog

## 0.5.0 — 2026-10-12

Phase 5 (design §14): public contribution domains.

- Moderators open a public domain to contributions and choose the accepted document types.
  Any signed-in user can then contribute.
- Human gate: every contribution and every agent-written reply starts as a draft only its author
  sees (and the activity feed shows only to them). A person publishes it from the web UI; no
  MCP tool publishes, and agents can't edit a published contribution. A Drafts page (with a count
  in the header) lists what's waiting.
- Secret scan on publishing, posting a reply and editing a published contribution: keys, tokens,
  passwords and private keys block; private IPs, internal host names, home paths and email
  addresses need "publish anyway". Confirmation notes must be free of both.
- Replies (another fix, didn't work, comment), published replies withdrawn rather than edited;
  "this worked for me" confirmations with an optional environment, on the contribution or a reply;
  the author marks the reply that resolved it.
- Moderation: hide (with a reason the author sees), lock, mark as a duplicate of another post
  (drops out of search); reports from any signed-in user to a Moderation page for the domain's
  moderators and admins.
- Rate limits on drafts, replies, confirmations and reports per user and per app, and on
  signed-out search per address.
- MCP: draft_contribution (checks for duplicates first and creates nothing if it finds some),
  reply, confirm, list_drafts; get_document includes the thread.

### Fixed
- The markdown editor no longer throws when it is replaced right after it appears (picking a
  type as the page loads).

## 0.4.0 — 2026-10-12

Phase 4 (design §14): leases, live updates, share links. (Sharing personal domains with
readers and editors shipped in 0.1.0.)

- Edit leases: opening the editor holds the document for that browser tab (renewed while open,
  released on save, cancel or closing the tab). Others, other tabs and agents can't write it
  meanwhile and see who is editing; anyone who may edit can take over ("Edit anyway").
  Agents can hold a document with lock_document / unlock_document (1-15 minutes). Unlike
  nodewright, writes don't take a lease themselves, so one agent edit doesn't lock the editor.
- Live updates: an open document refreshes when someone else changes it and says who and
  through which app; while editing it warns instead. Lease changes, trash and restore show
  live too. Signed-out readers of public documents get them as well.
- Read-only share links per document for people without an account: current text only, no
  history or domain. Created and revoked by whoever may grant roles on the domain; view counts;
  kept out of search engines. Public documents don't need them.
- MCP: lock_document, unlock_document; get_document reports when someone is editing; writes
  to a document someone else holds are refused with who and for how long.

## 0.3.0 — 2026-10-12

Phase 3 (design §14): search, find and activity.

- Search: hybrid semantic + full-text + title matching over everything the viewer can read,
  merged by reciprocal rank fusion, one result per document with its best section and a snippet,
  labelled public / personal / shared. Embeddings come from all-MiniLM-L6-v2 run in-process
  (fastembed, model shipped in the image); pgvector and Postgres full-text indexes in production.
- Find: literal lookup of a word or phrase (whole word and match case options) with line numbers
  and sections, for error strings, identifiers and part numbers.
- Search page reached from a search box in the header: Semantic / Find toggle; filters for source,
  domain, kind, updated date, document type, tag and author; state in the URL.
- Indexing runs in the background; writes never wait on it, and only changed sections are
  re-embedded. Results say when recently changed documents are still being indexed.
- Activity: feed on the home page, an Activity page, and an Activity tab per domain, showing who
  changed what and through which app; it follows read access.
- MCP: search, find, whats_new (changes since this app last asked, grouped per document; the
  app's own writes are left out). Nothing is injected at session start.

## 0.2.0 — 2026-10-11

Phase 2 (design §14): document types and the proposal queue.

- Document types: markdown skeletons whose headings are the sections, with guidance in HTML comments
  and "(optional)" for sections that may be left out, plus fields and default tags. Built-ins:
  Architecture, Decision record, Issue resolution. A domain's own type wins over its owner's, which
  wins over the built-in. Types page to create, customize and version them.
- Typed documents: pick a type when creating, start from its headings with the guidance alongside,
  fill its fields; required sections and fields are checked at creation and shown if missing later.
  Fields are editable and restored with versions.
- Proposals: agents can propose a domain, a change to a domain, or a document type; nothing
  changes until a person approves on the Proposals page (count in the header), optionally editing
  a new domain first. Rejections carry a note the agent can read.
- MCP: list_templates, get_template, typed create_document, update_document fields,
  propose_domain, propose_domain_change, propose_template, list_proposals.

### Changed
- Admins no longer have any access to other people's personal domains; their control covers
  public domains.

## 0.1.0 — 2026-10-11

Phase 1 (design §14). Live at https://webdoc.danwils.com (Argo app in infra/cluster-config).

- Domains: personal (one created at first sign-in) and public (admins only), created and changed
  only in the web UI; every change recorded. Only an empty domain can be deleted.
- Roles: admin from the `ADMIN_GROUP` authentik group; per-domain reader / editor grants, and
  moderators on public domains. Signed-out visitors read public domains.
- Notes and documents with projects and tags; Toast UI markdown editor; stale saves refused with a
  "save anyway" option.
- History: every write is a revision recording user, client and what changed; diffs, named
  versions, restore; trash with restore, and purge for owners and admins.
- Markdown export of a document, or a domain as a zip with frontmatter.
- MCP server with OAuth sign-in and personal access tokens: list_domains, create_project,
  get_writing_guide, list_documents, get_document, create_document, create_note, edit_document
  (section operations), update_document, delete_document, restore_document, list_versions,
  diff_versions, save_version, restore_version. No tool can change a domain.
