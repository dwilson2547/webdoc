import { useEffect, useState } from "react";
import { api, errText, REPLY_KINDS, type Drafts as DraftsT } from "./api";
import { Link } from "./router";
import { ago } from "./util";

// Drafts waiting for a person to publish (design §12): contributions and
// replies, most of them written by agents. Publishing happens on the
// document page, after reading the draft in full.

export function DraftsPage({ onCount }: { onCount: () => void }) {
  const [data, setData] = useState<DraftsT | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.drafts().then((d) => { setData(d); onCount(); }, (e) => setError(errText(e))); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const via = (c: string) => (c === "web" ? "" : ` · from ${c.replace(/^token:/, "")}`);
  return (
    <div className="page narrow">
      <div className="crumbs"><Link to="/">Home</Link></div>
      <h1>Drafts</h1>
      <p className="muted">
        Contributions and replies for public domains start here, whether you or an agent wrote them. Only you
        can see them. Open each one, read it as the public will, and publish it from there.
      </p>
      {error && <p className="error">{error}</p>}
      {data && <>
        <h3>Contributions</h3>
        {data.contributions.length === 0 ? <p className="muted">None.</p> : (
          <ul className="docs">{data.contributions.map((c) => (
            <li key={c.id}>
              <Link to={`/d/${c.id}`} className="doc-title">{c.title}</Link>
              <div className="doc-meta muted"><span>{c.domain}</span>{c.type && <span className="kind k-contribution">{c.type}</span>}
                <span>{ago(c.updated_at)}{via(c.by_client)}</span></div>
            </li>))}
          </ul>)}
        <h3>Replies</h3>
        {data.replies.length === 0 ? <p className="muted">None.</p> : (
          <ul className="docs">{data.replies.map((r) => (
            <li key={r.id}>
              <span className="muted">{REPLY_KINDS[r.kind]} on </span><Link to={`/d/${r.document_id}`} className="doc-title">{r.title}</Link>
              <div className="doc-meta muted"><span>{ago(r.created_at)}{via(r.client)}</span></div>
              <p className="snippet">{r.excerpt}{r.excerpt.length >= 200 && "…"}</p>
            </li>))}
          </ul>)}
      </>}
    </div>
  );
}
