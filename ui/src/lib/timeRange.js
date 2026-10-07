// The time range a view covers: a preset relative to now, or an absolute
// window (after zooming into the timeline).

export const PRESETS = [
  { key: "15m", label: "Last 15 minutes", seconds: 15 * 60 },
  { key: "1h", label: "Last hour", seconds: 3600 },
  { key: "6h", label: "Last 6 hours", seconds: 6 * 3600 },
  { key: "24h", label: "Last 24 hours", seconds: 86400 },
  { key: "7d", label: "Last 7 days", seconds: 7 * 86400 },
  { key: "all", label: "All time", seconds: null },
];

export const DEFAULT_PRESET = "all";

export function presetFor(key) {
  return PRESETS.find((preset) => preset.key === key) ?? null;
}

// The start and end (ISO, UTC) to ask the API for; none for "all time".
export function resolveRange(range, now = Date.now()) {
  if (range.start && range.end) return { start: range.start, end: range.end };
  const preset = presetFor(range.preset) ?? presetFor(DEFAULT_PRESET);
  if (preset.seconds === null) return {};
  return {
    start: new Date(now - preset.seconds * 1000).toISOString(),
    end: new Date(now).toISOString(),
  };
}

export function isZoomed(range) {
  return Boolean(range.start && range.end);
}
