// Every write carries X-Webdoc: the server's CSRF guard (api/webdoc/auth.py).

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
  const headers: Record<string, string> = { "X-Webdoc": "1" };
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
};
export type Project = { id: string; domain_id: string; slug: string; name: string; description: string };
export type DomainDetail = Domain & { projects: Project[]; can: string[]; assignable: Role[]; tags: string[] };
export type Home = { public: Domain[]; personal: Domain[]; shared: Domain[] };
export type Kind = "note" | "document";
export type DocMeta = {
  id: string; kind: Kind; title: string; type: string | null; fields: Record<string, string>; domain_id: string; domain: string; project_id: string | null;
  project: string | null; tags: string[]; revision: number; created_at: string; updated_at: string;
  deleted_at: string | null;
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
  template?: (TemplateSpec & { check: TemplateCheck }) | null;
};
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
  saveDocument: (id: string, body: { base_revision: number; title?: string; body?: string; tags?: string[]; fields?: Record<string, string | null>; project_id?: string | null; domain_id?: string }) =>
    call<Doc>("PUT", `/api/documents/${id}`, body),
  trash: (id: string) => call("DELETE", `/api/documents/${id}`),
  untrash: (id: string) => call<Doc>("POST", `/api/documents/${id}/restore`),
  purge: (id: string) => call("DELETE", `/api/documents/${id}/purge`),
  history: (id: string, before?: number) => call<History>("GET", `/api/documents/${id}/revisions` + qs({ before })),
  revision: (id: string, n: number) =>
    call<Revision & { title: string; body: string; tags: string[] }>("GET", `/api/documents/${id}/revisions/${n}`),
  nameRevision: (id: string, n: number, label: string | null) => call<Revision>("PATCH", `/api/documents/${id}/revisions/${n}`, { label }),
  restoreRevision: (id: string, n: number) => call<Doc>("POST", `/api/documents/${id}/revisions/${n}/restore`),
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
