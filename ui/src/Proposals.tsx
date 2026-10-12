import { useEffect, useState } from "react";
import { api, errText, type Proposal } from "./api";
import { Link } from "./router";
import { ago } from "./util";

const KIND: Record<Proposal["kind"], string> = {
  "domain.create": "New domain", "domain.update": "Change to a domain",
  "template.create": "New document type", "template.update": "Change to a document type",
};

const via = (p: Proposal) => `${p.by ?? "someone"}${p.client === "web" ? "" : ` via ${p.client.replace(/^token:/, "token ")}`}`;

function Payload({ p, edits, setEdits }: { p: Proposal; edits: Record<string, string>; setEdits?: (e: Record<string, string>) => void }) {
  const pl = p.payload;
  if (p.kind === "domain.create") {
    const val = (k: string) => edits[k] ?? pl[k] ?? "";
    return setEdits ? (
      <div className="stack">
        <label>Name<input value={val("name")} onChange={(e) => setEdits({ ...edits, name: e.target.value })} /></label>
        <label>Slug<input value={val("slug")} onChange={(e) => setEdits({ ...edits, slug: e.target.value })} /></label>
        <label>Description<textarea rows={2} value={val("description")} onChange={(e) => setEdits({ ...edits, description: e.target.value })} /></label>
      </div>
    ) : <p><strong>{pl.name}</strong> <code>{pl.slug}</code> — {pl.description}</p>;
  }
  if (p.kind === "domain.update") {
    return (
      <table className="grid"><tbody>
        {["name", "slug", "description"].filter((k) => k in pl).map((k) => (
          <tr key={k}><td className="muted">{k}</td><td><s>{pl.current?.[k]}</s></td><td>→ {pl[k]}</td></tr>
        ))}
      </tbody></table>
    );
  }
  return (
    <div>
      <p><strong>{pl.name}</strong> <code>{pl.type}</code>{p.kind === "template.update" && <span className="muted"> (now version {pl.current_version})</span>}
        {pl.description && <> — {pl.description}</>}</p>
      {pl.fields?.length > 0 && <p className="muted">Fields: {pl.fields.map((f: any) => f.name + (f.required ? "*" : "")).join(", ")}</p>}
      <pre>{pl.skeleton}</pre>
    </div>
  );
}

function Decide({ p, onDone }: { p: Proposal; onDone: () => void }) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const act = async (f: () => Promise<unknown>) => {
    setError(null);
    try { await f(); onDone(); } catch (e) { setError(errText(e)); }
  };
  return (
    <div className="proposal">
      <div className="row">
        <strong>{KIND[p.kind]}</strong>
        <span className="badge">{p.scope === "public" ? "public" : "personal"}{p.domain ? ` · ${p.domain}` : ""}</span>
        <span className="grow" />
        <span className="muted">{via(p)} · {ago(p.created_at)}</span>
      </div>
      <p><span className="muted">Why:</span> {p.rationale}</p>
      <Payload p={p} edits={edits} setEdits={p.kind === "domain.create" ? setEdits : undefined} />
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button className="primary" onClick={() => void act(() => api.approve(p.id, Object.keys(edits).length ? edits : undefined))}>Approve</button>
        <input className="grow" placeholder="Note for the agent (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <button onClick={() => void act(() => api.reject(p.id, note))}>Reject</button>
      </div>
    </div>
  );
}

export function Proposals({ onCount }: { onCount: () => void }) {
  const [data, setData] = useState<{ to_decide: Proposal[]; mine: Proposal[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.proposals().then((r) => { setData(r); onCount(); }, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <div className="page"><p className="error">{error}</p></div>;
  if (!data) return <div className="page"><p className="muted">Loading…</p></div>;
  return (
    <div className="page">
      <h1>Proposals</h1>
      <p className="muted">Agents can't change your domains or document types; they ask here. Approving applies the change as you.</p>
      <h2>Waiting for you</h2>
      {data.to_decide.length === 0 && <p className="muted">Nothing waiting.</p>}
      {data.to_decide.map((p) => <Decide key={p.id} p={p} onDone={() => void load()} />)}
      <h2>Made from your account</h2>
      {data.mine.length === 0 && <p className="muted">None yet.</p>}
      <ul className="tpl-list">
        {data.mine.map((p) => (
          <li key={p.id} className="row">
            <span>{KIND[p.kind]}: <strong>{p.payload.name}</strong></span>
            <span className={"badge status"}>{p.status}</span>
            {p.decision_note && <span className="muted">“{p.decision_note}”</span>}
            {p.status === "approved" && p.kind.startsWith("domain") && p.result_id && <Link to={`/domain/${p.result_id}`}>open</Link>}
            <span className="grow" />
            <span className="muted">{via(p)} · {ago(p.created_at)}</span>
            {p.status === "pending" && <button onClick={() => void api.withdraw(p.id).then(load)}>Withdraw</button>}
          </li>
        ))}
      </ul>
    </div>
  );
}
