import { useCallback, useEffect, useState } from "react";
import { api, errText, type DocMeta, type DomainDetail, type ImportResult, type Kind, type Me } from "./api";
import { ActivityList } from "./Activity";
import { DomainSettings } from "./DomainSettings";
import { ImportDialog, ImportSummary } from "./ImportDialog";
import { PageTree, usePageTree } from "./PageTree";
import { Link, navigate } from "./router";
import { ago, roleText } from "./util";

type Filters = { kind?: Kind; tag?: string; q?: string; trashed?: boolean };
const PAGE = 50;

export function DomainPage({ id, me }: { id: string; me: Me | null }) {
  const [d, setD] = useState<DomainDetail | null>(null);
  const [docs, setDocs] = useState<DocMeta[]>([]);
  const [total, setTotal] = useState(0);
  const [f, setF] = useState<Filters>({});
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [settings, setSettings] = useState(false);
  const [tab, setTab] = useState<"docs" | "activity">("docs");
  const tree = usePageTree(id);
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<ImportResult | null>(null);

  const loadDomain = useCallback(() => api.domain(id).then(setD, (e) => setError(errText(e))), [id]);
  const loadDocs = useCallback((more = false) =>
    api.documents({ domain: id, ...f, limit: PAGE, offset: more ? docs.length : 0 }).then((r) => {
      setDocs((prev) => (more ? [...prev, ...r.documents] : r.documents));
      setTotal(r.total);
    }, (e) => setError(errText(e))), [id, f, docs.length]);

  useEffect(() => { void loadDomain(); setF({}); setQ(""); }, [loadDomain]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadDocs(); }, [id, f]);

  if (error) return <div className="page"><p className="error">{error}</p></div>;
  if (!d) return <div className="page"><p className="muted">Loading…</p></div>;
  const canWrite = d.can.includes("write");
  const canDelete = d.can.includes("delete");
  const set = (patch: Filters) => setF((prev) => ({ ...prev, ...patch }));

  const restore = async (doc: DocMeta) => {
    await api.untrash(doc.id).catch((e) => setError(errText(e)));
    void loadDocs();
    void tree.reload();
  };
  const purge = async (doc: DocMeta) => {
    if (!confirm(`Permanently delete "${doc.title}", the pages trashed with it, and all their history? This cannot be undone.`)) return;
    await api.purge(doc.id).catch((e) => setError(errText(e)));
    void loadDocs();
  };
  const newDoc = (kind: Kind) => navigate(`/new?domain=${d.id}&kind=${kind}`);

  return (
    <div className="page domain-page">
      <div className="crumbs"><Link to="/">Home</Link> › <span>{d.forum ? "Forums" : d.scope === "public" ? "Public" : "Personal"}</span></div>
      <div className="row domain-head">
        <h1>{d.name}</h1>
        {d.forum === "private" && <span className="badge">private forum</span>}
        {d.role && <span className="badge">{roleText(d.role, d.forum)}</span>}
        <span className="grow" />
        {canWrite && <button className="primary" onClick={() => newDoc("document")}>New document</button>}
        {canWrite && <button onClick={() => newDoc("note")}>New note</button>}
        {d.can_contribute && <button className={canWrite ? "" : "primary"} onClick={() => newDoc("contribution")}>New contribution</button>}
        {canWrite && <button onClick={() => setImporting(true)} title="Import a zip or tar of markdown">Import</button>}
        <a className="button" href={`/api/domains/${d.id}/export`} title="Download as markdown files">Export</a>
        {me && (d.can.includes("manage") || d.can.includes("grants")) &&
          <button onClick={() => setSettings(true)}>Settings</button>}
      </div>
      {d.description && <p className="muted domain-desc">{d.description}</p>}
      {imported && (
        <div className="banner info import-done">
          Imported: {imported.created.length} new, {imported.updated.length} updated, {imported.attachments.length} files.
          <button className="flat-btn" onClick={() => setImported(null)}>×</button>
          <ImportSummary r={imported} />
        </div>
      )}
      <div className="domain-body">
        <aside className="filters">
          <h4>Pages</h4>
          <PageTree domainId={d.id} canWrite={canWrite} pages={tree.pages} reload={tree.reload}
            onMoved={() => void loadDocs()} />
          <h4>Kind</h4>
          {([undefined, "document", "note", ...(d.accepts_contributions ? ["contribution" as const] : [])] as const).map((k) => (
            <button key={k || "all"} className={"flat" + (f.kind === k ? " on" : "")} onClick={() => set({ kind: k })}>
              {k ? k[0].toUpperCase() + k.slice(1) + "s" : "All"}
            </button>
          ))}
          {d.tags.length > 0 && <h4>Tags</h4>}
          <div className="tags">
            {d.tags.map((t) => (
              <button key={t} className={"tag" + (f.tag === t ? " on" : "")} onClick={() => set({ tag: f.tag === t ? undefined : t })}>{t}</button>
            ))}
          </div>
          {canDelete && <>
            <h4>Trash</h4>
            <button className={"flat" + (f.trashed ? " on" : "")} onClick={() => set({ trashed: !f.trashed })}>
              {f.trashed ? "Back to documents" : "Show trash"}
            </button>
          </>}
        </aside>
        <section className="doc-list">
          <div className="tabs">
            <button className={tab === "docs" ? "on" : ""} onClick={() => setTab("docs")}>Documents</button>
            <button className={tab === "activity" ? "on" : ""} onClick={() => setTab("activity")}>Activity</button>
          </div>
          {tab === "activity" ? <ActivityList domainId={d.id} /> : <>
          <form className="row" onSubmit={(e) => { e.preventDefault(); set({ q: q.trim() || undefined }); }}>
            <input className="grow" placeholder="Filter by title" value={q} onChange={(e) => setQ(e.target.value)} />
          </form>
          {f.trashed && <p className="muted">Trash: documents here keep their history and can be restored.</p>}
          {docs.length === 0 && <p className="muted">{f.trashed ? "The trash is empty." : "Nothing here yet."}</p>}
          <ul className="docs">
            {docs.map((doc) => (
              <li key={doc.id}>
                <div className="row">
                  <span className={"kind k-" + doc.kind}>{doc.kind}</span>
                  {doc.status !== "published" && <span className={"badge st-" + doc.status}>{doc.status}</span>}
                  {f.trashed ? <span className="doc-title">{doc.title}</span>
                    : <Link to={`/d/${doc.id}`} className="doc-title">{doc.title}</Link>}
                  <span className="grow" />
                  {f.trashed ? <>
                    <button onClick={() => void restore(doc)}>Restore</button>
                    {d.can.includes("purge") && <button className="danger" onClick={() => void purge(doc)}>Delete forever</button>}
                  </> : <span className="muted">{ago(doc.updated_at)}</span>}
                </div>
                <div className="doc-meta muted">
                  {doc.path.length > 0 && <span>{doc.path.map((p) => p.title).join(" / ")}</span>}
                  {doc.tags.map((t) => <span key={t} className="tag small">{t}</span>)}
                </div>
              </li>
            ))}
          </ul>
          {docs.length < total && <button onClick={() => void loadDocs(true)}>Load more ({total - docs.length})</button>}
          </>}
        </section>
      </div>
      {importing && <ImportDialog domainId={d.id} pages={tree.pages} under={null} onClose={() => setImporting(false)}
        onDone={(r) => { setImporting(false); setImported(r); void tree.reload(); void loadDocs(); }} />}
      {settings && me && <DomainSettings domain={d} me={me} onClose={() => setSettings(false)}
        onChanged={() => void loadDomain()} />}
    </div>
  );
}
