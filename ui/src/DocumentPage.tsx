import { useEffect, useRef, useState } from "react";
import { api, ApiError, errText, type Doc, type DomainDetail } from "./api";
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
  useEffect(() => { api.domain(doc.domain_id).then(setDomain, () => undefined); }, [doc.domain_id]);

  const save = async (base = doc.revision) => {
    setSaving(true);
    setError(null);
    try {
      onSaved(await api.saveDocument(doc.id, {
        base_revision: base, title: title.trim(), body: editor.current!.getMarkdown(),
        tags: parseTags(tags), project_id: project || null,
        // Emptied fields are sent as null, which removes them.
        fields: Object.fromEntries([...new Set([...Object.keys(doc.fields), ...Object.keys(fields)])]
          .map((k) => [k, fields[k]?.trim() ? fields[k].trim() : null])),
      }));
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
        <label>Project
          <select value={project} onChange={(e) => setProject(e.target.value)}>
            <option value="">None</option>
            {domain?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
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
    </div>
  );
}

export function DocumentPage({ id }: { id: string }) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  const load = () => api.document(id).then(setDoc, (e) => setError(errText(e)));
  useEffect(() => { setEditing(false); void load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <div className="page"><p className="error">{error}</p></div>;
  if (!doc) return <div className="page"><p className="muted">Loading…</p></div>;

  const trash = async () => {
    if (!confirm(`Move "${doc.title}" to the trash? It can be restored.`)) return;
    await api.trash(doc.id).catch((e) => setError(errText(e)));
    navigate(`/domain/${doc.domain_id}`);
  };
  const untrash = async () => setDoc(await api.untrash(doc.id));

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
        {editing ? (
          <Editing doc={doc} onCancel={() => setEditing(false)} onSaved={(d) => { setDoc(d); setEditing(false); }} />
        ) : (
          <>
            <div className="row doc-head">
              <h1>{doc.title}</h1>
              <span className="grow" />
              {doc.can_edit && !doc.deleted_at && <button className="primary" onClick={() => setEditing(true)}>Edit</button>}
              <button onClick={() => setHistory((h) => !h)}>History</button>
              <a className="button" href={`/api/documents/${doc.id}/export`}>.md</a>
              {doc.can_delete && !doc.deleted_at && <button onClick={() => void trash()}>Delete</button>}
            </div>
            <div className="doc-meta muted">
              <span className={"kind k-" + doc.kind}>{doc.template ? doc.template.name : doc.type ?? doc.kind}</span>
              {doc.tags.map((t) => <span key={t} className="tag small">{t}</span>)}
              <span>revision {doc.revision} · updated {ago(doc.updated_at)}</span>
            </div>
            <FieldsView fields={doc.fields} />
            {doc.template && (doc.template.check.missing_sections.length > 0 || doc.template.check.missing_fields.length > 0) && (
              <p className="muted check-note">
                Not a complete {doc.template.name}:{" "}
                {[...doc.template.check.missing_sections.map((x) => `section “${x}”`),
                  ...doc.template.check.missing_fields.map((x) => `field “${x}”`)].join(", ")} missing.
              </p>
            )}
            <MarkdownView markdown={doc.body} />
          </>
        )}
      </div>
      {history && <HistoryPanel doc={doc} onClose={() => setHistory(false)} onRestored={(d) => setDoc(d)} />}
      {!history && editing && doc.template && <GuidancePanel spec={doc.template} check={doc.template.check} />}
    </div>
  );
}
