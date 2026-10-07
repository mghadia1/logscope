import { useEffect, useRef, useState } from "react";

// How often buffered events are handed over: one render per batch, not one
// per event, when they arrive quickly.
const FLUSH_MS = 150;

// Follow /api/tail while `enabled`, calling `onEvents` with each batch of new
// events (oldest first). Reconnects when the query changes; the browser's
// EventSource retries on its own if the connection drops.
//
// status: "off", "connecting", "live", "ended" (the server shut down).
export function useTail({ enabled, query, onEvents }) {
  const [state, setState] = useState({ enabled, query, status: "connecting", dropped: 0 });
  // A new connection starts from scratch: reset while rendering, the way
  // React suggests for state that depends on props.
  if (state.enabled !== enabled || state.query !== query) {
    setState({ enabled, query, status: "connecting", dropped: 0 });
  }
  const deliver = useRef(onEvents);
  useEffect(() => {
    deliver.current = onEvents;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    // Updates from this connection only, never a previous one.
    const update = (change) =>
      setState((current) =>
        current.enabled && current.query === query ? { ...current, ...change(current) } : current,
      );
    const buffer = [];
    const source = new EventSource(`/api/tail?${new URLSearchParams({ q: query })}`);
    const timer = setInterval(() => {
      if (buffer.length) deliver.current(buffer.splice(0));
    }, FLUSH_MS);

    source.onopen = () => update(() => ({ status: "live" }));
    source.onerror = () => update(() => ({ status: "connecting" }));
    source.addEventListener("event", (message) => buffer.push(JSON.parse(message.data)));
    source.addEventListener("dropped", (message) =>
      update((current) => ({ dropped: current.dropped + JSON.parse(message.data).events })),
    );
    source.addEventListener("end", () => {
      update(() => ({ status: "ended" }));
      source.close();
    });
    return () => {
      clearInterval(timer);
      source.close();
    };
  }, [enabled, query]);

  return enabled ? { status: state.status, dropped: state.dropped } : { status: "off", dropped: 0 };
}
