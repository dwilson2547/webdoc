import { useEffect, useRef, useState } from "react";
import { api, errText, type FieldDef, type Me, type Scope, type TemplateSpec } from "./api";
import { MarkdownEditor, type EditorHandle } from "./markdown";
import { GuidancePanel } from "./TemplateParts";
import { Modal, ago, parseTags } from "./util";

const HELP = `Each heading is a section. Put guidance for a section in an HTML comment under its heading
(<!-- like this -->): agents and people see it while writing, and it is never saved in the document.
End a heading with "(optional)" if the section may be left out; every other section is required.`;

type Draft = {
  id: string | null; scope: Scope; type: string; name: string; description: string; skeleton: string;
  fields: FieldDef[]; default_tags: string;
};

function TemplateEditor({ draft, me, onDone, onClose }: {
  draft: Draft; me: Me; onDone: () => void; onClose: () => void;
}) {
  const [d, setD] = useState(draft);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<TemplateSpec | null>(null);
  const editor = useRef<EditorHandle>(null);
  const set = (patch: Partial<Draft>) => setD((x) => ({ ...x, ...patch }));
  const setField = (i: number, patch: Partial<FieldDef>) =>
    set({ fields: d.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)) });

  const save = async () => {
    setError(null);
    const skeleton = editor.current!.getMarkdown();
    const body = { name: d.name, description: d.description, skeleton,
      fields: d.fields.filter((f) => f.name.trim()), default_tags: parseTags(d.default_tags) };
    try {
      if (d.id) await api.patchTemplate(d.id, body);
      else await api.createTemplate({ ...body, scope: d.scope, type: d.type });
      onDone();
    } catch (e) {
      setError(errText(e));
    }
  };
  const showPreview = () => {
    // Same parsing as the server: headings, comments, "(optional)".
    const sk = editor.current!.getMarkdown();
    const lines = sk.split("\n");
    const sections: TemplateSpec["sections"] = [];
    const stack: { level: number; text: string }[] = [];
    let fence = false;
    lines.forEach((ln, i) => {
      if (/^ {0,3}(```|~~~)/.test(ln)) fence = !fence;
      const m = !fence && ln.match(/^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (!m) return;
      const level = m[1].length;
      while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
      const optional = /\(optional\)\s*$/i.test(m[2]);
      const text = m[2].replace(/\s*\(optional\)\s*$/i, "");
      stack.push({ level, text });
      let j = i + 1;
      const own: string[] = [];
      while (j < lines.length && !/^ {0,3}#{1,6}\s/.test(lines[j])) own.push(lines[j++]);
      const guidance = [...own.join("\n").matchAll(/<!--([\s\S]*?)-->/g)].map((x) => x[1].trim().replace(/\s+/g, " ")).join(" ");
      sections.push({ path: stack.map((x) => x.text), required: !optional, guidance });
    });
    setPreview({ id: null, type: d.type, name: d.name || d.type, description: d.description, source: "personal",
      version: 0, fields: d.fields, default_tags: [], sections });
  };

  return (
    <Modal title={d.id ? `Edit ${draft.name}` : "New document type"} onClose={onClose} wide>
      <div className="stack">
        <div className="row">
          <label className="grow">Name<input value={d.name} maxLength={100} onChange={(e) => set({ name: e.target.value })} /></label>
          <label>Type <span className="muted">(what agents ask for)</span>
            <input value={d.type} disabled={!!d.id} placeholder="architecture" maxLength={64}
              onChange={(e) => set({ type: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} /></label>
          {!d.id && me.is_admin && (
            <label>For
              <select value={d.scope} onChange={(e) => set({ scope: e.target.value as Scope })}>
                <option value="personal">My domains</option>
                <option value="public">Public domains</option>
              </select>
            </label>
          )}
        </div>
        <label>Description<input value={d.description} maxLength={2000} onChange={(e) => set({ description: e.target.value })} /></label>
        <div className="field-rows">
          <div className="muted">Fields (frontmatter values each document of this type carries)</div>
          {d.fields.map((f, i) => (
            <div className="row" key={i}>
              <input placeholder="name" value={f.name} onChange={(e) => setField(i, { name: e.target.value.toLowerCase() })} />
              <input className="grow" placeholder="what it holds" value={f.description} onChange={(e) => setField(i, { description: e.target.value })} />
              <label className="check"><input type="checkbox" checked={f.required} onChange={(e) => setField(i, { required: e.target.checked })} />required</label>
              <button onClick={() => set({ fields: d.fields.filter((_, j) => j !== i) })}>Remove</button>
            </div>
          ))}
          <button onClick={() => set({ fields: [...d.fields, { name: "", description: "", required: false }] })}>Add field</button>
        </div>
        <label>Default tags<input value={d.default_tags} placeholder="comma, separated" onChange={(e) => set({ default_tags: e.target.value })} /></label>
        <p className="muted" style={{ whiteSpace: "pre-line", margin: 0 }}>{HELP}</p>
        <MarkdownEditor ref={editor} initial={d.skeleton} height="380px" />
        {preview && <GuidancePanel spec={preview} />}
        {error && <p className="error">{error}</p>}
        <div className="row">
          <button className="primary" disabled={!d.name.trim() || !d.type.trim()} onClick={() => void save()}>Save</button>
          <button onClick={showPreview}>Preview sections</button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </Modal>
  );
}

const SOURCE: Record<string, string> = { personal: "Your domains", public: "All public domains", domain: "One domain", "built-in": "Built-in" };

export function Templates({ me }: { me: Me }) {
  const [mine, setMine] = useState<TemplateSpec[]>([]);
  const [builtin, setBuiltin] = useState<TemplateSpec[]>([]);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.templates().then((r) => { setMine(r.templates); setBuiltin(r.builtin); }, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, []);

  const edit = (t: TemplateSpec, copy = false) => setEditing({
    id: copy ? null : t.id, scope: t.source === "public" ? "public" : "personal", type: t.type,
    name: t.name, description: t.description, skeleton: t.skeleton ?? "", fields: t.fields,
    default_tags: t.default_tags.join(", "),
  });
  const remove = async (t: TemplateSpec) => {
    if (!confirm(`Delete "${t.name}"? Documents keep their content; a built-in of the same type takes over for new ones.`)) return;
    await api.deleteTemplate(t.id!).catch((e) => setError(errText(e)));
    void load();
  };
  const overridden = new Set(mine.filter((t) => t.source === "personal").map((t) => t.type));

  return (
    <div className="page">
      <div className="row"><h1>Document types</h1><span className="grow" />
        <button className="primary" onClick={() => setEditing({ id: null, scope: "personal", type: "", name: "", description: "",
          skeleton: "## Summary\n<!-- What this is about, in a paragraph. -->\n", fields: [], default_tags: "" })}>New type</button>
      </div>
      <p className="muted">Ask an agent for "an architecture document" and it uses the type with that name: a domain's own first,
        then yours (or the public ones for a public domain), then the built-in.</p>
      {error && <p className="error">{error}</p>}
      <h2>Yours</h2>
      {mine.length === 0 && <p className="muted">None yet. Customize a built-in or start a new one.</p>}
      <ul className="tpl-list">
        {mine.map((t) => (
          <li key={t.id} className="row">
            <strong>{t.name}</strong> <code>{t.type}</code>
            <span className="muted">{SOURCE[t.source]}{t.domain ? `: ${t.domain}` : ""} · v{t.version}{t.updated_at && ` · ${ago(t.updated_at)}`}</span>
            <span className="grow" />
            <button onClick={() => edit(t)}>Edit</button>
            <button onClick={() => void remove(t)}>Delete</button>
          </li>
        ))}
      </ul>
      <h2>Built-in</h2>
      <ul className="tpl-list">
        {builtin.map((t) => (
          <li key={t.type} className="row">
            <strong>{t.name}</strong> <code>{t.type}</code>
            <span className="muted">{t.description}{overridden.has(t.type) && " · replaced by yours"}</span>
            <span className="grow" />
            <button onClick={() => edit(t, true)}>Customize</button>
          </li>
        ))}
      </ul>
      {editing && <TemplateEditor draft={editing} me={me} onClose={() => setEditing(null)} onDone={() => { setEditing(null); void load(); }} />}
    </div>
  );
}
