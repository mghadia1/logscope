import { useLayoutEffect, useRef, useState } from "react";

import { formatCount, formatInterval, formatMoment, formatTick } from "../lib/format.js";

// The strip chart: matching events per time bucket, drawn on recorder paper.
// The fine vertical rules are the bucket boundaries, so the paper itself
// says how the time is divided.
const PLOT_HEIGHT = 120;
const TOP = 10;
const LEFT = 48; // room for the count labels
const RIGHT = 10;
const AXIS = 24; // room for the time labels
const MAX_BAR = 24;
const GAP = 2;
const RADIUS = 4;

// Time-axis steps, in seconds: ticks only ever fall on round times.
const TICK_STEPS = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600,
  43200, 86400, 2 * 86400, 7 * 86400, 30 * 86400,
];

// The smallest round step (a whole number of buckets) giving at most
// `most` labels.
export function tickStep(intervalSeconds, buckets, most) {
  const span = intervalSeconds * buckets;
  for (const step of TICK_STEPS) {
    if (step > span) break; // a step longer than the axis labels nothing
    if (step % intervalSeconds === 0 && span / step <= most) return step;
  }
  // An interval no round step is a multiple of (7 s): every n-th bucket.
  return intervalSeconds * Math.max(1, Math.ceil(buckets / most));
}

// The smallest of 1, 2 or 5 x 10^k at or above `value`: a clean top for the
// count axis.
export function niceCeiling(value) {
  if (value <= 1) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 5, 10]) {
    if (step * power >= value) return step * power;
  }
  return 10 * power;
}

// A bar with rounded data-end corners and a square foot on the baseline.
function barPath(x, y, width, height) {
  const r = Math.min(RADIUS, width / 2, height);
  const bottom = y + height;
  return [
    `M${x},${bottom}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${bottom}`,
    "Z",
  ].join(" ");
}

function useWidth(ref) {
  const [width, setWidth] = useState(720);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(240, Math.floor(entry.contentRect.width)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export default function Timeline({ histogram, busy, onZoom }) {
  const frame = useRef(null);
  const width = useWidth(frame);
  const [hover, setHover] = useState(null); // a bucket index
  const [drag, setDrag] = useState(null); // { from, to } bucket indexes
  const [showTable, setShowTable] = useState(false);

  const buckets = histogram?.buckets ?? [];
  const interval = histogram?.interval_seconds ?? 1;
  const count = buckets.length;
  const plotWidth = width - LEFT - RIGHT;
  const slot = count ? plotWidth / count : plotWidth;
  const barWidth = Math.max(1, Math.min(MAX_BAR, slot - GAP));
  const top = niceCeiling(Math.max(0, ...buckets.map((b) => b.count)));
  const step = tickStep(interval, count, Math.max(2, Math.floor(plotWidth / 90)));
  // A bucket starting on a multiple of the step gets a label and a major rule.
  const isTick = (index) => (Date.parse(buckets[index].start) / 1000) % step === 0;
  const minorEvery = slot >= 6 ? 1 : Math.ceil(6 / slot);

  const xOf = (index) => LEFT + index * slot;
  const heightOf = (value) => (value ? Math.max(2, (value / top) * PLOT_HEIGHT) : 0);
  const bucketEnd = (index) =>
    new Date(Date.parse(buckets[index].start) + interval * 1000).toISOString();

  function indexAt(event) {
    const box = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * width;
    return Math.min(count - 1, Math.max(0, Math.floor((x - LEFT) / slot)));
  }

  function zoom(from, to) {
    const [first, last] = from <= to ? [from, to] : [to, from];
    onZoom(buckets[first].start, bucketEnd(last));
  }

  function onKeyDown(event) {
    if (!count) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      setHover((index) => Math.min(count - 1, Math.max(0, (index ?? -1) + step)));
    } else if (event.key === "Enter" && hover !== null) {
      zoom(hover, hover);
    } else if (event.key === "Escape") {
      setHover(null);
    }
  }

  const selected = drag ? [Math.min(drag.from, drag.to), Math.max(drag.from, drag.to)] : null;
  const shown = hover !== null && hover < count ? buckets[hover] : null;
  const total = histogram?.total ?? 0;

  return (
    <section className={`timeline${busy ? " is-busy" : ""}`} aria-labelledby="timeline-title">
      <header className="timeline-head">
        <h2 id="timeline-title" className="label">
          Events over time
        </h2>
        <p className="timeline-summary">
          {formatCount(total)} {total === 1 ? "event" : "events"}
          {count > 0 && <> · {formatInterval(interval)} per bar</>}
          {count > 0 && <span className="hint"> · drag across bars to zoom in</span>}
        </p>
        {count > 0 && (
          <button type="button" className="text-button" onClick={() => setShowTable((v) => !v)}>
            {showTable ? "Hide the table" : "Show counts as a table"}
          </button>
        )}
      </header>
      <div className="timeline-paper" ref={frame}>
        {count === 0 ? (
          <p className="timeline-empty">No events in this range.</p>
        ) : (
          <svg
            className="timeline-chart"
            width={width}
            height={TOP + PLOT_HEIGHT + AXIS}
            role="img"
            aria-label={`Events over time: ${formatCount(total)} in ${count} bars of ${formatInterval(interval)}. Arrow keys read each bar; Enter zooms in to it.`}
            tabIndex={0}
            onKeyDown={onKeyDown}
            onPointerLeave={() => !drag && setHover(null)}
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture?.(event.pointerId);
              const index = indexAt(event);
              setDrag({ from: index, to: index });
            }}
            onPointerMove={(event) => {
              const index = indexAt(event);
              setHover(index);
              if (drag) setDrag({ ...drag, to: index });
            }}
            onPointerUp={() => {
              if (drag) zoom(drag.from, drag.to);
              setDrag(null);
            }}
          >
            <g className="grid" aria-hidden="true">
              {buckets.map((_, index) =>
                index % minorEvery === 0 || isTick(index) ? (
                  <line
                    key={index}
                    className={isTick(index) ? "major" : "minor"}
                    x1={xOf(index)}
                    x2={xOf(index)}
                    y1={TOP}
                    y2={TOP + PLOT_HEIGHT}
                  />
                ) : null,
              )}
              {[0.25, 0.5, 0.75, 1].map((fraction) => (
                <line
                  key={fraction}
                  className={fraction === 1 ? "major" : "minor"}
                  x1={LEFT}
                  x2={width - RIGHT}
                  y1={TOP + PLOT_HEIGHT * (1 - fraction)}
                  y2={TOP + PLOT_HEIGHT * (1 - fraction)}
                />
              ))}
              <line className="baseline" x1={LEFT} x2={width - RIGHT} y1={TOP + PLOT_HEIGHT} y2={TOP + PLOT_HEIGHT} />
            </g>
            <g className="axis" aria-hidden="true">
              <text x={LEFT - 8} y={TOP + 4} textAnchor="end">
                {formatCount(top)}
              </text>
              <text x={LEFT - 8} y={TOP + PLOT_HEIGHT} textAnchor="end">
                0
              </text>
              {buckets.map((bucket, index) =>
                isTick(index) ? (
                  <text key={index} x={xOf(index) + 3} y={TOP + PLOT_HEIGHT + 16}>
                    {formatTick(bucket.start, step)}
                  </text>
                ) : null,
              )}
            </g>
            {selected && (
              <rect
                className="selection"
                x={xOf(selected[0])}
                y={TOP}
                width={(selected[1] - selected[0] + 1) * slot}
                height={PLOT_HEIGHT}
              />
            )}
            <g className="bars">
              {buckets.map((bucket, index) => {
                const height = heightOf(bucket.count);
                if (!height) return null;
                return (
                  <path
                    key={index}
                    className={index === hover ? "bar is-hovered" : "bar"}
                    d={barPath(xOf(index) + (slot - barWidth) / 2, TOP + PLOT_HEIGHT - height, barWidth, height)}
                  />
                );
              })}
            </g>
          </svg>
        )}
        {shown && (
          <div
            className="tooltip"
            role="status"
            style={{
              left: Math.min(width - 160, Math.max(0, xOf(hover) + slot / 2 - 80)),
            }}
          >
            <strong>{formatCount(shown.count)}</strong> {shown.count === 1 ? "event" : "events"}
            <span>
              {formatMoment(shown.start)} – {formatTick(bucketEnd(hover), 0)}
            </span>
          </div>
        )}
      </div>
      {showTable && count > 0 && (
        <table className="counts-table">
          <caption className="label">Events per {formatInterval(interval)} (bars with events)</caption>
          <thead>
            <tr>
              <th scope="col">From</th>
              <th scope="col">Events</th>
            </tr>
          </thead>
          <tbody>
            {buckets
              .filter((bucket) => bucket.count > 0)
              .map((bucket) => (
                <tr key={bucket.start}>
                  <td>{formatMoment(bucket.start)}</td>
                  <td>{formatCount(bucket.count)}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
