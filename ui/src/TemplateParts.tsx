import type { FieldDef, TemplateCheck, TemplateSpec } from "./api";

/** A document type's sections with their guidance (design §6). Guidance is for
 *  whoever is writing; it is never stored in the document. */
export function GuidancePanel({ spec, check }: { spec: TemplateSpec; check?: TemplateCheck }) {
  const missing = new Set(check?.missing_sections ?? []);
  return (
    <aside className="guidance">
      <h3>{spec.name}</h3>
      {spec.description && <p className="muted">{spec.description}</p>}
      <ol>
        {spec.sections.map((sec) => {
          const key = sec.path.join(" > ");
          return (
            <li key={key} className={missing.has(key) ? "missing" : ""} style={{ marginLeft: (sec.path.length - 1) * 12 }}>
              <strong>{sec.path[sec.path.length - 1]}</strong>
              {!sec.required && <span className="muted"> (optional)</span>}
              {missing.has(key) && <span className="error"> missing</span>}
              {sec.guidance && <div className="muted">{sec.guidance}</div>}
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

/** Inputs for a document's fields: the type's declared ones first, then any
 *  others the document already has. */
export function FieldsEditor({ defs, values, onChange, missing }: {
  defs: FieldDef[]; values: Record<string, string>; onChange: (v: Record<string, string>) => void; missing?: string[];
}) {
  const extra = Object.keys(values).filter((k) => !defs.some((d) => d.name === k));
  const rows: FieldDef[] = [...defs, ...extra.map((name) => ({ name, description: "", required: false }))];
  if (!rows.length) return null;
  return (
    <div className="fields-edit">
      {rows.map((f) => (
        <label key={f.name} title={f.description}>
          <span>{f.name}{f.required && <span className="error">*</span>}</span>
          <input value={values[f.name] ?? ""} placeholder={f.description}
            className={missing?.includes(f.name) ? "invalid" : ""}
            onChange={(e) => onChange({ ...values, [f.name]: e.target.value })} />
        </label>
      ))}
    </div>
  );
}

export function FieldsView({ fields }: { fields: Record<string, string> }) {
  const entries = Object.entries(fields);
  if (!entries.length) return null;
  return (
    <dl className="fields-view">
      {entries.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  );
}
