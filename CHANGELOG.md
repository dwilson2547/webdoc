# Changelog

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
