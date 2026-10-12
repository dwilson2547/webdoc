import { useEffect, useRef } from "react";
import type { DocEvent } from "./api";

// Server-sent events for one document (api/webdoc/main.py document_events).
// EventSource reconnects by itself; each reconnect starts with a "hello"
// carrying the current revision, so a tab that missed events catches up.

export function useDocEvents(id: string, onEvent: (e: DocEvent) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const es = new EventSource(`/api/documents/${id}/events`);
    es.onmessage = (m) => {
      try {
        handler.current(JSON.parse(m.data) as DocEvent);
      } catch { /* a malformed event is skipped */ }
    };
    return () => es.close();
  }, [id]);
}
