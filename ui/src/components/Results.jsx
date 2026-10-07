import { useState } from "react";

import { formatClock, formatCount, formatMoment, formatMs } from "../lib/format.js";
import { canFilter, valueText } from "../lib/query.js";

// Where common log shapes keep their parts: SurgiServe writes "event",
// Gatekeeper (Go's slog) writes "msg".
const MESSAGE_FIELDS = ["msg", "message", "event", "error"];
const LEVEL_FIELDS = ["level", "severity", "lvl"];
const SERVICE_FIELDS = ["service", "app", "logger"];
const TIME_FIELDS = ["time", "ts", "timestamp", "@timestamp"];
const MAX_EXTRAS = 6;

function pick(event, names) {
  for (const name of names) {
    const value = event[name];
    if (typeof value === "string" || typeof value === "number") return [name, value];
  }
  return [null, null];
}

// error, warn, info, debug, or null for an unknown level.
export function levelKind(level) {
  const text = String(level ?? "").toLowerCase();
  if (/^(err|error|fatal|crit|critical|panic|alert|emerg)/.test(text)) return "error";
  if (/^warn/.test(text)) return "warn";
  if (/^(info|notice)/.test(text)) return "info";
  if (/^(debug|trace)/.test(text)) return "debug";
  return null;
}

const LEVEL_ICONS = {
  // A stop-sign octagon, a warning triangle, a plain dot: the shape carries
  // the level as well as the color.
  error: <path d="M4 1h4l3 3v4l-3 3H4L1 8V4z" />,
  warn: <path d="M6 1l5.5 10h-11z" />,
  info: <circle cx="6" cy="6" r="4" />,
  debug: <circle cx="6" cy="6" r="3.5" fill="none" strokeWidth="1.5" />,
};

export function LevelBadge({ level }) {
  if (level === null || level === undefined) return <span className="level" />;
  const kind = levelKind(level);
  return (
    <span className={`level level-${kind ?? "other"}`}>
      {kind && (
        <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">
          {LEVEL_ICONS[kind]}
        </svg>
      )}
      {String(level).toUpperCase().slice(0, 5)}
    </span>
  );
}

function Leaf({ path, value, onFilter }) {
  const text = valueText(value);
  const kind = value === null ? "null" : typeof value;
  return (
    <span className="json-leaf">
      <span className={`json-value json-${kind}`}>
        {typeof value === "string" ? JSON.stringify(value) : text}
      </span>
      {canFilter(path, value) && (
        <span className="json-actions">
          <button
            type="button"
            className="icon-button"
            onClick={() => onFilter(path, value, false)}
            aria-label={`Show only events where ${path} is ${text}`}
            title="Show only these"
          >
            +
          </button>
          <button
            type="button"
            className="icon-button"
            onClick={() => onFilter(path, value, true)}
            aria-label={`Hide events where ${path} is ${text}`}
            title="Hide these"
          >
            −
          </button>
        </span>
      )}
    </span>
  );
}

// The event's JSON, every value with buttons to filter on it. Array elements
// filter on the array's path, as the index stores them.
function JsonView({ value, path, onFilter }) {
  if (Array.isArray(value)) {
    if (!value.length) return <span className="json-value">[]</span>;
    return (
      <ul className="json-list">
        {value.map((item, index) => (
          <li key={index}>
            <JsonView value={item} path={path} onFilter={onFilter} />
          </li>
        ))}
      </ul>
    );
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return <span className="json-value">{"{}"}</span>;
    return (
      <dl className="json-object">
        {entries.map(([key, child]) => (
          <div key={key} className="json-entry">
            <dt>{key}</dt>
            <dd>
              <JsonView value={child} path={path ? `${path}.${key}` : key} onFilter={onFilter} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Leaf path={path} value={value} onFilter={onFilter} />;
}

function Row({ item, fresh, onFilter }) {
  const [open, setOpen] = useState(false);
  const event = item.event;
  const [levelField, level] = pick(event, LEVEL_FIELDS);
  const [serviceField, service] = pick(event, SERVICE_FIELDS);
  const [messageField, message] = pick(event, MESSAGE_FIELDS);
  const shown = new Set([levelField, serviceField, messageField, ...TIME_FIELDS]);
  const extras = Object.entries(event)
    .filter(([key, value]) => !shown.has(key) && (value === null || typeof value !== "object"))
    .slice(0, MAX_EXTRAS);

  return (
    <li className={`row${open ? " is-open" : ""}${fresh ? " is-fresh" : ""}`}>
      <button
        type="button"
        className="row-summary"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <time className="row-time" dateTime={item.time} title={`${formatMoment(item.time)} (${item.time})`}>
          {formatClock(item.time)}
        </time>
        <LevelBadge level={level} />
        <span className="row-service">{service ?? ""}</span>
        <span className="row-message">
          {message !== null ? String(message) : <span className="row-raw">{JSON.stringify(event)}</span>}
          {extras.map(([key, value]) => (
            <span key={key} className="row-extra">
              {" "}
              <span className="row-key">{key}=</span>
              {valueText(value)}
            </span>
          ))}
        </span>
      </button>
      {open && (
        <div className="row-detail">
          <JsonView value={event} path="" onFilter={onFilter} />
          <p className="row-meta">
            id {item.id} · {item.time}
          </p>
        </div>
      )}
    </li>
  );
}

export default function Results({ events, freshIds, stats, busy, hasMore, loadingMore, onLoadOlder, onFilter }) {
  return (
    <section className={`results${busy ? " is-busy" : ""}`} aria-labelledby="results-title">
      <header className="results-head">
        <h2 id="results-title" className="label">
          Events, newest first
        </h2>
        {stats && (
          <p className="results-stats">
            {formatMs(stats.took_ms)} · read {formatCount(stats.events_read)}{" "}
            {stats.events_read === 1 ? "event" : "events"}
            {stats.segments_total === 0
              ? ", all still in memory"
              : ` · opened ${stats.segments_read} of ${stats.segments_total} ${stats.segments_total === 1 ? "segment" : "segments"}`}
          </p>
        )}
      </header>
      {events.length === 0 ? (
        <p className="empty">
          Nothing matches. Widen the time range, or remove a term from the query.
        </p>
      ) : (
        <ol className="rows">
          {events.map((item) => (
            <Row key={item.id} item={item} fresh={freshIds.has(item.id)} onFilter={onFilter} />
          ))}
        </ol>
      )}
      {hasMore && (
        <button type="button" className="button button-quiet load-older" onClick={onLoadOlder} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load older events"}
        </button>
      )}
    </section>
  );
}
