import { useEffect, useState } from "react";
import { api, errText, type ReportReview, type ReportT } from "./api";
import { MarkdownView } from "./markdown";
import { Link } from "./router";
import { ago, Modal } from "./util";

// Reports for the domains this person moderates; admins also get reports on
// personal documents (design §4). A personal document can't be browsed by an
// admin: Review opens just that one, while the report is open, and the owner
// sees in their activity that it was opened.

function Review({ report, onClose }: { report: ReportT; onClose: () => void }) {
  const [data, setData] = useState<ReportReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showReported, setShowReported] = useState(false);
  useEffect(() => { api.reviewReport(report.id).then(setData, (e) => setError(errText(e))); }, [report.id]);
  return (
    <Modal title={`Review: ${report.title ?? "document"}`} onClose={onClose} wide>
      {error && <p className="error">{error}</p>}
      {data && <>
        <p className="muted">
          {data.scope === "public" ? data.domain : `${data.owner}'s ${data.domain}`} · revision {data.revision}, updated {ago(data.updated_at)}
          {data.scope !== "public" && " · opening this is recorded in the owner's activity"}
        </p>
        {data.reported && (
          <div className="banner">
            Changed since it was reported (revision {data.reported.revision}).
            <button onClick={() => setShowReported((x) => !x)}>{showReported ? "Show current" : "Show as reported"}</button>
          </div>
        )}
        <h2>{showReported && data.reported ? data.reported.title : data.title}</h2>
        <MarkdownView key={showReported ? "old" : "now"} markdown={showReported && data.reported ? data.reported.body : data.body} />
      </>}
    </Modal>
  );
}

export function ModerationPage({ onCount }: { onCount: () => void }) {
  const [reports, setReports] = useState<ReportT[] | null>(null);
  const [recent, setRecent] = useState<ReportT[]>([]);
  const [reviewing, setReviewing] = useState<ReportT | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api.reports().then((r) => { setReports(r.reports); setRecent(r.recent); onCount(); }, (e) => setError(errText(e)));
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const resolve = async (r: ReportT, action: "dismiss" | "hide") => {
    const note = prompt(action === "hide" ? "Reason shown to the author (blank: the report's reason)" : "Note (optional)");
    if (note === null) return;
    await api.resolveReport(r.id, action, note).catch((e) => setError(errText(e)));
    void load();
  };
  const undo = async (r: ReportT) => {
    await api.undoReport(r.id).catch((e) => setError(errText(e)));
    void load();
  };
  return (
    <div className="page narrow">
      <div className="crumbs"><Link to="/">Home</Link></div>
      <h1>Moderation</h1>
      <p className="muted">Open reports in the domains you moderate{" "}
        and, for admins, on personal documents, which you can only open from here.</p>
      {error && <p className="error">{error}</p>}
      {reports && reports.length === 0 && <p className="muted">Nothing reported.</p>}
      <ul className="docs">
        {reports?.map((r) => (
          <li key={r.id}>
            <div className="row">
              {r.scope === "public" ? <Link to={`/d/${r.document_id}`} className="doc-title">{r.title}</Link>
                : <span className="doc-title">{r.title}</span>}
              <span className="muted">{r.reply_id ? "· a reply" : ""} · {r.scope === "public" ? r.domain : "personal document"}</span>
              <span className="grow" />
              <button onClick={() => setReviewing(r)}>Review</button>
              <button onClick={() => void resolve(r, "dismiss")}>Dismiss</button>
              <button className="danger" onClick={() => void resolve(r, "hide")}>Hide it</button>
            </div>
            <p className="snippet"><strong>{r.by ?? "someone"}</strong>, {ago(r.created_at)}: {r.reason}</p>
            {r.reply_excerpt && <blockquote className="muted">{r.reply_excerpt}</blockquote>}
          </li>
        ))}
      </ul>
      {recent.length > 0 && <>
        <h3>Recently handled</h3>
        <ul className="docs">
          {recent.map((r) => (
            <li key={r.id} className="row">
              <span className="doc-title">{r.title}</span>
              <span className="muted">{r.status === "actioned" ? "hidden" : "dismissed"}{r.decided_at && ` ${ago(r.decided_at)}`}{r.note && ` · ${r.note}`}</span>
              <span className="grow" />
              {r.status === "actioned" && <button onClick={() => void undo(r)}>Undo hide</button>}
            </li>
          ))}
        </ul>
      </>}
      {reviewing && <Review report={reviewing} onClose={() => setReviewing(null)} />}
    </div>
  );
}
