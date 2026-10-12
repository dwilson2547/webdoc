import { useEffect, useState } from "react";
import { api, errText, type DomainDetail, type DomainRevision, type Grant, type Me, type Role } from "./api";
import { navigate } from "./router";
import { ROLE_TEXT, Modal } from "./util";

function General({ d, onChanged }: { d: DomainDetail; onChanged: () => void }) {
  const [name, setName] = useState(d.name);
  const [slug, setSlug] = useState(d.slug);
  const [description, setDescription] = useState(d.description);
  const [msg, setMsg] = useState<string | null>(null);
  const save = async () => {
    try {
      await api.patchDomain(d.id, { name, slug, description });
      setMsg("Saved.");
      onChanged();
    } catch (e) {
      setMsg(errText(e));
    }
  };
  const remove = async () => {
    if (!confirm(`Delete the domain "${d.name}"? Only an empty domain (trash included) can be deleted.`)) return;
    try {
      await api.deleteDomain(d.id);
      navigate("/");
    } catch (e) {
      setMsg(errText(e));
    }
  };
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <label>Name<input value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
      <label>Slug <span className="muted">(how agents refer to it)</span>
        <input value={slug} maxLength={64} onChange={(e) => setSlug(e.target.value)} /></label>
      <label>Description<textarea rows={3} value={description} maxLength={2000} onChange={(e) => setDescription(e.target.value)} /></label>
      {msg && <p className="muted">{msg}</p>}
      <div className="row">
        <button type="submit" className="primary">Save</button>
        <span className="grow" />
        <button type="button" className="danger" onClick={() => void remove()}>Delete domain</button>
      </div>
    </form>
  );
}

function People({ d, me }: { d: DomainDetail; me: Me }) {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [assignable, setAssignable] = useState<Role[]>([]);
  const [owner, setOwner] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("reader");
  const [error, setError] = useState<string | null>(null);
  const load = () => api.grants(d.id).then((r) => {
    setGrants(r.grants);
    setAssignable(r.assignable);
    setOwner(r.owner);
  }, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (p: Promise<unknown>) => {
    setError(null);
    try { await p; await load(); } catch (e) { setError(errText(e)); }
  };
  return (
    <div className="stack">
      <p className="muted">
        {d.scope === "public"
          ? "Everyone can read a public domain. Editors can change anyone's documents; moderators also manage who is an editor."
          : `Owned by ${owner}. Readers can read and search; editors can also write, and move things to the trash.`}
      </p>
      <table className="grid">
        <tbody>
          {grants.map((g) => (
            <tr key={g.user_id}>
              <td>{g.username}</td>
              <td>
                {assignable.includes(g.role) ? (
                  <select value={g.role} onChange={(e) => void act(api.setGrant(d.id, g.username, e.target.value as Role))}>
                    {assignable.map((r) => <option key={r} value={r}>{ROLE_TEXT[r]}</option>)}
                  </select>
                ) : ROLE_TEXT[g.role]}
              </td>
              <td>{(assignable.includes(g.role) || g.user_id === me.id) &&
                <button onClick={() => void act(api.removeGrant(d.id, g.user_id))}>Remove</button>}</td>
            </tr>
          ))}
          {grants.length === 0 && <tr><td className="muted">Nobody else yet.</td></tr>}
        </tbody>
      </table>
      {assignable.length > 0 && (
        <form className="row" onSubmit={(e) => { e.preventDefault(); void act(api.setGrant(d.id, username.trim(), role)).then(() => setUsername("")); }}>
          <input placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} />
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {assignable.map((r) => <option key={r} value={r}>{ROLE_TEXT[r]}</option>)}
          </select>
          <button type="submit" disabled={!username.trim()}>Add</button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function DomainHistory({ d }: { d: DomainDetail }) {
  const [rows, setRows] = useState<DomainRevision[] | null>(null);
  useEffect(() => { api.domainHistory(d.id).then(setRows, () => setRows([])); }, [d.id]);
  if (!rows) return <p className="muted">Loading…</p>;
  return (
    <table className="grid">
      <thead><tr><th>#</th><th>Name</th><th>Slug</th><th>By</th><th>When</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.revision}><td>{r.revision}</td><td>{r.name}</td><td>{r.slug}</td><td>{r.by}</td>
          <td>{new Date(r.created_at).toLocaleString()}</td></tr>
      ))}</tbody>
    </table>
  );
}

export function DomainSettings({ domain, me, onClose, onChanged }: {
  domain: DomainDetail; me: Me; onClose: () => void; onChanged: () => void;
}) {
  const manage = domain.can.includes("manage");
  const [tab, setTab] = useState<"general" | "people" | "history">(manage ? "general" : "people");
  return (
    <Modal title={`${domain.name} settings`} onClose={onClose}>
      <div className="tabs">
        {manage && <button className={tab === "general" ? "on" : ""} onClick={() => setTab("general")}>General</button>}
        <button className={tab === "people" ? "on" : ""} onClick={() => setTab("people")}>People</button>
        {manage && <button className={tab === "history" ? "on" : ""} onClick={() => setTab("history")}>History</button>}
      </div>
      {tab === "general" && <General d={domain} onChanged={onChanged} />}
      {tab === "people" && <People d={domain} me={me} />}
      {tab === "history" && <DomainHistory d={domain} />}
    </Modal>
  );
}
