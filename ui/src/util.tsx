import { useEffect, type ReactNode } from "react";

export function ago(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}

export const parseTags = (text: string) =>
  text.split(",").map((t) => t.trim()).filter(Boolean);

export function Modal({ title, onClose, children, wide }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={"modal" + (wide ? " wide" : "")} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <span className="grow" />
          <button onClick={onClose} aria-label="Close">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Diff({ text }: { text: string }) {
  if (!text) return <p className="muted">No changes.</p>;
  return (
    <pre className="diff">
      {text.split("\n").map((ln, i) => {
        const cls = ln.startsWith("+++") || ln.startsWith("---") ? "d-file"
          : ln.startsWith("@@") ? "d-hunk" : ln.startsWith("+") ? "d-add" : ln.startsWith("-") ? "d-del" : "";
        return <div key={i} className={cls}>{ln || " "}</div>;
      })}
    </pre>
  );
}

export const ROLE_TEXT: Record<string, string> = {
  admin: "Admin", owner: "Owner", moderator: "Moderator", editor: "Editor", reader: "Reader",
};
