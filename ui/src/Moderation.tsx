import { useEffect, useState } from "react";
import { api, errText, type ReportT } from "./api";
import { Link } from "./router";
import { ago } from "./util";

// Open reports for the domains this person moderates (admins: every public
// domain). Hiding here hides what was reported, the contribution or the reply.

export function ModerationPage({ onCount }: { onCount: () => void }) {
  const [reports, setReports] = useState<ReportT[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.reports().then((r) => { setReports(r.reports); onCount(); }, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const resolve = async (r: ReportT, action: "dismiss" | "hide") => {
    const note = prompt(action === "hide" ? "Reason shown to the author (blank: the report's reason)" : "Note (optional)") ;
    if (note === null) return;
    await api.resolveReport(r.id, action, note).catch((e) => setError(errText(e)));
    void load();
  };
  return (
    <div className="page narrow">
      <div className="crumbs"><Link to="/">Home</Link></div>
      <h1>Moderation</h1>
      <p className="muted">Open reports in the domains you moderate.</p>
      {error && <p className="error">{error}</p>}
      {reports && reports.length === 0 && <p className="muted">Nothing reported.</p>}
      <ul className="docs">
        {reports?.map((r) => (
          <li key={r.id}>
            <div className="row">
              <Link to={`/d/${r.document_id}`} className="doc-title">{r.title}</Link>
              <span className="muted">{r.reply_id ? "· a reply" : ""} · {r.domain}</span>
              <span className="grow" />
              <button onClick={() => void resolve(r, "dismiss")}>Dismiss</button>
              <button className="danger" onClick={() => void resolve(r, "hide")}>Hide it</button>
            </div>
            <p className="snippet"><strong>{r.by ?? "someone"}</strong>, {ago(r.created_at)}: {r.reason}</p>
            {r.reply_excerpt && <blockquote className="muted">{r.reply_excerpt}</blockquote>}
          </li>
        ))}
      </ul>
    </div>
  );
}
