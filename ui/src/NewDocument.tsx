import { useEffect, useRef, useState } from "react";
import { api, errText, type DomainDetail, type Kind } from "./api";
import { MarkdownEditor, type EditorHandle } from "./markdown";
import { Link, navigate } from "./router";
import { parseTags } from "./util";

export function NewDocument({ search }: { search: string }) {
  const params = new URLSearchParams(search);
  const domainId = params.get("domain") || "";
  const [kind, setKind] = useState<Kind>(params.get("kind") === "note" ? "note" : "document");
  const [domain, setDomain] = useState<DomainDetail | null>(null);
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [project, setProject] = useState(params.get("project") || "");
  const [error, setError] = useState<string | null>(null);
  const editor = useRef<EditorHandle>(null);
  useEffect(() => { api.domain(domainId).then(setDomain, (e) => setError(errText(e))); }, [domainId]);

  const create = async () => {
    try {
      const d = await api.createDocument({
        domain_id: domainId, kind, title: title.trim(), body: editor.current!.getMarkdown(),
        tags: parseTags(tags), project_id: project || null,
      });
      navigate(`/d/${d.id}`);
    } catch (e) {
      setError(errText(e));
    }
  };

  if (!domain) return <div className="page">{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</div>;
  return (
    <div className="page">
      <div className="crumbs"><Link to="/">Home</Link> › <Link to={`/domain/${domain.id}`}>{domain.name}</Link> › New {kind}</div>
      <div className="editing">
        <input className="title-input" autoFocus placeholder="Title" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
        <div className="row meta-edit">
          <label>Kind
            <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              <option value="document">Document</option>
              <option value="note">Note</option>
            </select>
          </label>
          <label>Project
            <select value={project} onChange={(e) => setProject(e.target.value)}>
              <option value="">None</option>
              {domain.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label className="grow">Tags <input value={tags} placeholder="comma, separated" onChange={(e) => setTags(e.target.value)} /></label>
        </div>
        <MarkdownEditor ref={editor} initial="" />
        {error && <p className="error">{error}</p>}
        <div className="row">
          <button className="primary" disabled={!title.trim()} onClick={() => void create()}>Create</button>
          <button onClick={() => history.back()}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
