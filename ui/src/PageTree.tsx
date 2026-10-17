import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api, errText, type TreePage } from "./api";
import { Link, navigate } from "./router";
import { Modal } from "./util";

// The page tree of a domain (api/webdoc/tree.py), as a wiki sidebar.
// Pages are dragged onto another page to go under it, or onto the top or
// bottom edge of a row to sit before or after it. Moving under another
// parent asks first; the question can be muted for a while or for good, as
// dan-wiki planned (its TODO: drag-and-drop page re-parenting).

type Kids = Map<string | null, TreePage[]>;

function childrenOf(pages: TreePage[]): Kids {
  const kids: Kids = new Map();
  for (const p of pages) {
    const list = kids.get(p.parent_id) ?? [];
    list.push(p);
    kids.set(p.parent_id, list);
  }
  return kids;
}

function below(kids: Kids, id: string): Set<string> {
  const out = new Set<string>();
  const todo = [id];
  while (todo.length) for (const c of kids.get(todo.pop()!) ?? []) { out.add(c.id); todo.push(c.id); }
  return out;
}

const store = {
  get(key: string): string | null { try { return localStorage.getItem(key); } catch { return null; } },
  set(key: string, v: string | null) {
    try { if (v === null) localStorage.removeItem(key); else localStorage.setItem(key, v); } catch { /* private mode */ }
  },
};

const MUTE_KEY = "wd-move-confirm-mute";
const muted = () => {
  const v = store.get(MUTE_KEY);
  return v === "always" || (!!v && Number(v) > Date.now());
};

type Zone = "before" | "inside" | "after";
type Pending = { page: TreePage; parent: TreePage | null; position: number | null };

export function usePageTree(domainId: string) {
  const [pages, setPages] = useState<TreePage[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => (domainId
    ? api.tree(domainId).then(setPages, (e) => setError(errText(e)))
    : Promise.resolve()), [domainId]);
  useEffect(() => { void reload(); }, [reload]);
  return { pages, error, reload };
}

export function PageTree({ domainId, currentId, canWrite, pages, reload, onMoved }: {
  domainId: string; currentId?: string; canWrite: boolean; pages: TreePage[] | null;
  reload: () => Promise<unknown>; onMoved?: (id: string) => void;
}) {
  const openKey = `wd-tree-open:${domainId}`;
  const [open, setOpen] = useState<Set<string>>(() => new Set(JSON.parse(store.get(openKey) || "[]")));
  const [drag, setDrag] = useState<string | null>(null);
  const [over, setOver] = useState<{ id: string | null; zone: Zone } | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState<string | null>(null);
  const kids = useMemo(() => childrenOf(pages ?? []), [pages]);
  const byId = useMemo(() => new Map((pages ?? []).map((p) => [p.id, p])), [pages]);

  // The current page and its ancestors open when you arrive on it.
  const openedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!currentId || !byId.has(currentId) || openedFor.current === currentId) return;
    openedFor.current = currentId;
    let p = byId.get(currentId);
    const add: string[] = [currentId];
    while (p?.parent_id) { add.push(p.parent_id); p = byId.get(p.parent_id); }
    if (add.some((id) => !open.has(id))) setOpen((o) => new Set([...o, ...add]));
  }, [currentId, byId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => store.set(openKey, JSON.stringify([...open])), [open, openKey]);
  const toggle = (id: string) => setOpen((o) => { const n = new Set(o); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  const doMove = async ({ page, parent, position }: Pending) => {
    try {
      await api.move(page.id, parent ? parent.id : null, position);
      if (parent) setOpen((o) => new Set([...o, parent.id]));
      await reload();
      onMoved?.(page.id);
    } catch (e) {
      setError(errText(e));
    }
  };

  const zoneOf = (e: DragEvent<HTMLElement>): Zone => {
    const r = e.currentTarget.getBoundingClientRect();
    const y = (e.clientY - r.top) / r.height;
    return y < 0.25 ? "before" : y > 0.75 ? "after" : "inside";
  };

  const allowed = (target: TreePage | null) => {
    if (!drag) return false;
    if (!target) return true;
    return target.id !== drag && !below(kids, drag).has(target.id);
  };

  const drop = (target: TreePage | null, zone: Zone) => {
    const page = drag ? byId.get(drag) : undefined;
    setDrag(null);
    setOver(null);
    if (!page || !allowed(target)) return;
    let parent: TreePage | null;
    let position: number | null;
    if (!target) {
      parent = null;
      position = null;
    } else if (zone === "inside") {
      parent = target;
      position = null;
    } else {
      parent = target.parent_id ? byId.get(target.parent_id) ?? null : null;
      const sibs = (kids.get(target.parent_id) ?? []).filter((p) => p.id !== page.id);
      position = sibs.findIndex((p) => p.id === target.id) + (zone === "after" ? 1 : 0);
    }
    const move = { page, parent, position };
    if ((parent?.id ?? null) === page.parent_id || muted()) void doMove(move);
    else setPending(move);
  };

  const row = (p: TreePage) => {
    const children = kids.get(p.id) ?? [];
    const isOpen = open.has(p.id);
    const zone = over?.id === p.id ? over.zone : null;
    return (
      <li key={p.id}>
        <div className={"tree-row" + (p.id === currentId ? " current" : "") + (zone ? " drop-" + zone : "")
            + (drag === p.id ? " dragging" : "")}
          draggable={canWrite}
          onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", p.id); setDrag(p.id); }}
          onDragEnd={() => { setDrag(null); setOver(null); }}
          onDragOver={(e) => {
            if (!allowed(p)) return;
            e.preventDefault();
            const z = zoneOf(e);
            if (over?.id !== p.id || over.zone !== z) setOver({ id: p.id, zone: z });
          }}
          onDragLeave={() => over?.id === p.id && setOver(null)}
          onDrop={(e) => { e.preventDefault(); drop(p, zoneOf(e)); }}>
          {children.length > 0
            ? <button className="tree-toggle" aria-label={isOpen ? "Collapse" : "Expand"} onClick={() => toggle(p.id)}>{isOpen ? "▾" : "▸"}</button>
            : <span className="tree-toggle" />}
          <Link to={`/d/${p.id}`} className="tree-link" title={p.title}>
            {p.kind === "note" && <span className="tree-note" title="Note">✎</span>}
            {p.title}
          </Link>
          {p.status !== "published" && <span className={"badge st-" + p.status}>{p.status}</span>}
          {canWrite && (
            <button className="tree-add" title="New page under this one"
              onClick={() => navigate(`/new?domain=${domainId}&kind=document&parent=${p.id}`)}>+</button>
          )}
        </div>
        {children.length > 0 && isOpen && <ul>{children.map(row)}</ul>}
      </li>
    );
  };

  if (!pages) return <p className="muted tree-empty">Loading…</p>;
  const top = kids.get(null) ?? [];
  return (
    <nav className="page-tree" aria-label="Pages">
      {top.length === 0 && <p className="muted tree-empty">No pages yet.</p>}
      <ul>{top.map(row)}</ul>
      {canWrite && drag && byId.get(drag)?.parent_id && (
        <div className={"tree-root-drop" + (over?.id === null ? " on" : "")}
          onDragOver={(e) => { e.preventDefault(); if (over?.id !== null) setOver({ id: null, zone: "inside" }); }}
          onDragLeave={() => setOver(null)}
          onDrop={(e) => { e.preventDefault(); drop(null, "inside"); }}>
          Drop here to move to the top
        </div>
      )}
      {error && <p className="error">{error} <button className="flat-btn" onClick={() => setError(null)}>×</button></p>}
      {pending && <MoveConfirm move={pending} onCancel={() => setPending(null)}
        onConfirm={(mute) => {
          if (mute) store.set(MUTE_KEY, mute === "always" ? "always" : String(Date.now() + mute * 60_000));
          const m = pending;
          setPending(null);
          void doMove(m);
        }} />}
    </nav>
  );
}

function MoveConfirm({ move, onConfirm, onCancel }: {
  move: Pending; onConfirm: (mute: number | "always" | null) => void; onCancel: () => void;
}) {
  const [mute, setMute] = useState(false);
  const [length, setLength] = useState<string>("5");
  return (
    <Modal title="Move page" onClose={onCancel}>
      <p>
        Move <strong>{move.page.title}</strong> {move.parent ? <>under <strong>{move.parent.title}</strong></> : "to the top of the domain"}?
        {" "}Its own pages move with it.
      </p>
      <div className="row">
        <label className="check"><input type="checkbox" checked={mute} onChange={(e) => setMute(e.target.checked)} />Don't ask again for</label>
        <select value={length} disabled={!mute} onChange={(e) => setLength(e.target.value)}>
          <option value="1">1 minute</option>
          <option value="5">5 minutes</option>
          <option value="30">30 minutes</option>
          <option value="60">1 hour</option>
          <option value="always">ever (on this browser)</option>
        </select>
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <button className="primary" autoFocus
          onClick={() => onConfirm(mute ? (length === "always" ? "always" : Number(length)) : null)}>Move</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </Modal>
  );
}

/** Pick a page to file under: a filterable list showing the tree's indentation. */
export function ParentPicker({ pages, value, exclude, onChange }: {
  pages: TreePage[]; value: string | null; exclude?: string; onChange: (id: string | null) => void;
}) {
  const [openList, setOpenList] = useState(false);
  const [q, setQ] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const kids = useMemo(() => childrenOf(pages), [pages]);
  const banned = useMemo(() => (exclude ? new Set([exclude, ...below(kids, exclude)]) : new Set<string>()), [kids, exclude]);
  const byId = useMemo(() => new Map(pages.map((p) => [p.id, p])), [pages]);
  const pathOf = (id: string | null) => {
    const parts: string[] = [];
    let p = id ? byId.get(id) : undefined;
    while (p) { parts.unshift(p.title); p = p.parent_id ? byId.get(p.parent_id) : undefined; }
    return parts.join(" / ");
  };
  useEffect(() => {
    if (!openList) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpenList(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [openList]);
  const needle = q.trim().toLowerCase();
  const shown = pages.filter((p) => !banned.has(p.id) && p.kind !== "contribution"
    && (!needle || pathOf(p.id).toLowerCase().includes(needle)));
  const pick = (id: string | null) => { onChange(id); setOpenList(false); setQ(""); };
  return (
    <div className="parent-picker" ref={box}>
      <button type="button" className="parent-current" onClick={() => setOpenList((o) => !o)} title="Where this page sits in the tree">
        {value ? pathOf(value) || "(a page you can't see)" : "Top of the domain"} ▾
      </button>
      {openList && (
        <div className="parent-list">
          <input autoFocus placeholder="Filter pages" value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") setOpenList(false); if (e.key === "Enter" && shown[0]) { e.preventDefault(); pick(shown[0].id); } }} />
          <ul>
            {!needle && <li className={value === null ? "on" : ""} onClick={() => pick(null)}><em>Top of the domain</em></li>}
            {shown.slice(0, 200).map((p) => (
              <li key={p.id} className={p.id === value ? "on" : ""} onClick={() => pick(p.id)}
                style={needle ? undefined : { paddingLeft: 8 + p.depth * 14 }} title={pathOf(p.id)}>
                {needle ? pathOf(p.id) : p.title}
              </li>
            ))}
            {shown.length === 0 && <li className="muted">No matching page</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
