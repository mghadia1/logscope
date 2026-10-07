import { useEffect, useState } from "react";

import { api } from "../lib/api.js";
import { formatCount } from "../lib/format.js";
import { canFilter } from "../lib/query.js";

const VALUES_SHOWN = 8;

function FieldValues({ field, window, onFilter }) {
  const [state, setState] = useState({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api.values({ ...window, field, size: VALUES_SHOWN }, controller.signal),
      api.numbers({ ...window, field }, controller.signal),
    ])
      .then(([values, numbers]) => setState({ status: "ready", values, numbers }))
      .catch((error) => {
        if (error.name !== "AbortError") setState({ status: "error", message: error.message });
      });
    return () => controller.abort();
  }, [field, window]);

  if (state.status === "loading") return <p className="field-note">Counting…</p>;
  if (state.status === "error") return <p className="field-note is-error">{state.message}</p>;

  const { values, numbers } = state;
  const most = Math.max(1, ...values.values.map((v) => v.count));
  const filterable = canFilter(field);
  return (
    <div className="field-values">
      {values.values.length === 0 ? (
        <p className="field-note">No exact values (only long text).</p>
      ) : (
        <ul>
          {values.values.map(({ value, count }) => (
            <li key={value} className="value">
              <button
                type="button"
                className="value-pick"
                disabled={!filterable}
                onClick={() => onFilter(field, value, false)}
                title={filterable ? `Show only ${field}:${value}` : undefined}
              >
                <span className="value-text">{value}</span>
                <span className="value-count">{formatCount(count)}</span>
                <span className="value-bar" aria-hidden="true">
                  <span style={{ width: `${(count / most) * 100}%` }} />
                </span>
              </button>
              {filterable && (
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => onFilter(field, value, true)}
                  aria-label={`Hide events where ${field} is ${value}`}
                  title="Hide these"
                >
                  −
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {values.distinct > values.values.length && (
        <p className="field-note">
          {formatCount(values.distinct - values.values.length)} more distinct values
        </p>
      )}
      {numbers.count > 0 && (
        <dl className="field-numbers">
          <div>
            <dt>min</dt>
            <dd>{numbers.min}</dd>
          </div>
          <div>
            <dt>median</dt>
            <dd>{numbers.percentiles.p50}</dd>
          </div>
          <div>
            <dt>p99</dt>
            <dd>{numbers.percentiles.p99}</dd>
          </div>
          <div>
            <dt>max</dt>
            <dd>{numbers.max}</dd>
          </div>
        </dl>
      )}
    </div>
  );
}

export default function FieldSidebar({ fields, window, busy, onFilter }) {
  const [open, setOpen] = useState(null);

  return (
    <aside className={`fields${busy ? " is-busy" : ""}`} aria-labelledby="fields-title">
      <h2 id="fields-title" className="label">
        Fields
      </h2>
      {!fields || fields.fields.length === 0 ? (
        <p className="field-note">No fields: no events match.</p>
      ) : (
        <ul className="field-list">
          {fields.fields.map(({ field, count }) => (
            <li key={field} className={open === field ? "field is-open" : "field"}>
              <button
                type="button"
                className="field-name"
                aria-expanded={open === field}
                onClick={() => setOpen(open === field ? null : field)}
              >
                <span>{field}</span>
                <span className="field-count" title={`${formatCount(count)} of ${formatCount(fields.matched)} events have it`}>
                  {formatCount(count)}
                </span>
              </button>
              {open === field && <FieldValues field={field} window={window} onFilter={onFilter} />}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
