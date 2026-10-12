// Every write carries X-Webdoc: the server's CSRF guard (api/webdoc/auth.py).
// X-Webdoc-Tab names this browser tab, so edit leases tell two tabs of the same
// person apart (api/webdoc/leases.py).

function tabId(): string {
  try {
    let id = sessionStorage.getItem("wd-tab");
    if (!id) sessionStorage.setItem("wd-tab", (id = crypto.randomUUID().replace(/-/g, "").slice(0, 16)));
    return id;
  } catch {
    return Math.random().toString(36).slice(2, 18);
  }
}
export const TAB_ID = tabId();

export class ApiError extends Error {
  status: number;
  body: any;
  constructor(status: number, body: any) {
    const d = body?.detail;
    super(typeof d === "string" ? d : typeof d?.message === "string" ? d.message : `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Webdoc": "1", "X-Webdoc-Tab": TAB_ID };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(url, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 204) return undefined as T;
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new ApiError(r.status, data);
  return data as T;
}

const qs = (o: Record<string, string | number | boolean | null | undefined>) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "" && v !== false) q.set(k, String(v));
  const s = q.toString();
  return s ? "?" + s : "";
};

export type Me = { id: string; username: string; is_admin: boolean };
export type Config = { oidc: boolean; dev_login: boolean; version: string; max_document_bytes: number };
export type Role = "admin" | "owner" | "moderator" | "editor" | "reader";
export type Scope = "public" | "personal";
export type Domain = {
  id: string; slug: string; name: string; description: string; scope: Scope; owner: string | null;
  role: Role | null; documents: number; last_activity: string;
  accepts_contributions: boolean; contribution_types: string[]; can_contribute: boolean;
};
export type Project = { id: string; domain_id: string; slug: string; name: string; description: string };
export type DomainDetail = Domain & {
  projects: Project[]; can: string[]; assignable: Role[]; tags: string[]; contribution_type_names: Record<string, string>;
};
export type Home = { public: Domain[]; personal: Domain[]; shared: Domain[] };
export type Kind = "note" | "document" | "contribution";
export type DocStatus = "published" | "draft" | "hidden";
export type DocMeta = {
  id: string; kind: Kind; title: string; type: string | null; fields: Record<string, string>; domain_id: string; domain: string; project_id: string | null;
  project: string | null; tags: string[]; revision: number; created_at: string; updated_at: string;
  deleted_at: string | null; status: DocStatus;
};
export type ScanFinding = { kind: string; line: number; excerpt: string };
export type ScanResult = { secrets: ScanFinding[]; warnings: ScanFinding[] };
export type ReplyKind = "solution" | "didnt-work" | "comment";
export const REPLY_KINDS: Record<ReplyKind, string> = { solution: "Another fix", "didnt-work": "Didn't work for me", comment: "Comment" };
export type ReplyT = {
  id: string; kind: ReplyKind; kind_text: string; status: "draft" | "published" | "withdrawn" | "hidden"; by: string | null;
  client: string; created_at: string; published_at: string | null; confirmations: number; confirmed_by_me: boolean;
  mine: boolean; body?: string; hidden_reason?: string;
};
export type Thread = {
  status: DocStatus; locked: boolean; hidden_reason: string | null; published_at: string | null; author: string | null;
  merged_into: { id: string; title: string } | null; accepted_reply_id: string | null;
  confirmations: { by: string; environment: string; at: string }[]; confirmed_by_me: boolean; replies: ReplyT[];
  can: { reply: boolean; confirm: boolean; publish: boolean; accept: boolean; moderate: boolean; report: boolean };
};
export type Counts = { drafts: number; proposals: number; reports: number; moderates: boolean };
export type Drafts = {
  contributions: { id: string; title: string; domain_id: string; domain: string; type: string | null; updated_at: string; by_client: string }[];
  replies: { id: string; document_id: string; title: string; kind: ReplyKind; excerpt: string; client: string; created_at: string }[];
};
export type ReportT = {
  id: string; document_id: string; title: string | null; domain_id: string; domain: string | null; reply_id: string | null;
  reply_excerpt: string | null; reason: string; by: string | null; status: string; created_at: string;
};
export type FieldDef = { name: string; description: string; required: boolean };
export type TemplateSection = { path: string[]; required: boolean; guidance: string };
export type TemplateSpec = {
  id: string | null; type: string; name: string; description: string; source: "built-in" | "personal" | "public" | "domain";
  version: number; fields: FieldDef[]; default_tags: string[]; sections: TemplateSection[];
  skeleton?: string; starter?: string; domain_id?: string | null; domain?: string | null; updated_at?: string;
};
export type TemplateCheck = { missing_sections: string[]; missing_fields: string[] };
export type Doc = DocMeta & {
  body: string; role: Role | null; can_edit: boolean; can_delete: boolean; can_purge: boolean; domain_scope: Scope;
  lease: Lease | null; can_share: boolean; contribution?: Thread;
  template?: (TemplateSpec & { check: TemplateCheck }) | null;
};
export type Lease = {
  holder: string; kind: "ui" | "agent"; user_id: string; label: string; expires_at: string; seconds_left: number; message: string;
};
export type ShareLink = { id: string; document_id: string; url: string; created_at: string; by: string | null; views: number; last_viewed_at: string | null };
export type SharedDoc = { title: string; body: string; kind: Kind; type: string | null; fields: Record<string, string>; tags: string[]; updated_at: string; revision: number };
export type DocEvent =
  | { type: "hello"; revision: number; lease: Lease | null }
  | { type: "revision"; revision: number; by: string; user_id: string; client: string; summary: string }
  | { type: "lease"; lease: Lease | null }
  | { type: "trashed" | "restored" | "purged" | "published" | "reply" | "confirmation" | "moderation"; by: string };
export type Proposal = {
  id: string; kind: "domain.create" | "domain.update" | "template.create" | "template.update";
  status: "pending" | "approved" | "rejected" | "withdrawn"; payload: any; rationale: string; scope: Scope;
  domain_id: string | null; domain: string | null; by: string | null; client: string; created_at: string;
  decided_by: string | null; decided_at: string | null; decision_note: string; result_id: string | null; can_decide: boolean;
};
export type Revision = {
  revision: number; created_at: string; by: string | null; client: string; source: string; summary: string;
  label: string | null; restored_from: number | null;
};
export type History = { current: number; more: boolean; revisions: Revision[] };
export type Grant = { user_id: string; username: string; role: Role };
export type DomainRevision = { revision: number; name: string; slug: string; description: string; by: string | null; created_at: string };
export type Token = { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null };
export type NewToken = Token & { token: string; mcp_url: string };
export type OAuthGrant = { id: string; client_name: string; created_at: string; last_used_at: string | null };
export type Guide = { text: string; is_default: boolean };
export type Source = "public" | "personal" | "shared";
export type HitMeta = {
  id: string; title: string; kind: Kind; type: string | null; domain_id: string; domain: string; source: Source;
  owner: string | null; project_id: string | null; project: string | null; tags: string[]; updated_at: string;
};
export type SearchHit = HitMeta & { heading: string; snippet: string; matched: ("semantic" | "keyword" | "title")[]; score: number };
export type FindMatch = { line: number; heading: string; text: string; in: "title" | "body" };
export type FindHit = HitMeta & { count: number; matches: FindMatch[] };
export type SearchResult = { query: string; semantic: boolean; pending: number; index_error: string | null; results: SearchHit[] };
export type FindResult = { text: string; whole_word: boolean; case_sensitive: boolean; limited: boolean; results: FindHit[] };
export type SearchParams = {
  q: string; mode?: "semantic" | "find"; whole_word?: boolean; case_sensitive?: boolean; source?: string;
  domain_id?: string; kind?: string; type?: string; tag?: string; author?: string; updated_after?: string; limit?: number;
};
export type ActivityEvent = {
  id: string; at: string; by: string; client: string; action: string; summary: string; target_kind: string;
  target_id: string; domain_id: string | null; domain: string | null; title: string | null; exists: boolean | null;
};

export const api = {
  config: () => call<Config>("GET", "/api/config"),
  me: () => call<Me>("GET", "/api/me"),
  logout: () => call("POST", "/api/auth/logout"),
  devLogin: (name: string, admin: boolean) => call<Me>("POST", "/api/auth/dev-login", { name, admin }),
  home: () => call<Home>("GET", "/api/home"),
  createDomain: (body: { name: string; slug?: string; description: string; scope: Scope }) =>
    call<Domain>("POST", "/api/domains", body),
  domain: (id: string) => call<DomainDetail>("GET", `/api/domains/${id}`),
  patchDomain: (id: string, body: { name?: string; slug?: string; description?: string }) =>
    call<DomainDetail>("PATCH", `/api/domains/${id}`, body),
  deleteDomain: (id: string) => call("DELETE", `/api/domains/${id}`),
  domainHistory: (id: string) =>
    call<{ revisions: DomainRevision[] }>("GET", `/api/domains/${id}/revisions`).then((r) => r.revisions),
  grants: (id: string) => call<{ grants: Grant[]; assignable: Role[]; owner: string | null }>("GET", `/api/domains/${id}/grants`),
  setGrant: (id: string, username: string, role: Role) => call<Grant>("PUT", `/api/domains/${id}/grants`, { username, role }),
  removeGrant: (id: string, userId: string) => call("DELETE", `/api/domains/${id}/grants/${userId}`),
  createProject: (domainId: string, name: string) => call<Project>("POST", `/api/domains/${domainId}/projects`, { name }),
  renameProject: (id: string, name: string) => call<Project>("PATCH", `/api/projects/${id}`, { name }),
  deleteProject: (id: string) => call("DELETE", `/api/projects/${id}`),
  documents: (f: { domain?: string; project?: string; kind?: string; tag?: string; q?: string; trashed?: boolean; limit?: number; offset?: number }) =>
    call<{ total: number; documents: DocMeta[] }>("GET", "/api/documents" + qs(f)),
  createDocument: (body: { domain_id: string; kind: Kind; title: string; body: string; tags: string[]; project_id: string | null; type?: string | null; fields?: Record<string, string> }) =>
    call<Doc>("POST", "/api/documents", body),
  document: (id: string) => call<Doc>("GET", `/api/documents/${id}`),
  saveDocument: (id: string, body: { base_revision: number; title?: string; body?: string; tags?: string[]; fields?: Record<string, string | null>; project_id?: string | null; domain_id?: string; ack_warnings?: boolean }) =>
    call<Doc>("PUT", `/api/documents/${id}`, body),
  trash: (id: string) => call("DELETE", `/api/documents/${id}`),
  untrash: (id: string) => call<Doc>("POST", `/api/documents/${id}/restore`),
  purge: (id: string) => call("DELETE", `/api/documents/${id}/purge`),
  history: (id: string, before?: number) => call<History>("GET", `/api/documents/${id}/revisions` + qs({ before })),
  revision: (id: string, n: number) =>
    call<Revision & { title: string; body: string; tags: string[] }>("GET", `/api/documents/${id}/revisions/${n}`),
  nameRevision: (id: string, n: number, label: string | null) => call<Revision>("PATCH", `/api/documents/${id}/revisions/${n}`, { label }),
  restoreRevision: (id: string, n: number) => call<Doc>("POST", `/api/documents/${id}/revisions/${n}/restore`),
  takeLease: (id: string, force = false) => call<{ lease: Lease }>("POST", `/api/documents/${id}/lease` + qs({ force })).then((r) => r.lease),
  dropLease: (id: string) => call<{ released: boolean }>("DELETE", `/api/documents/${id}/lease`),
  /** For page unload, when an awaited request would be cut off. */
  dropLeaseBeacon: (id: string) => {
    void fetch(`/api/documents/${id}/lease`, { method: "DELETE", keepalive: true, credentials: "same-origin",
      headers: { "X-Webdoc": "1", "X-Webdoc-Tab": TAB_ID } }).catch(() => undefined);
  },
  shares: (id: string) => call<{ shares: ShareLink[] }>("GET", `/api/documents/${id}/shares`).then((r) => r.shares),
  createShare: (id: string) => call<ShareLink>("POST", `/api/documents/${id}/shares`),
  revokeShare: (shareId: string) => call("DELETE", `/api/shares/${shareId}`),
  counts: () => call<Counts>("GET", "/api/counts"),
  contributionSettings: (domainId: string, accepts: boolean, types: string[]) =>
    call<DomainDetail>("PATCH", `/api/domains/${domainId}/contributions`, { accepts, types }),
  scan: (id: string) => call<ScanResult>("GET", `/api/documents/${id}/scan`),
  publish: (id: string, ack_warnings = false) => call<Doc>("POST", `/api/documents/${id}/publish`, { ack_warnings }),
  postReply: (id: string, kind: ReplyKind, body: string, ack_warnings = false) =>
    call<Doc>("POST", `/api/documents/${id}/replies`, { kind, body, ack_warnings }),
  publishReply: (replyId: string, ack_warnings = false) => call<Doc>("POST", `/api/replies/${replyId}/publish`, { ack_warnings }),
  withdrawReply: (replyId: string) => call<Doc>("DELETE", `/api/replies/${replyId}`),
  moderateReply: (replyId: string, hidden: boolean, reason = "") => call<Doc>("POST", `/api/replies/${replyId}/moderate`, { hidden, reason }),
  accept: (id: string, reply_id: string | null) => call<Doc>("POST", `/api/documents/${id}/accept`, { reply_id }),
  confirm: (id: string, reply_id: string | null, environment = "") => call<Doc>("POST", `/api/documents/${id}/confirm`, { reply_id, environment }),
  unconfirm: (id: string, reply_id: string | null) => call<Doc>("DELETE", `/api/documents/${id}/confirm` + qs({ reply_id })),
  moderate: (id: string, action: "hide" | "unhide" | "lock" | "unlock" | "duplicate" | "unduplicate", reason = "", of?: string) =>
    call<Doc>("POST", `/api/documents/${id}/moderate`, { action, reason, of }),
  report: (document_id: string, reason: string, reply_id?: string) => call("POST", "/api/reports", { document_id, reason, reply_id }),
  reports: () => call<{ reports: ReportT[]; moderates: boolean }>("GET", "/api/reports"),
  resolveReport: (id: string, action: "dismiss" | "hide", note = "") => call<ReportT>("POST", `/api/reports/${id}/resolve`, { action, note }),
  drafts: () => call<Drafts>("GET", "/api/drafts"),
  shared: (token: string) => call<SharedDoc>("GET", `/api/s/${encodeURIComponent(token)}`),
  diff: (id: string, a: number, b?: number) => call<{ diff: string }>("GET", `/api/documents/${id}/diff` + qs({ a, b })).then((r) => r.diff),
  tags: (domain?: string) => call<{ tags: { tag: string; count: number }[] }>("GET", "/api/tags" + qs({ domain })).then((r) => r.tags),
  domainTemplates: (domainId: string) =>
    call<{ templates: TemplateSpec[] }>("GET", `/api/domains/${domainId}/templates`).then((r) => r.templates),
  templates: () => call<{ templates: TemplateSpec[]; builtin: TemplateSpec[] }>("GET", "/api/templates"),
  createTemplate: (body: { scope: Scope; domain_id?: string | null; type: string; name: string; description: string; skeleton: string; fields: FieldDef[]; default_tags: string[] }) =>
    call<TemplateSpec>("POST", "/api/templates", body),
  patchTemplate: (id: string, body: { name?: string; description?: string; skeleton?: string; fields?: FieldDef[]; default_tags?: string[] }) =>
    call<TemplateSpec>("PATCH", `/api/templates/${id}`, body),
  deleteTemplate: (id: string) => call("DELETE", `/api/templates/${id}`),
  templateHistory: (id: string) =>
    call<{ revisions: { version: number; name: string; skeleton: string; by: string | null; created_at: string }[] }>("GET", `/api/templates/${id}/revisions`).then((r) => r.revisions),
  proposals: () => call<{ to_decide: Proposal[]; mine: Proposal[] }>("GET", "/api/proposals"),
  proposalCount: () => call<{ pending: number }>("GET", "/api/proposals/count").then((r) => r.pending),
  approve: (id: string, edits?: Record<string, unknown>) => call<Proposal>("POST", `/api/proposals/${id}/approve`, { edits }),
  reject: (id: string, note: string) => call<Proposal>("POST", `/api/proposals/${id}/reject`, { note }),
  withdraw: (id: string) => call<Proposal>("POST", `/api/proposals/${id}/withdraw`),
  tokens: () => call<Token[]>("GET", "/api/tokens"),
  createToken: (name: string) => call<NewToken>("POST", "/api/tokens", { name }),
  revokeToken: (id: string) => call("DELETE", `/api/tokens/${id}`),
  oauthGrants: () => call<{ grants: OAuthGrant[] }>("GET", "/api/oauth/grants").then((r) => r.grants),
  revokeOAuthGrant: (id: string) => call("DELETE", `/api/oauth/grants/${id}`),
  search: (p: SearchParams) => call<SearchResult>("GET", "/api/search" + qs({ ...p, mode: "semantic" })),
  find: (p: SearchParams) =>
    call<FindResult>("GET", "/api/search" + qs({ ...p, mode: "find", whole_word: p.whole_word === false ? "false" : undefined, case_sensitive: p.case_sensitive })),
  activity: (p: { domain_id?: string; before?: string; limit?: number }) =>
    call<{ events: ActivityEvent[]; more: boolean }>("GET", "/api/activity" + qs(p)),
  guide: () => call<Guide>("GET", "/api/writing-guide"),
  saveGuide: (text: string) => call<Guide>("PUT", "/api/writing-guide", { text }),
};

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
