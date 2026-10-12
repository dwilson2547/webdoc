import { useEffect, useState } from "react";
import { api, errText, type Domain, type Home as HomeData, type Me, type Scope } from "./api";
import { ActivityList } from "./Activity";
import { Link, navigate } from "./router";
import { ROLE_TEXT, ago, Modal } from "./util";

function DomainCard({ d }: { d: Domain }) {
  return (
    <Link to={`/domain/${d.id}`} className="card">
      <div className="card-title">{d.name}</div>
      {d.description && <div className="card-desc">{d.description}</div>}
      <div className="card-meta muted">
        {d.documents} {d.documents === 1 ? "item" : "items"} · {ago(d.last_activity)}
        {d.owner && d.scope === "personal" && d.role !== "owner" && <> · {d.owner}'s</>}
        {d.role && d.role !== "reader" && d.role !== "owner" && <span className="badge">{ROLE_TEXT[d.role]}</span>}
      </div>
    </Link>
  );
}

function Section({ title, hint, domains, empty, action }: {
  title: string; hint?: string; domains: Domain[]; empty: string; action?: React.ReactNode;
}) {
  return (
    <section className="home-section">
      <div className="row">
        <h2>{title}</h2>
        {hint && <span className="muted">{hint}</span>}
        <span className="grow" />
        {action}
      </div>
      {domains.length ? <div className="cards">{domains.map((d) => <DomainCard key={d.id} d={d} />)}</div>
        : <p className="muted">{empty}</p>}
    </section>
  );
}

function NewDomain({ me, scope, onClose }: { me: Me; scope: Scope; onClose: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const create = async () => {
    try {
      const d = await api.createDomain({ name: name.trim(), description, scope });
      navigate(`/domain/${d.id}`);
    } catch (e) {
      setError(errText(e));
    }
  };
  return (
    <Modal title={scope === "public" ? "New public domain" : "New domain"} onClose={onClose}>
      <p className="muted">
        {scope === "public"
          ? "Public domains are readable by everyone, signed in or not."
          : `Domains are ${me.username}'s closed taxonomy: agents can file things in them but never add or change them.`}
      </p>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <label>Name<input autoFocus value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
        <label>Description <span className="muted">(agents read this to decide what belongs here)</span>
          <textarea rows={3} value={description} maxLength={2000} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="row"><button type="submit" className="primary" disabled={!name.trim()}>Create</button></div>
      </form>
    </Modal>
  );
}

export function Home({ me }: { me: Me | null }) {
  const [data, setData] = useState<HomeData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<Scope | null>(null);
  useEffect(() => { api.home().then(setData, (e) => setError(errText(e))); }, [me?.id]);
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <div className="page">
      <Section title="Public" hint="open to everyone" domains={data.public} empty="No public domains yet."
        action={me?.is_admin && <button onClick={() => setCreating("public")}>New public domain</button>} />
      {me && (
        <Section title="Personal" hint="your own taxonomy" domains={data.personal}
          empty="No domains yet." action={<button onClick={() => setCreating("personal")}>New domain</button>} />
      )}
      {me && data.shared.length > 0 && <Section title="Shared with you" domains={data.shared} empty="" />}
      {!me && <p className="muted">Sign in to keep your own notes and documents.</p>}
      <section className="home-section">
        <div className="row"><h2>Recent activity</h2><span className="grow" /><Link to="/activity">All</Link></div>
        <ActivityList limit={8} compact />
      </section>
      {creating && me && <NewDomain me={me} scope={creating} onClose={() => setCreating(null)} />}
    </div>
  );
}
