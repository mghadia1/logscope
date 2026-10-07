// Editing a query from the UI: adding a field filter the way the query
// language reads it (see src/logscope/query.py).

const TIME_FIELDS = new Set(["time", "ts", "timestamp", "@timestamp"]);
// A field name the language can write before ":".
const FIELD = /^[^\s()":<>]+$/;
// A value that needs no quotes.
const PLAIN = /^[^\s()"\\]+$/;
// Longer values are not indexed exactly; the API refuses to match them.
const MAX_VALUE = 256;

export function canFilter(field, value) {
  return (
    FIELD.test(field) &&
    !TIME_FIELDS.has(field) &&
    (value === undefined || valueText(value).length <= MAX_VALUE)
  );
}

// How a JSON value is written in a query: null, true, 503, or the string.
export function valueText(value) {
  return value === null ? "null" : String(value);
}

export function quoteValue(text) {
  // "*" alone means "the field exists", and a leading < or > would read as a
  // range, so both are quoted.
  if (PLAIN.test(text) && text !== "*" && !/^[<>]/.test(text)) return text;
  return `"${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

export function addFilter(query, field, value, exclude = false) {
  const term = `${field}:${quoteValue(valueText(value))}`;
  const clause = exclude ? `NOT ${term}` : term;
  const current = query.trim();
  if (!current) return clause;
  // AND binds tighter than OR: "a OR b" plus a filter must become
  // "(a OR b) filter", not "a OR (b filter)".
  const grouped = /\bOR\b/.test(current) ? `(${current})` : current;
  return `${grouped} ${clause}`;
}
