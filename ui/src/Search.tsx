import { useEffect, useState, type ReactNode } from "react";
import { api, errText, type Domain, type FindResult, type HitMeta, type SearchResult } from "./api";
import { Link, navigate } from "./router";
import { ago } from "./util";

// Central search (design §8, §11): every source the viewer can read, semantic
// or literal (find), with filters. All state lives in the URL, so results can
// be linked to and Back works.

const KEYS = ["q", "mode", "source", "domain_id", "kind", "type", "tag", "author", "since", "whole_word", "case"] as const;
type State = Partial<Record<(typeof KEYS)[number], string>>;

const SINCE: Record<string, number> = { "7d": 7, "30d": 30, "1y": 365 };
const SOURCE_TEXT: Record<string, string> = { public: "Public", personal: "Personal", shared: "Shared" };

export function searchUrl(s: State): string {
  const q = new URLSearchParams();
  for (const k of KEYS) if (s[k]) q.set(k, s[k]!);
  return "/search?" + q.toString();
}

function escapeRe(s: string) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

/** Wrap matches of `re` in <mark>. */
function Marked({ text, re }: { text: string; re: RegExp | null }) {
  if (!re) return <>{text}</>;
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (!m[0]) continue;
    out.push(text.slice(last, m.index), <mark key={m.index}>{m[0]}</mark>);
    last = m.index! + m[0].length;
  }
  out.push(text.slice(last));
  return <>{out}</>;
}

function Meta({ h }: { h: HitMeta }) {
  return (
    <div className="doc-meta muted">
      <span className={"src src-" + h.source}>{SOURCE_TEXT[h.source]}{h.source === "shared" && h.owner && ` · ${h.owner}`}</span>
      <Link to={`/domain/${h.domain_id}`}>{h.domain}</Link>
      {h.path.includes("/") && <span>› {h.path.split("/").slice(0, -1).join(" / ")}</span>}
      <span className={"kind k-" + h.kind}>{h.type || h.kind}</span>
      {h.tags.map((t) => <span key={t} className="tag small">{t}</span>)}
      <span>{ago(h.updated_at)}</span>
    </div>
  );
}

function Filters({ s, set, domains }: { s: State; set: (p: State) => void; domains: Domain[] }) {
  const opt = (key: keyof State, value: string | undefined, label: string) => (
    <button key={`${key}:${label}`} className={"flat" + ((s[key] || undefined) === value ? " on" : "")} onClick={() => set({ [key]: value })}>{label}</button>
  );
  return (
    <aside className="filters">
      <h4>Mode</h4>
      <div className="seg">
        <button className={s.mode !== "find" ? "on" : ""} onClick={() => set({ mode: undefined })} title="By meaning">Semantic</button>
        <button className={s.mode === "find" ? "on" : ""} onClick={() => set({ mode: "find" })} title="Exact text">Find</button>
      </div>
      {s.mode === "find" && <>
        <label className="check"><input type="checkbox" checked={s.whole_word !== "0"} onChange={(e) => set({ whole_word: e.target.checked ? undefined : "0" })} />Whole word</label>
        <label className="check"><input type="checkbox" checked={s.case === "1"} onChange={(e) => set({ case: e.target.checked ? "1" : undefined })} />Match case</label>
      </>}
      <h4>Source</h4>
      {opt("source", undefined, "All")}
      {opt("source", "public", "Public")}
      {opt("source", "personal", "Personal")}
      {opt("source", "shared", "Shared with you")}
      <h4>Domain</h4>
      <select value={s.domain_id || ""} onChange={(e) => set({ domain_id: e.target.value || undefined })}>
        <option value="">All domains</option>
        {domains.map((d) => <option key={d.id} value={d.id}>{d.name}{d.scope === "public" ? " (public)" : d.owner && d.role !== "owner" ? ` (${d.owner})` : ""}</option>)}
      </select>
      <h4>Kind</h4>
      {opt("kind", undefined, "All")}
      {opt("kind", "document", "Documents")}
      {opt("kind", "note", "Notes")}
      <h4>Updated</h4>
      {opt("since", undefined, "Any time")}
      {opt("since", "7d", "Past week")}
      {opt("since", "30d", "Past month")}
      {opt("since", "1y", "Past year")}
      <h4>More</h4>
      <form className="stack tight" onSubmit={(e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        set({ type: String(f.get("type") || "").trim() || undefined, tag: String(f.get("tag") || "").trim() || undefined,
              author: String(f.get("author") || "").trim() || undefined });
      }}>
        <input name="type" placeholder="Type, e.g. architecture" defaultValue={s.type} key={"t" + s.type} />
        <input name="tag" placeholder="Tag" defaultValue={s.tag} key={"g" + s.tag} />
        <input name="author" placeholder="Author (username)" defaultValue={s.author} key={"a" + s.author} />
        <button type="submit">Apply</button>
      </form>
    </aside>
  );
}

export function SearchPage({ search }: { search: string }) {
  const s: State = Object.fromEntries(new URLSearchParams(search)) as State;
  const [domains, setDomains] = useState<Domain[]>([]);
  const [res, setRes] = useState<SearchResult | FindResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.home().then((h) => setDomains([...h.personal, ...h.shared, ...h.public]), () => undefined);
  }, []);
  useEffect(() => {
    setError(null);
    if (!s.q?.trim()) return setRes(null);
    const days = s.since ? SINCE[s.since] : undefined;
    const p = {
      q: s.q, source: s.source, domain_id: s.domain_id, kind: s.kind, type: s.type, tag: s.tag, author: s.author,
      updated_after: days ? new Date(Date.now() - days * 86400e3).toISOString() : undefined, limit: 30,
      whole_word: s.whole_word !== "0", case_sensitive: s.case === "1",
    };
    setBusy(true);
    (s.mode === "find" ? api.find(p) : api.search(p)).then(setRes, (e) => { setRes(null); setError(errText(e)); })
      .finally(() => setBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);
  const set = (patch: State) => navigate(searchUrl({ ...s, ...patch }));

  const terms = (s.q || "").toLowerCase().match(/\w{2,}/g) || [];
  const find = s.mode === "find";
  const re = !s.q ? null : find
    ? new RegExp(escapeRe(s.q.trim()), s.case === "1" ? "g" : "gi")
    : terms.length ? new RegExp(terms.map(escapeRe).join("|"), "gi") : null;

  return (
    <div className="page search-page">
      <div className="domain-body">
        <Filters s={s} set={set} domains={domains} />
        <section className="results">
          {!s.q && <p className="muted">Search everything you can read: public domains, your own, and those shared with you.
            Semantic search finds documents by meaning; Find looks up exact text such as an error message or identifier.</p>}
          {error && <p className="error">{error}</p>}
          {busy && !res && <p className="muted">Searching…</p>}
          {res && "semantic" in res && <>
            <p className="muted">
              {res.results.length ? `${res.results.length} result${res.results.length === 1 ? "" : "s"}` : "Nothing matched."}
              {!res.semantic && " · semantic search is unavailable, showing keyword matches"}
              {res.pending > 0 && ` · ${res.pending} recently changed ${res.pending === 1 ? "document is" : "documents are"} still being indexed`}
            </p>
            <ul className="docs">
              {res.results.map((h) => (
                <li key={h.id}>
                  <Link to={`/d/${h.id}`} className="doc-title">{h.title}</Link>
                  {h.heading && <span className="muted"> § {h.heading}</span>}
                  <Meta h={h} />
                  {h.snippet && <p className="snippet"><Marked text={h.snippet} re={re} /></p>}
                </li>
              ))}
            </ul>
          </>}
          {res && "whole_word" in res && <>
            <p className="muted">
              {res.results.length ? `${res.results.reduce((n, h) => n + h.count, 0)} occurrences in ${res.results.length} document${res.results.length === 1 ? "" : "s"}${res.limited ? " (more may exist; narrow the filters)" : ""}`
                : `"${res.text}" doesn't occur anywhere you can read.`}
            </p>
            <ul className="docs">
              {res.results.map((h) => (
                <li key={h.id}>
                  <Link to={`/d/${h.id}`} className="doc-title">{h.title}</Link>
                  <span className="muted"> · {h.count} {h.count === 1 ? "match" : "matches"}</span>
                  <Meta h={h} />
                  <ul className="find-lines">
                    {h.matches.map((m, i) => (
                      <li key={i}>
                        <span className="muted lineno">{m.in === "title" ? "title" : `L${m.line}`}</span>
                        {m.heading && <span className="muted"> § {m.heading} </span>}
                        <code><Marked text={m.text} re={re} /></code>
                      </li>
                    ))}
                    {h.count > h.matches.length && <li className="muted">and {h.count - h.matches.length} more</li>}
                  </ul>
                </li>
              ))}
            </ul>
          </>}
        </section>
      </div>
    </div>
  );
}
