import { useCallback, useEffect, useState } from "react";
import { api, errText, type ActivityEvent } from "./api";
import { Link } from "./router";
import { ago } from "./util";

// The activity feed (design §9.4, §11): what changed, who changed it and
// through which client, for every domain the viewer can read.

function target(e: ActivityEvent): string | null {
  if (e.target_kind === "document") return e.exists ? `/d/${e.target_id}` : null;
  if (e.target_kind === "domain") return `/domain/${e.target_id}`;
  if (e.target_kind === "project" && e.domain_id) return `/domain/${e.domain_id}`;
  if (e.target_kind === "proposal") return "/proposals";
  if (e.target_kind === "template") return "/templates";
  return null;
}

export function EventRow({ e, showDomain = true }: { e: ActivityEvent; showDomain?: boolean }) {
  const to = target(e);
  return (
    <li className="event">
      <div className="row">
        {to ? <Link to={to}>{e.summary}</Link> : <span>{e.summary}</span>}
        <span className="grow" />
        <span className="muted" title={new Date(e.at).toLocaleString()}>{ago(e.at)}</span>
      </div>
      <div className="muted event-meta">
        {e.by}{e.client !== "web" && <> via <span className="client">{e.client}</span></>}
        {showDomain && e.domain && e.domain_id && <> · <Link to={`/domain/${e.domain_id}`}>{e.domain}</Link></>}
      </div>
    </li>
  );
}

export function ActivityList({ domainId, limit = 30, compact = false }: { domainId?: string; limit?: number; compact?: boolean }) {
  const [events, setEvents] = useState<ActivityEvent[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback((before?: string) =>
    api.activity({ domain_id: domainId, before, limit }).then((r) => {
      setEvents((prev) => (before && prev ? [...prev, ...r.events] : r.events));
      setMore(r.more);
    }, (e) => setError(errText(e))), [domainId, limit]);
  useEffect(() => { void load(); }, [load]);
  if (error) return <p className="error">{error}</p>;
  if (!events) return <p className="muted">Loading…</p>;
  if (!events.length) return <p className="muted">No activity yet.</p>;
  return (
    <>
      <ul className="events">{events.map((e) => <EventRow key={e.id} e={e} showDomain={!domainId} />)}</ul>
      {more && (compact ? <Link to="/activity">All activity</Link>
        : <button onClick={() => void load(events[events.length - 1].at)}>Older</button>)}
    </>
  );
}

export function ActivityPage() {
  return (
    <div className="page narrow">
      <div className="crumbs"><Link to="/">Home</Link></div>
      <h1>Activity</h1>
      <p className="muted">Changes in every domain you can read, newest first.</p>
      <ActivityList />
    </div>
  );
}
