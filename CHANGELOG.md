# Changelog

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
