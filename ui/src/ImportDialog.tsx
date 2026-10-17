import { useState } from "react";
import { api, errText, type ImportResult, type TreePage } from "./api";
import { ParentPicker } from "./PageTree";
import { Modal } from "./util";

// Import a .zip or .tar.gz of markdown (api/webdoc/importer.py): webdoc's own
// export round-trips, any folder of markdown works. A preview (dry run)
// comes first; nothing is written until Import.

export function ImportDialog({ domainId, pages, under, onClose, onDone }: {
  domainId: string; pages: TreePage[] | null; under: string | null;
  onClose: () => void; onDone: (r: ImportResult) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState<string | null>(under);
  const [plan, setPlan] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (dry: boolean) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.importArchive(domainId, file, target, dry);
      if (dry) setPlan(r);
      else onDone(r);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const nothing = plan && !plan.created.length && !plan.updated.length && !plan.attachments.length;

  return (
    <Modal title="Import markdown" onClose={onClose} wide>
      <p className="muted">
        A .zip or .tar.gz: a webdoc export (pages that still exist here are updated, not duplicated) or any
        folder of markdown, such as an Obsidian vault or a repo's docs. Folders become parent pages, and
        images and files the pages link to become attachments.
      </p>
      <div className="stack tight">
        <label>Archive
          <input type="file" accept=".zip,.tar,.tar.gz,.tgz,application/zip,application/gzip"
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPlan(null); }} />
        </label>
        {pages && <label>Put new top-level pages under
          <ParentPicker pages={pages} value={target} onChange={(v) => { setTarget(v); setPlan(null); }} />
        </label>}
      </div>
      {plan && <ImportSummary r={plan} />}
      {error && <p className="error">{error}</p>}
      <div className="row" style={{ marginTop: 14 }}>
        {!plan
          ? <button className="primary" disabled={!file || busy} onClick={() => void run(true)}>{busy ? "Reading…" : "Preview"}</button>
          : <button className="primary" disabled={busy || !!nothing} onClick={() => void run(false)}>{busy ? "Importing…" : "Import"}</button>}
        <button onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

export function ImportSummary({ r }: { r: ImportResult }) {
  const verb = r.dry_run ? "Would " : "";
  const list = (title: string, items: string[]) => items.length > 0 && (
    <details open={items.length <= 8}>
      <summary>{title} ({items.length})</summary>
      <ul>{items.slice(0, 500).map((x, i) => <li key={i}>{x}</li>)}</ul>
    </details>
  );
  return (
    <div className="import-summary">
      {list(`${verb}create`.trim(), r.created.map((x) => x.path ?? `${x.title} (folder)`))}
      {list(`${verb}update`.trim(), r.updated.map((x) => x.path ?? x.title))}
      {list(`${verb}attach`.trim(), r.attachments.map((x) => `${x.path} → ${x.page}`))}
      {!r.dry_run && list("Unchanged", r.unchanged.map((x) => x.path ?? x.title))}
      {list("Not written", r.conflicts.map((x) => `${x.path}: ${x.reason}`))}
      {list("Skipped", r.skipped.map((x) => `${x.path}: ${x.reason}`))}
      {list("Warnings", r.warnings)}
      {r.dry_run && !r.created.length && !r.updated.length && !r.attachments.length &&
        <p className="muted">Nothing to import: every page is already here as it is.</p>}
    </div>
  );
}
