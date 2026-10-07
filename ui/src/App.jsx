import { useCallback, useEffect, useMemo, useState } from "react";

import FieldSidebar from "./components/FieldSidebar.jsx";
import QueryBar from "./components/QueryBar.jsx";
import Results from "./components/Results.jsx";
import Timeline from "./components/Timeline.jsx";
import { api } from "./lib/api.js";
import { addFilter } from "./lib/query.js";
import { DEFAULT_PRESET, presetFor, resolveRange } from "./lib/timeRange.js";
import { useTail } from "./lib/useTail.js";

const PAGE = 100;
const FIELDS_SHOWN = 40;
const LIVE_KEPT = 500;

// The view lives in the URL, so a search can be bookmarked or shared:
// ?q=...&range=1h, or ?q=...&start=...&end=... after zooming in.
export function readView(search = window.location.search) {
  const params = new URLSearchParams(search);
  const start = params.get("start");
  const end = params.get("end");
  const preset = presetFor(params.get("range"))?.key ?? DEFAULT_PRESET;
  return {
    q: params.get("q") ?? "",
    range: start && end ? { preset, start, end } : { preset },
  };
}

function writeView(view) {
  const params = new URLSearchParams();
  if (view.q) params.set("q", view.q);
  params.set("range", view.range.preset);
  if (view.range.start && view.range.end) {
    params.set("start", view.range.start);
    params.set("end", view.range.end);
  }
  const url = `${window.location.pathname}?${params}`;
  if (url !== `${window.location.pathname}${window.location.search}`) {
    window.history.pushState(null, "", url);
  }
}

export default function App() {
  const [view, setView] = useState(() => readView());
  const [draft, setDraft] = useState(view.q);
  const [data, setData] = useState(null); // the last successful load
  // The view whose load last finished, well or not: loading until it is
  // the current one.
  const [settled, setSettled] = useState(null);
  const [failure, setFailure] = useState(null);
  const [queryError, setQueryError] = useState(null);
  const [older, setOlder] = useState({ events: [], cursor: null, loading: false });
  const [live, setLive] = useState(false);
  const [fresh, setFresh] = useState([]); // events from the live tail, newest first

  // The time window of the current load, resolved once, so that paging and
  // the field sidebar ask about exactly the same events.
  const scope = useMemo(() => ({ q: view.q, ...resolveRange(view.range) }), [view]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    Promise.all([
      api.search({ ...scope, limit: PAGE }, signal),
      api.histogram(scope, signal),
      api.fields({ ...scope, size: FIELDS_SHOWN }, signal),
    ])
      .then(([search, histogram, fields]) => {
        setData({ search, histogram, fields });
        setOlder({ events: [], cursor: search.next_cursor, loading: false });
        setFailure(null);
        setQueryError(null);
        setSettled(scope);
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
        if (error.status === 422 && error.position !== null) {
          setQueryError({ text: scope.q, message: error.message, position: error.position });
        } else {
          setFailure(error.message);
        }
        setSettled(scope);
      });
    return () => controller.abort();
  }, [scope]);

  // Back and forward move between views.
  useEffect(() => {
    function onPop() {
      const next = readView();
      setView(next);
      setDraft(next.q);
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const apply = useCallback((next) => {
    writeView(next);
    setView(next);
    setFresh([]);
  }, []);

  const onFilter = useCallback(
    (field, value, exclude) => {
      const q = addFilter(view.q, field, value, exclude);
      setDraft(q);
      apply({ ...view, q });
    },
    [view, apply],
  );

  const onTail = useCallback((events) => {
    setFresh((current) => [...events.reverse(), ...current].slice(0, LIVE_KEPT));
  }, []);
  const tail = useTail({ enabled: live, query: view.q, onEvents: onTail });

  async function loadOlder() {
    setOlder((current) => ({ ...current, loading: true }));
    try {
      const page = await api.search({ ...scope, limit: PAGE, cursor: older.cursor });
      setOlder((current) => ({
        events: [...current.events, ...page.events],
        cursor: page.next_cursor,
        loading: false,
      }));
    } catch (error) {
      setFailure(error.message);
      setOlder((current) => ({ ...current, loading: false }));
    }
  }

  const busy = settled !== scope;
  const loaded = data ? [...data.search.events, ...older.events] : [];
  const loadedIds = new Set(loaded.map((event) => event.id));
  const events = [...fresh.filter((event) => !loadedIds.has(event.id)), ...loaded];
  const freshIds = new Set(fresh.map((event) => event.id));

  return (
    <div className="app">
      <header className="masthead">
        <h1 className="wordmark">
          Log<span>Scope</span>
        </h1>
        <QueryBar
          draft={draft}
          onDraft={setDraft}
          onSubmit={() => apply({ ...view, q: draft.trim() })}
          range={view.range}
          onPreset={(preset) => apply({ ...view, range: { preset } })}
          onClearZoom={() => apply({ ...view, range: { preset: view.range.preset } })}
          live={live}
          onLive={(on) => {
            setLive(on);
            setFresh([]);
          }}
          liveStatus={tail.status}
          dropped={tail.dropped}
          queryError={queryError}
          understood={
            // Only the reading of the query on screen, once its results are in.
            !busy && !queryError && draft.trim() === view.q ? data?.search.query : null
          }
          lastGoodQuery={data && queryError ? data.search.query : null}
        />
      </header>

      {failure && (
        <p className="failure" role="alert">
          {failure}. Showing the last results that loaded.
        </p>
      )}

      {data ? (
        <main className="workspace">
          <Timeline
            histogram={data.histogram}
            busy={busy}
            onZoom={(start, end) => apply({ ...view, range: { preset: view.range.preset, start, end } })}
          />
          <div className="columns">
            <FieldSidebar fields={data.fields} window={scope} busy={busy} onFilter={onFilter} />
            <Results
              events={events}
              freshIds={freshIds}
              stats={data.search.stats}
              busy={busy}
              hasMore={Boolean(older.cursor)}
              loadingMore={older.loading}
              onLoadOlder={loadOlder}
              onFilter={onFilter}
            />
          </div>
        </main>
      ) : (
        !failure && <p className="loading">Loading…</p>
      )}
    </div>
  );
}
