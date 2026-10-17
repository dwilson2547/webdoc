import { useEffect, useRef, useState } from "react";
import { api, errText, type DomainDetail, type Kind, type TemplateSpec } from "./api";
import { MarkdownEditor, type EditorHandle } from "./markdown";
import { ParentPicker, usePageTree } from "./PageTree";
import { Link, navigate } from "./router";
import { FieldsEditor, GuidancePanel } from "./TemplateParts";
import { parseTags } from "./util";

export function NewDocument({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const domainId = params.get("domain") || "";
  const initialKind = params.get("kind");
  const [kind, setKind] = useState<Kind>(initialKind === "note" || initialKind === "contribution" ? initialKind : "document");
  const [domain, setDomain] = useState<DomainDetail | null>(null);
  const [types, setTypes] = useState<TemplateSpec[] | null>(null);
  const [type, setType] = useState(params.get("type") || "");
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [parent, setParent] = useState<string | null>(params.get("parent") || null);
  const tree = usePageTree(domainId);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef<EditorHandle>(null);
  useEffect(() => {
    api.domain(domainId).then(setDomain, (e) => setError(errText(e)));
    api.domainTemplates(domainId).then(setTypes, () => setTypes([]));
  }, [domainId]);
  // A contribution domain may only take certain types; pick the only one.
  const allowed = kind === "contribution" && domain?.contribution_types.length
    ? (types ?? []).filter((t) => domain.contribution_types.includes(t.type)) : types ?? [];
  useEffect(() => {
    if (kind === "contribution" && domain?.contribution_types.length === 1) setType(domain.contribution_types[0]);
  }, [kind, domain?.contribution_types]); // eslint-disable-line react-hooks/exhaustive-deps

  const spec = types?.find((t) => t.type === type) ?? null;
  // Mount the editor once the domain, its types and any automatic type
  // choice have settled: Toast UI throws if destroyed right after creation.
  const only = kind === "contribution" && domain?.contribution_types.length === 1 ? domain.contribution_types[0] : null;
  const ready = !!domain && types !== null && (!only || type === only);
  // Picking a type starts the body from its headings (the editor remounts with them).
  useEffect(() => {
    if (!spec) return;
    setTags((t) => t || spec.default_tags.join(", "));
  }, [spec?.type]); // eslint-disable-line react-hooks/exhaustive-deps

  const create = async () => {
    try {
      const d = await api.createDocument({
        domain_id: domainId, kind, title: title.trim(), body: editor.current!.getMarkdown(),
        tags: parseTags(tags), parent_id: kind === "contribution" ? null : parent, type: spec ? spec.type : null, fields,
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
            {kind !== "contribution" && <label>Kind
              <select value={kind} onChange={(e) => { setKind(e.target.value as Kind); if (e.target.value === "note") setType(""); }}>
                <option value="document">Document</option>
                <option value="note">Note</option>
              </select>
            </label>}
            {kind !== "note" && (
              <label>Type
                <select value={type} onChange={(e) => setType(e.target.value)}>
                  {!(kind === "contribution" && domain.contribution_types.length) && <option value="">None</option>}
                  {allowed.map((t) => <option key={t.type} value={t.type}>{t.name}{t.source !== "built-in" ? "" : " (built-in)"}</option>)}
                </select>
              </label>
            )}
            {kind !== "contribution" && tree.pages && <label>Under
              <ParentPicker pages={tree.pages} value={parent} onChange={setParent} />
            </label>}
            <label className="grow">Tags <input value={tags} placeholder="comma, separated" onChange={(e) => setTags(e.target.value)} /></label>
          </div>
          {kind === "contribution" && (
            <p className="banner draft">This starts as a draft only you can see. You publish it after reading it through;
              it's then public to everyone, so leave out hostnames, addresses, internal paths, project names and secrets.</p>
          )}
          {spec && <FieldsEditor defs={spec.fields} values={fields} onChange={setFields} />}
          {ready ? <MarkdownEditor key={spec?.type ?? ""} ref={editor} initial={spec?.starter ?? ""}
            onUploadError={(m) => setError(m)} /> : <p className="muted">Loading…</p>}
          {error && <p className="error">{error}</p>}
          <div className="row">
            <button className="primary" disabled={!title.trim()} onClick={() => void create()}>{kind === "contribution" ? "Save draft" : "Create"}</button>
            <button onClick={() => history.back()}>Cancel</button>
          </div>
        </div>
      </div>
      {spec && <GuidancePanel spec={spec} />}
    </div>
  );
}
