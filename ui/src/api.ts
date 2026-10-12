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
  id: string; kind: Kind; title: string; domain_id: string; domain: string; project_id: string | null;
  project: string | null; tags: string[]; revision: number; created_at: string; updated_at: string;
  deleted_at: string | null;
};
export type Doc = DocMeta & {
  body: string; role: Role | null; can_edit: boolean; can_delete: boolean; can_purge: boolean; domain_scope: Scope;
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
  createDocument: (body: { domain_id: string; kind: Kind; title: string; body: string; tags: string[]; project_id: string | null }) =>
    call<Doc>("POST", "/api/documents", body),
  document: (id: string) => call<Doc>("GET", `/api/documents/${id}`),
  saveDocument: (id: string, body: { base_revision: number; title?: string; body?: string; tags?: string[]; project_id?: string | null; domain_id?: string }) =>
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
  tokens: () => call<Token[]>("GET", "/api/tokens"),
  createToken: (name: string) => call<NewToken>("POST", "/api/tokens", { name }),
  revokeToken: (id: string) => call("DELETE", `/api/tokens/${id}`),
  oauthGrants: () => call<{ grants: OAuthGrant[] }>("GET", "/api/oauth/grants").then((r) => r.grants),
  revokeOAuthGrant: (id: string) => call("DELETE", `/api/oauth/grants/${id}`),
  guide: () => call<Guide>("GET", "/api/writing-guide"),
  saveGuide: (text: string) => call<Guide>("PUT", "/api/writing-guide", { text }),
};

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
