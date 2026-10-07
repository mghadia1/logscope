import { useEffect, useRef } from "react";

import { formatCount, formatMoment } from "../lib/format.js";
import { PRESETS } from "../lib/timeRange.js";

const LIVE_LABELS = {
  connecting: "Connecting…",
  live: "Live",
  ended: "Server stopped",
};

export default function QueryBar({
  draft,
  onDraft,
  onSubmit,
  range,
  onPreset,
  onClearZoom,
  live,
  onLive,
  liveStatus,
  dropped,
  queryError,
  understood,
  lastGoodQuery,
}) {
  const input = useRef(null);

  // "/" anywhere focuses the query, as in most search tools.
  useEffect(() => {
    function onKey(event) {
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
      if (event.key === "/" && !typing) {
        event.preventDefault();
        input.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const zoomed = Boolean(range.start && range.end);
  const regrouped = understood && understood !== draft.trim().replace(/\s+/g, " ");

  return (
    <form
      className="querybar"
      role="search"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="query-field">
        <label htmlFor="query" className="label">
          Query
        </label>
        <input
          id="query"
          ref={input}
          className={queryError ? "query-input has-error" : "query-input"}
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          placeholder='level:error service:gatekeeper status>=500 "upstream timeout"'
          spellCheck={false}
          autoComplete="off"
          aria-invalid={Boolean(queryError)}
          aria-describedby={queryError ? "query-error" : undefined}
        />
        {queryError ? (
          <div id="query-error" className="query-error" role="alert">
            <pre aria-hidden="true">
              {queryError.text}
              {"\n"}
              {" ".repeat(queryError.position)}^
            </pre>
            <p>
              {queryError.message} (at character {queryError.position + 1})
            </p>
            {lastGoodQuery !== null && (
              <p className="query-stale">
                {lastGoodQuery
                  ? `Below: the results of the last query that worked, ${lastGoodQuery}.`
                  : "Below: the results from before this query."}
              </p>
            )}
          </div>
        ) : (
          regrouped && (
            <p className="query-understood">
              Read as <code>{understood}</code>
            </p>
          )
        )}
      </div>

      <div className="query-controls">
        {zoomed ? (
          <span className="zoom-chip">
            {formatMoment(range.start)} – {formatMoment(range.end)}
            <button type="button" className="icon-button" onClick={onClearZoom} aria-label="Zoom out to the previous range">
              ×
            </button>
          </span>
        ) : (
          <label className="range-select">
            <span className="visually-hidden">Time range</span>
            <select value={range.preset} onChange={(event) => onPreset(event.target.value)}>
              {PRESETS.map((preset) => (
                <option key={preset.key} value={preset.key}>
                  {preset.label}
                </option>
              ))}
            </select>
          </label>
        )}
        <button type="submit" className="button">
          Search
        </button>
        <button
          type="button"
          className={live ? "button live-toggle is-on" : "button button-quiet live-toggle"}
          aria-pressed={live}
          onClick={() => onLive(!live)}
          title="Show new matching events as they arrive"
        >
          <span className="live-dot" aria-hidden="true" />
          {live ? (LIVE_LABELS[liveStatus] ?? "Live") : "Follow live"}
        </button>
      </div>
      {live && dropped > 0 && (
        <p className="live-note" role="status">
          {formatCount(dropped)} new events were skipped: they arrived faster than this page could take them.
        </p>
      )}
    </form>
  );
}
