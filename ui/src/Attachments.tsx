import { useRef, useState } from "react";
import { api, errText, type Attachment } from "./api";
import { ago, size } from "./util";

// Files attached to a document (api/webdoc/attachments.py). Images pasted or
// dropped into the editor land here too; the body links to them by URL.

export function AttachmentsPanel({ docId, items, canEdit, uploads, onChange, onInsert }: {
  docId: string; items: Attachment[]; canEdit: boolean; uploads: boolean;
  onChange: (items: Attachment[]) => void; onInsert?: (markdown: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  if (!items.length && !(canEdit && uploads)) return null;

  const add = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    setError(null);
    try {
      const added: Attachment[] = [];
      for (const f of Array.from(files)) {
        const a = await api.upload(docId, f, f.name);
        added.push(a);
        onInsert?.(a.markdown);
      }
      onChange([...added.reverse(), ...items]);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };
  const remove = async (a: Attachment) => {
    if (!confirm(`Delete ${a.name}? Links to it in the page will stop working.`)) return;
    try {
      await api.deleteAttachment(a.id);
      onChange(items.filter((x) => x.id !== a.id));
    } catch (e) {
      setError(errText(e));
    }
  };
  const copy = async (a: Attachment) => {
    await navigator.clipboard?.writeText(a.markdown).catch(() => undefined);
    setCopied(a.id);
    setTimeout(() => setCopied((c) => (c === a.id ? null : c)), 1500);
  };

  return (
    <section className="attachments">
      <div className="row">
        <h3>Attachments</h3>
        <span className="grow" />
        {canEdit && uploads && <>
          <input ref={input} type="file" multiple hidden onChange={(e) => void add(e.target.files)} />
          <button disabled={busy} onClick={() => input.current?.click()}>{busy ? "Uploading…" : "Attach files"}</button>
        </>}
      </div>
      {items.length === 0 && <p className="muted">No files yet. Images pasted into the editor are kept here too.</p>}
      <ul>
        {items.map((a) => (
          <li key={a.id} className="row">
            {a.image && <img src={a.url} alt="" className="att-thumb" loading="lazy" />}
            <a href={a.url} target="_blank" rel="noreferrer">{a.name}</a>
            <span className="muted">{size(a.size)} · {a.by ?? "someone"} · {ago(a.created_at)}</span>
            <span className="grow" />
            {onInsert && <button onClick={() => onInsert(a.markdown)}>Insert</button>}
            <button className="flat-btn" onClick={() => void copy(a)}>{copied === a.id ? "Copied" : "Copy link"}</button>
            {canEdit && <button className="flat-btn" onClick={() => void remove(a)}>Delete</button>}
          </li>
        ))}
      </ul>
      {error && <p className="error">{error}</p>}
    </section>
  );
}
