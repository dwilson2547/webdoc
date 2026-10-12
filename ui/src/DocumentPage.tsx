import { useEffect, useRef, useState } from "react";
import { api, ApiError, errText, TAB_ID, type Doc, type DomainDetail, type Lease, type Me } from "./api";
import { useDocEvents } from "./live";
import { ShareDialog } from "./Share";
import { ContributionHead, ContributionThread, useScanGate } from "./Contribution";
import { HistoryPanel } from "./HistoryPanel";
import { MarkdownEditor, MarkdownView, type EditorHandle } from "./markdown";
import { Link, navigate } from "./router";
import { FieldsEditor, FieldsView, GuidancePanel } from "./TemplateParts";
import { ago, parseTags } from "./util";

function Editing({ doc, onSaved, onCancel }: { doc: Doc; onSaved: (d: Doc) => void; onCancel: () => void }) {
  const editor = useRef<EditorHandle>(null);
  const [title, setTitle] = useState(doc.title);
  const [tags, setTags] = useState(doc.tags.join(", "));
  const [project, setProject] = useState(doc.project_id ?? "");
  const [fields, setFields] = useState<Record<string, string>>(doc.fields);
  const [domain, setDomain] = useState<DomainDetail | null>(null);
  const [conflict, setConflict] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const gate = useScanGate();
  useEffect(() => { api.domain(doc.domain_id).then(setDomain, () => undefined); }, [doc.domain_id]);

  const save = async (base = doc.revision) => {
    setSaving(true);
    setError(null);
    try {
      // A published contribution's edits go public at once, past the secret scan.
      await gate.run(async (ack) => onSaved(await api.saveDocument(doc.id, {
        ack_warnings: ack,
        base_revision: base, title: title.trim(), body: editor.current!.getMarkdown(),
        tags: parseTags(tags), project_id: project || null,
        // Emptied fields are sent as null, which removes them.
        fields: Object.fromEntries([...new Set([...Object.keys(doc.fields), ...Object.keys(fields)])]
          .map((k) => [k, fields[k]?.trim() ? fields[k].trim() : null])),
      })));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(e.body?.detail?.current ?? null);
      else setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="editing">
      <input className="title-input" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} aria-label="Title" />
      <div className="row meta-edit">
        {doc.kind !== "contribution" && <label>Project
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">None</option>
            {domain?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>}
        <label className="grow">Tags <input value={tags} placeholder="comma, separated" onChange={(e) => setTags(e.target.value)} /></label>
      </div>
      {(doc.template || Object.keys(doc.fields).length > 0) && (
        <FieldsEditor defs={doc.template?.fields ?? []} values={fields} onChange={setFields}
          missing={doc.template?.check.missing_fields} />
      )}
      <MarkdownEditor ref={editor} initial={doc.body} />
      {conflict !== null && (
        <div className="banner warn">
          Someone saved revision {conflict} while you were editing. Your text is still here.
          Saving anyway makes yours the latest; theirs stays in the history.
          <button onClick={() => { setConflict(null); void save(conflict); }}>Save anyway</button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row">
        <button className="primary" disabled={saving || !title.trim()} onClick={() => void save()}>{saving ? "Saving…" : "Save"}</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {gate.dialog}
    </div>
  );
}

function clientText(client: string) {
  return client === "web" ? "" : ` via ${client.replace(/^token:/, "")}`;
}

export function DocumentPage({ id, me }: { id: string; me: Me | null }) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [lease, setLease] = useState<Lease | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [remote, setRemote] = useState<string | null>(null);
  const load = () => api.document(id).then((d) => { setDoc(d); setLease(d.lease); }, (e) => setError(errText(e)));
  useEffect(() => { setEditing(false); setRemote(null); setNotice(null); void load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  const mine = (l: Lease | null) => !!l && l.kind === "ui" && l.holder.endsWith(":" + TAB_ID);
  const other = lease && !mine(lease) ? lease : null;

  // Live updates: reload when someone else changes the document while it's
  // being read; while editing, say so instead (the save will ask first).
  useDocEvents(id, (e) => {
    if (e.type === "hello") {
      setLease(e.lease);
      if (doc && e.revision > doc.revision && !editing) void load();
    } else if (e.type === "lease") {
      setLease(e.lease);
    } else if (e.type === "revision") {
      if (!doc || e.revision <= doc.revision) return;
      const who = `${e.by.split(" via ")[0]}${clientText(e.client)}`;
      if (editing) {
        if (!(me && e.user_id === me.id && e.client === "web")) setRemote(`${who} saved revision ${e.revision} (${e.summary}) while you were editing.`);
      } else {
        void load();
        setNotice(`Updated by ${who}: ${e.summary}`);
      }
    } else if (e.type === "trashed" || e.type === "restored") {
      if (editing) setRemote(`${e.by} moved this document ${e.type === "trashed" ? "to the trash" : "out of the trash"}.`);
      else void load();
    } else if (["published", "reply", "confirmation", "moderation"].includes(e.type)) {
      if (!editing) void load();
    } else if (e.type === "purged") {
      setError(`${e.by} deleted this document permanently.`);
    }
  });
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 8000);
    return () => clearTimeout(t);
  }, [notice]);

  // While the editor is open this tab holds the lease, renewed every 15 s,
  // and gives it up on leaving (or on closing the tab).
  useEffect(() => {
    if (!editing) return;
    const renew = setInterval(() => {
      api.takeLease(id).then(setLease, (e) => {
        if (e instanceof ApiError && e.status === 423) setLease(e.body?.detail?.lease ?? null);
      });
    }, 15000);
    const bye = () => api.dropLeaseBeacon(id);
    window.addEventListener("pagehide", bye);
    return () => {
      clearInterval(renew);
      window.removeEventListener("pagehide", bye);
      bye();
    };
  }, [editing, id]);

  if (error) return <div className="page"><p className="error">{error}</p></div>;
  if (!doc) return <div className="page"><p className="muted">Loading…</p></div>;

  const startEdit = async (force = false) => {
    try {
      setLease(await api.takeLease(doc.id, force));
      setRemote(null);
      setEditing(true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 423) setLease(e.body?.detail?.lease ?? null);
      else setError(errText(e));
    }
  };
  const stopEdit = () => setEditing(false);
  const trash = async () => {
    if (!confirm(`Move "${doc.title}" to the trash? It can be restored.`)) return;
    await api.trash(doc.id).then(() => navigate(`/domain/${doc.domain_id}`), (e) => setError(errText(e)));
  };
  const untrash = async () => setDoc(await api.untrash(doc.id));
  const report = async () => {
    const reason = prompt(doc.domain_scope === "public"
      ? "What's wrong with it? Moderators read this."
      : "What's wrong with it? An admin reads this and may open this document (only this one) to review it.");
    if (!reason?.trim()) return;
    await api.report(doc.id, reason.trim()).then(() => alert("Reported. Thanks."), (e) => setError(errText(e)));
  };
  const self = !!other && other.kind === "ui" && !!me && other.user_id === me.id;
  const whoHolds = other && (other.kind === "agent" ? `${other.label.replace(" via token:", " via ")} (an agent)` : other.label);

  return (
    <div className={"page doc-page" + (history || (editing && doc.template) ? " with-side" : "")}>
      <div className="doc-main">
        <div className="crumbs">
          <Link to="/">Home</Link> › <Link to={`/domain/${doc.domain_id}`}>{doc.domain}</Link>
          {doc.project && <> › <span>{doc.project}</span></>}
        </div>
        {doc.deleted_at && (
          <div className="banner warn">
            In the trash since {new Date(doc.deleted_at).toLocaleString()}.
            {doc.can_delete && <button onClick={() => void untrash()}>Restore</button>}
          </div>
        )}
        {doc.status === "hidden" && !doc.contribution && (
          <div className="banner warn">Hidden after a report{doc.hidden_reason ? `: ${doc.hidden_reason}` : ""}. Only you can see it now.</div>
        )}
        {other && (
          <div className="banner lease">
            {editing
              ? <>{self ? "Another tab or window of yours" : whoHolds} took over editing. Your text is still here, but saving is blocked until {self ? "it lets go" : "they finish"}.</>
              : self ? <>You have this document open in the editor in another tab or window.</>
              : <>{whoHolds} {other.kind === "agent" ? "has this document locked for edits" : "is editing this document"}.</>}
            {doc.can_edit && !doc.deleted_at && (
              <button onClick={() => void startEdit(true)}>{editing ? "Take back" : "Edit anyway"}</button>
            )}
          </div>
        )}
        {notice && !editing && <div className="banner info">{notice}</div>}
        {editing && remote && <div className="banner warn">{remote} Saving will ask before replacing their change.</div>}
        {editing ? (
          <Editing doc={doc} onCancel={stopEdit} onSaved={(d) => { setDoc(d); stopEdit(); }} />
        ) : (
          <>
            <div className="row doc-head">
              <h1>{doc.title}</h1>
              <span className="grow" />
              {doc.can_edit && !doc.deleted_at && !other && <button className="primary" onClick={() => void startEdit()}>Edit</button>}
              <button onClick={() => setHistory((h) => !h)}>History</button>
              {doc.can_share && !doc.deleted_at && <button onClick={() => setSharing(true)}>Share</button>}
              {doc.can_report && !doc.contribution && <button className="flat-btn" onClick={() => void report()}>Report</button>}
              <a className="button" href={`/api/documents/${doc.id}/export`}>.md</a>
              {doc.can_delete && !doc.deleted_at && <button onClick={() => void trash()}>Delete</button>}
            </div>
            <div className="doc-meta muted">
              <span className={"kind k-" + doc.kind}>{doc.template ? doc.template.name : doc.type ?? doc.kind}</span>
              {doc.tags.map((t) => <span key={t} className="tag small">{t}</span>)}
              <span>revision {doc.revision} · updated {ago(doc.updated_at)}</span>
            </div>
            {doc.contribution && <ContributionHead doc={doc} onDoc={setDoc} onError={setError} />}
            <FieldsView fields={doc.fields} />
            {doc.template && (doc.template.check.missing_sections.length > 0 || doc.template.check.missing_fields.length > 0) && (
              <p className="muted check-note">
                Not a complete {doc.template.name}:{" "}
                {[...doc.template.check.missing_sections.map((x) => `section “${x}”`),
                  ...doc.template.check.missing_fields.map((x) => `field “${x}”`)].join(", ")} missing.
              </p>
            )}
            <MarkdownView markdown={doc.body} />
            {doc.contribution && <ContributionThread doc={doc} onDoc={setDoc} onError={setError} />}
          </>
        )}
      </div>
      {history && <HistoryPanel doc={doc} onClose={() => setHistory(false)} onRestored={(d) => setDoc(d)} />}
      {!history && editing && doc.template && <GuidancePanel spec={doc.template} check={doc.template.check} />}
      {sharing && <ShareDialog doc={doc} onClose={() => setSharing(false)} />}
    </div>
  );
}
