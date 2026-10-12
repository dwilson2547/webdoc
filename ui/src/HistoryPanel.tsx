import { useEffect, useState } from "react";
import { api, errText, type Doc, type Revision } from "./api";
import { MarkdownView } from "./markdown";
import { Diff, ago } from "./util";

function who(r: Revision) {
  const by = r.by ?? "someone";
  return r.client === "web" ? by : `${by} via ${r.client.replace(/^token:/, "token ")}`;
}

/** A document's revisions (design §9.1): who, through which app, what changed;
 *  diff any revision against the current one, view it, name it, restore it. */
export function HistoryPanel({ doc, onRestored, onClose }: {
  doc: Doc; onRestored: (d: Doc) => void; onClose: () => void;
}) {
  const [revs, setRevs] = useState<Revision[]>([]);
  const [more, setMore] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [view, setView] = useState<"diff" | "text">("diff");
  const [diff, setDiff] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editable = doc.can_edit && !doc.deleted_at;

  const load = (before?: number) => api.history(doc.id, before).then((h) => {
    setRevs((prev) => (before ? [...prev, ...h.revisions] : h.revisions));
    setMore(h.more);
  }, (e) => setError(errText(e)));
  useEffect(() => { void load(); setSel(null); }, [doc.id, doc.revision]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (sel === null) return;
    setDiff(null);
    setText(null);
    if (view === "diff") {
      // The selected revision against the one before it shows what that write did.
      const prev = sel > 1 ? sel - 1 : sel;
      api.diff(doc.id, prev, sel).then(setDiff, (e) => setError(errText(e)));
    } else {
      api.revision(doc.id, sel).then((r) => setText(r.body), (e) => setError(errText(e)));
    }
  }, [sel, view, doc.id]);

  const rename = async (r: Revision) => {
    const label = prompt("Name this version (empty to remove the name)", r.label ?? "");
    if (label === null) return;
    await api.nameRevision(doc.id, r.revision, label.trim() || null).catch((e) => setError(errText(e)));
    void load();
  };
  const restore = async (n: number) => {
    if (!confirm(`Restore revision ${n}? It becomes a new revision; nothing is lost.`)) return;
    try {
      onRestored(await api.restoreRevision(doc.id, n));
    } catch (e) {
      setError(errText(e));
    }
  };

  return (
    <aside className="history">
      <div className="row"><h3>History</h3><span className="grow" /><button onClick={onClose} aria-label="Close history">×</button></div>
      {error && <p className="error">{error}</p>}
      <ol className="revs">
        {revs.map((r) => (
          <li key={r.revision} className={sel === r.revision ? "on" : ""} onClick={() => setSel(r.revision)}>
            <div className="row">
              <strong>#{r.revision}</strong>
              {r.label && <span className="badge">{r.label}</span>}
              <span className="grow" />
              <span className="muted" title={new Date(r.created_at).toLocaleString()}>{ago(r.created_at)}</span>
            </div>
            <div className="muted">{who(r)} · {r.summary}</div>
          </li>
        ))}
      </ol>
      {more && <button onClick={() => void load(revs[revs.length - 1].revision)}>Older…</button>}
      {sel !== null && (
        <div className="rev-detail">
          <div className="row">
            <div className="tabs">
              <button className={view === "diff" ? "on" : ""} onClick={() => setView("diff")}>Changes</button>
              <button className={view === "text" ? "on" : ""} onClick={() => setView("text")}>Text</button>
            </div>
            <span className="grow" />
            {editable && <button onClick={() => void rename(revs.find((r) => r.revision === sel)!)}>Name</button>}
            {editable && sel !== doc.revision && <button onClick={() => void restore(sel)}>Restore</button>}
          </div>
          {view === "diff" && (diff === null ? <p className="muted">Loading…</p> : <Diff text={diff} />)}
          {view === "text" && (text === null ? <p className="muted">Loading…</p> : <MarkdownView markdown={text} />)}
        </div>
      )}
    </aside>
  );
}
