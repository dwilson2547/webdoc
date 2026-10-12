import { useEffect, useState } from "react";
import { api, errText, type Doc, type ShareLink, type SharedDoc } from "./api";
import { MarkdownView } from "./markdown";
import { FieldsView } from "./TemplateParts";
import { ago, Modal } from "./util";

// Read-only share links (api/webdoc/shares.py): the dialog on a document, and
// the page someone without an account sees at /s/<token>.

export function ShareDialog({ doc, onClose }: { doc: Doc; onClose: () => void }) {
  const [links, setLinks] = useState<ShareLink[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const load = () => api.shares(doc.id).then(setLinks, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, [doc.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const create = () => api.createShare(doc.id).then(load, (e) => setError(errText(e)));
  const revoke = async (l: ShareLink) => {
    if (!confirm("Revoke this link? Anyone using it loses access.")) return;
    await api.revokeShare(l.id).catch((e) => setError(errText(e)));
    void load();
  };
  const copy = async (l: ShareLink) => {
    await navigator.clipboard.writeText(l.url).catch(() => undefined);
    setCopied(l.id);
  };
  return (
    <Modal title="Share a read-only link" onClose={onClose} wide>
      <p className="muted">
        Anyone with a link can read this document's current text without signing in. They don't see its history,
        its domain or anything else. To let someone edit, give them a role on the domain instead (domain Settings).
      </p>
      {error && <p className="error">{error}</p>}
      {links && links.length === 0 && <p className="muted">No links yet.</p>}
      {links && links.length > 0 && (
        <table className="grid">
          <tbody>
            {links.map((l) => (
              <tr key={l.id}>
                <td><input readOnly value={l.url} className="share-url" onFocus={(e) => e.target.select()} aria-label="Link" /></td>
                <td className="muted">{l.views} {l.views === 1 ? "view" : "views"}{l.last_viewed_at && `, last ${ago(l.last_viewed_at)}`}</td>
                <td className="muted">by {l.by ?? "?"} {ago(l.created_at)}</td>
                <td className="row">
                  <button onClick={() => void copy(l)}>{copied === l.id ? "Copied" : "Copy"}</button>
                  <button className="danger" onClick={() => void revoke(l)}>Revoke</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row" style={{ marginTop: 12 }}><button className="primary" onClick={() => void create()}>Create link</button></div>
    </Modal>
  );
}

export function SharedPage({ token }: { token: string }) {
  const [doc, setDoc] = useState<SharedDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.shared(token).then(setDoc, (e) => setError(errText(e))); }, [token]);
  if (error) return <div className="page narrow"><p className="error">{error}</p></div>;
  if (!doc) return <div className="page narrow"><p className="muted">Loading…</p></div>;
  return (
    <div className="page narrow shared">
      <p className="muted shared-note">Shared with you read-only from webdoc</p>
      <h1>{doc.title}</h1>
      <div className="doc-meta muted">
        <span className={"kind k-" + doc.kind}>{doc.type ?? doc.kind}</span>
        {doc.tags.map((t) => <span key={t} className="tag small">{t}</span>)}
        <span>updated {ago(doc.updated_at)}</span>
      </div>
      <FieldsView fields={doc.fields} />
      <MarkdownView markdown={doc.body} />
    </div>
  );
}
