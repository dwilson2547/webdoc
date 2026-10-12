import { useEffect, useRef, useState } from "react";
import { api, errText, type DomainDetail, type Kind, type TemplateSpec } from "./api";
import { MarkdownEditor, type EditorHandle } from "./markdown";
import { Link, navigate } from "./router";
import { FieldsEditor, GuidancePanel } from "./TemplateParts";
import { parseTags } from "./util";

export function NewDocument({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const domainId = params.get("domain") || "";
  const [kind, setKind] = useState<Kind>(params.get("kind") === "note" ? "note" : "document");
  const [domain, setDomain] = useState<DomainDetail | null>(null);
  const [types, setTypes] = useState<TemplateSpec[]>([]);
  const [type, setType] = useState(params.get("type") || "");
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [project, setProject] = useState(params.get("project") || "");
  const [error, setError] = useState<string | null>(null);
  const [editorKey, setEditorKey] = useState(0);
  const editor = useRef<EditorHandle>(null);
  useEffect(() => {
    api.domain(domainId).then(setDomain, (e) => setError(errText(e)));
    api.domainTemplates(domainId).then(setTypes, () => setTypes([]));
  }, [domainId]);

  const spec = types.find((t) => t.type === type) ?? null;
  // Picking a type starts the body from its headings (the editor remounts with them).
  useEffect(() => {
    if (!spec) return;
    setTags((t) => t || spec.default_tags.join(", "));
    setEditorKey((k) => k + 1);
  }, [spec?.type]); // eslint-disable-line react-hooks/exhaustive-deps

  const create = async () => {
    try {
      const d = await api.createDocument({
        domain_id: domainId, kind, title: title.trim(), body: editor.current!.getMarkdown(),
        tags: parseTags(tags), project_id: project || null, type: spec ? spec.type : null, fields,
      });
      navigate(`/d/${d.id}`);
    } catch (e) {
      setError(errText(e));
    }
  };

  if (!domain) return <div className="page">{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</div>;
  return (
    <div className={"page" + (spec ? " doc-page with-side" : "")}>
      <div className="doc-main">
        <div className="crumbs"><Link to="/">Home</Link> › <Link to={`/domain/${domain.id}`}>{domain.name}</Link> › New {spec ? spec.name : kind}</div>
        <div className="editing">
          <input className="title-input" autoFocus placeholder="Title" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
          <div className="row meta-edit">
            <label>Kind
              <select value={kind} onChange={(e) => { setKind(e.target.value as Kind); if (e.target.value === "note") setType(""); }}>
                <option value="document">Document</option>
                <option value="note">Note</option>
              </select>
            </label>
            {kind === "document" && (
              <label>Type
                <select value={type} onChange={(e) => setType(e.target.value)}>
                  <option value="">None</option>
                  {types.map((t) => <option key={t.type} value={t.type}>{t.name}{t.source !== "built-in" ? "" : " (built-in)"}</option>)}
                </select>
              </label>
            )}
            <label>Project
              <select value={project} onChange={(e) => setProject(e.target.value)}>
                <option value="">None</option>
                {domain.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            <label className="grow">Tags <input value={tags} placeholder="comma, separated" onChange={(e) => setTags(e.target.value)} /></label>
          </div>
          {spec && <FieldsEditor defs={spec.fields} values={fields} onChange={setFields} />}
          <MarkdownEditor key={editorKey} ref={editor} initial={spec?.starter ?? ""} />
          {error && <p className="error">{error}</p>}
          <div className="row">
            <button className="primary" disabled={!title.trim()} onClick={() => void create()}>Create</button>
            <button onClick={() => history.back()}>Cancel</button>
          </div>
        </div>
      </div>
      {spec && <GuidancePanel spec={spec} />}
    </div>
  );
}
