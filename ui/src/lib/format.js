// Numbers and times as people read them. Times show in the browser's own
// time zone; the API's are UTC.

export function formatCount(count) {
  return count.toLocaleString("en-US");
}

// Date handles milliseconds only; the API sends microseconds.
function toDate(iso) {
  return new Date(iso.replace(/(\.\d{3})\d+/, "$1"));
}

const pad = (value, size = 2) => String(value).padStart(size, "0");

// 21:04:08.123
export function formatClock(iso) {
  const date = toDate(iso);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

// Oct 6, 21:04:08
export function formatMoment(iso) {
  const date = toDate(iso);
  const day = date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return `${day}, ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// A time-axis label, as precise as the step between labels: 21:04:30 for
// steps under a minute, 21:04 under a day, Oct 6 beyond.
export function formatTick(iso, stepSeconds) {
  const date = toDate(iso);
  if (stepSeconds >= 86400) {
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return stepSeconds < 60 ? `${clock}:${pad(date.getSeconds())}` : clock;
}

// 30 s, 5 min, 1 h, 1 d
export function formatInterval(seconds) {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${seconds / 60} min`;
  if (seconds < 86400) return `${seconds / 3600} h`;
  return `${seconds / 86400} d`;
}

export function formatMs(ms) {
  return ms < 10 ? `${ms.toFixed(1)} ms` : `${Math.round(ms)} ms`;
}
