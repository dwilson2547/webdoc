import { useState, type ReactNode } from "react";
import { api, ApiError, errText, REPLY_KINDS, type Doc, type ReplyKind, type ReplyT, type ScanResult } from "./api";
import { MarkdownView } from "./markdown";
import { Link } from "./router";
import { ago, Modal } from "./util";

// Public contributions (design §12, api/webdoc/contributions.py): the publish
// gate with its secret scan, status banners, moderator actions, and the
// thread of confirmations and replies under a contribution.

type Gate = { scan: ScanResult; needsAck: boolean; message: string; retry: () => Promise<void> };

/** Run something that may be stopped by the secret scan (HTTP 422 with a scan
 * result). Secrets block; warnings can be acknowledged and retried. */
export function useScanGate() {
  const [gate, setGate] = useState<Gate | null>(null);
  const run = async (fn: (ack: boolean) => Promise<unknown>): Promise<boolean> => {
    try {
      await fn(false);
      return true;
    } catch (e) {
      const d = e instanceof ApiError ? e.body?.detail : null;
      if (e instanceof ApiError && e.status === 422 && d?.scan) {
        setGate({ scan: d.scan, needsAck: d.needs_ack, message: d.message, retry: async () => { await fn(true); } });
        return false;
      }
      throw e;
    }
  };
  const dialog = gate && <ScanDialog gate={gate} onClose={() => setGate(null)} />;
  return { run, dialog };
}

function ScanDialog({ gate, onClose }: { gate: Gate; onClose: () => void }) {
  const [ok, setOk] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const list = (title: string, items: ScanResult["secrets"]) => items.length > 0 && (
    <>
      <h4>{title}</h4>
      <ul className="scan-list">{items.map((f, i) => <li key={i}><strong>{f.kind}</strong> on line {f.line} <code>{f.excerpt}</code></li>)}</ul>
    </>
  );
  return (
    <Modal title={gate.needsAck ? "Check before it goes public" : "Remove secrets first"} onClose={onClose}>
      <p>{gate.message}.</p>
      {list("Secrets (must be removed)", gate.scan.secrets)}
      {list("Possibly private", gate.scan.warnings)}
      {gate.needsAck && (
        <label className="check"><input type="checkbox" checked={ok} onChange={(e) => setOk(e.target.checked)} />
          I've checked these and they're fine to make public</label>
      )}
      {error && <p className="error">{error}</p>}
      <div className="row" style={{ marginTop: 10 }}>
        {gate.needsAck && <button className="primary" disabled={!ok} onClick={() => gate.retry().then(onClose, (e) => setError(errText(e)))}>Publish anyway</button>}
        <button onClick={onClose}>{gate.needsAck ? "Cancel" : "Close and edit"}</button>
      </div>
    </Modal>
  );
}

function ask(text: string, fallback = ""): string | null {
  const v = prompt(text, fallback);
  return v === null ? null : v.trim();
}

/** Banners and actions above a contribution's body. */
export function ContributionHead({ doc, onDoc, onError }: { doc: Doc; onDoc: (d: Doc) => void; onError: (m: string) => void }) {
  const t = doc.contribution!;
  const { run, dialog } = useScanGate();
  const act = (p: Promise<Doc>) => p.then(onDoc, (e) => onError(errText(e)));
  const publish = () => run((ack) => api.publish(doc.id, ack).then(onDoc)).catch((e) => onError(errText(e)));
  const report = async () => {
    const reason = ask("What's wrong with it? Moderators read this.");
    if (!reason) return;
    await api.report(doc.id, reason).then(() => alert("Reported. Thanks."), (e) => onError(errText(e)));
  };
  const duplicate = () => {
    const ref = ask("Duplicate of which contribution? Paste its link or id.");
    const id = ref?.match(/[a-f0-9]{32}/)?.[0];
    if (ref && !id) return onError("That isn't a webdoc document link or id.");
    if (id) void act(api.moderate(doc.id, "duplicate", "", id));
  };
  return (
    <>
      {t.status === "draft" && (
        <div className="banner draft">
          <span><strong>Draft.</strong> Only you can see this. Read it through as the public will, then publish.
            {" "}Nothing private: hostnames, addresses, internal paths, project names, secrets.</span>
          {t.can.publish && <button className="primary" onClick={() => void publish()}>Publish</button>}
        </div>
      )}
      {t.status === "hidden" && <div className="banner warn">Hidden by a moderator{t.hidden_reason ? `: ${t.hidden_reason}` : ""}. Only you and moderators can see it.</div>}
      {t.merged_into && <div className="banner">This duplicates <Link to={`/d/${t.merged_into.id}`}>{t.merged_into.title}</Link>; continue there.</div>}
      {t.locked && <div className="banner">Locked by a moderator: no more edits, replies or confirmations.</div>}
      <div className="row contrib-bar muted">
        {t.author && <span>by <strong>{t.author}</strong>{t.published_at && <> · published {ago(t.published_at)}</>}</span>}
        {t.status !== "draft" && <span className="confirm-count">{t.confirmations.length} {t.confirmations.length === 1 ? "person" : "people"} confirmed this worked</span>}
        <span className="grow" />
        {t.can.report && !t.can.moderate && <button className="flat-btn" onClick={() => void report()}>Report</button>}
        {t.can.moderate && t.status !== "draft" && (
          <span className="row mod-actions">
            <span className="muted">Moderate:</span>
            {t.status === "hidden"
              ? <button onClick={() => void act(api.moderate(doc.id, "unhide"))}>Unhide</button>
              : <button onClick={() => { const r = ask("Why hide it? The author sees this."); if (r !== null) void act(api.moderate(doc.id, "hide", r)); }}>Hide</button>}
            <button onClick={() => void act(api.moderate(doc.id, t.locked ? "unlock" : "lock"))}>{t.locked ? "Unlock" : "Lock"}</button>
            {t.merged_into
              ? <button onClick={() => void act(api.moderate(doc.id, "unduplicate"))}>Not a duplicate</button>
              : <button onClick={duplicate}>Duplicate of…</button>}
          </span>
        )}
      </div>
      {dialog}
    </>
  );
}

function ReplyForm({ doc, onDoc }: { doc: Doc; onDoc: (d: Doc) => void }) {
  const [kind, setKind] = useState<ReplyKind>("solution");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { run, dialog } = useScanGate();
  const post = async () => {
    setError(null);
    try {
      const ok = await run((ack) => api.postReply(doc.id, kind, body, ack).then((d) => { onDoc(d); setBody(""); }));
      if (!ok) return;
    } catch (e) {
      setError(errText(e));
    }
  };
  return (
    <form className="reply-form stack" onSubmit={(e) => { e.preventDefault(); void post(); }}>
      <div className="row">
        <h4 className="grow">Reply</h4>
        <select value={kind} onChange={(e) => setKind(e.target.value as ReplyKind)} aria-label="Reply kind">
          {Object.entries(REPLY_KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      <textarea rows={5} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Reply"
        placeholder={kind === "didnt-work" ? "What happened, and on what (OS, versions)?" : "Markdown. Keep private details out."} />
      {error && <p className="error">{error}</p>}
      <div className="row"><button type="submit" className="primary" disabled={!body.trim()}>Post reply</button></div>
      {dialog}
    </form>
  );
}

function ReplyItem({ doc, r, onDoc, onError }: { doc: Doc; r: ReplyT; onDoc: (d: Doc) => void; onError: (m: string) => void }) {
  const t = doc.contribution!;
  const { run, dialog } = useScanGate();
  const act = (p: Promise<Doc>) => p.then(onDoc, (e) => onError(errText(e)));
  const accepted = t.accepted_reply_id === r.id;
  const report = () => {
    const reason = ask("What's wrong with this reply?");
    if (reason) api.report(doc.id, reason, r.id).then(() => alert("Reported. Thanks."), (e) => onError(errText(e)));
  };
  let note: ReactNode = null;
  if (r.status === "draft") note = <span className="badge draft-badge">draft{r.client !== "web" ? ` from ${r.client.replace(/^token:/, "")}` : ""}, only you see it</span>;
  if (r.status === "hidden") note = <span className="badge">hidden{r.hidden_reason ? `: ${r.hidden_reason}` : ""}</span>;
  if (r.status === "withdrawn") note = <span className="badge">withdrawn by its author</span>;
  return (
    <li className={"reply" + (accepted ? " accepted" : "") + (r.status !== "published" ? " dim" : "")}>
      <div className="row reply-head">
        <span className={"reply-kind rk-" + r.kind}>{r.kind_text}</span>
        <strong>{r.by}</strong><span className="muted">{ago(r.published_at ?? r.created_at)}</span>
        {accepted && <span className="badge resolved">Resolution</span>}
        {note}
        <span className="grow" />
        {r.status === "published" && <span className="muted">{r.confirmations} confirmed</span>}
      </div>
      {r.body !== undefined && <MarkdownView markdown={r.body} />}
      <div className="row reply-actions">
        {r.status === "draft" && r.mine && <>
          <button className="primary" onClick={() => void run((ack) => api.publishReply(r.id, ack).then(onDoc)).catch((e) => onError(errText(e)))}>Publish</button>
          <button onClick={() => void act(api.withdrawReply(r.id))}>Delete draft</button>
        </>}
        {r.status === "published" && <>
          {t.can.confirm && !r.mine && (r.confirmed_by_me
            ? <button onClick={() => void act(api.unconfirm(doc.id, r.id))}>Remove my confirmation</button>
            : <button onClick={() => void act(api.confirm(doc.id, r.id))}>Worked for me</button>)}
          {t.can.accept && r.kind === "solution" && (accepted
            ? <button onClick={() => void act(api.accept(doc.id, null))}>Unmark resolution</button>
            : <button onClick={() => void act(api.accept(doc.id, r.id))}>Mark as the resolution</button>)}
          {r.mine && <button onClick={() => { if (confirm("Withdraw this reply? It stops being shown.")) void act(api.withdrawReply(r.id)); }}>Withdraw</button>}
          {t.can.report && !r.mine && !t.can.moderate && <button className="flat-btn" onClick={report}>Report</button>}
        </>}
        {t.can.moderate && (r.status === "published"
          ? <button onClick={() => { const why = ask("Why hide this reply?"); if (why !== null) void act(api.moderateReply(r.id, true, why)); }}>Hide</button>
          : r.status === "hidden" && <button onClick={() => void act(api.moderateReply(r.id, false))}>Unhide</button>)}
      </div>
      {dialog}
    </li>
  );
}

/** Confirmations and replies under a published contribution. */
export function ContributionThread({ doc, onDoc, onError }: { doc: Doc; onDoc: (d: Doc) => void; onError: (m: string) => void }) {
  const t = doc.contribution!;
  const [env, setEnv] = useState("");
  const [confirming, setConfirming] = useState(false);
  if (t.status === "draft") return null;
  const act = (p: Promise<Doc>) => p.then(onDoc, (e) => onError(errText(e)));
  return (
    <section className="thread">
      <div className="confirmations">
        <div className="row">
          <h3 className="grow">Worked for {t.confirmations.length} {t.confirmations.length === 1 ? "person" : "people"}</h3>
          {t.can.confirm && (t.confirmed_by_me
            ? <button onClick={() => void act(api.unconfirm(doc.id, null))}>Remove my confirmation</button>
            : !confirming && <button className="primary" onClick={() => setConfirming(true)}>This worked for me</button>)}
        </div>
        {confirming && (
          <form className="row" onSubmit={(e) => { e.preventDefault(); void act(api.confirm(doc.id, null, env)).then(() => { setConfirming(false); setEnv(""); }); }}>
            <input className="grow" maxLength={300} value={env} onChange={(e) => setEnv(e.target.value)}
              placeholder="Optional: where (OS, versions, hardware); no hostnames or paths" aria-label="Environment" />
            <button type="submit" className="primary">Confirm</button>
            <button type="button" onClick={() => setConfirming(false)}>Cancel</button>
          </form>
        )}
        {t.confirmations.length > 0 && (
          <ul className="confirm-list muted">
            {t.confirmations.map((c, i) => <li key={i}>{c.by}{c.environment && <> · {c.environment}</>} · {ago(c.at)}</li>)}
          </ul>
        )}
      </div>
      <h3>{t.replies.filter((r) => r.status === "published").length} replies</h3>
      <ul className="replies">{t.replies.map((r) => <ReplyItem key={r.id} doc={doc} r={r} onDoc={onDoc} onError={onError} />)}</ul>
      {t.can.reply ? <ReplyForm doc={doc} onDoc={onDoc} /> : !t.locked && !t.merged_into && <p className="muted">Sign in to reply or confirm.</p>}
    </section>
  );
}
