// The LogScope API. Every call takes an AbortSignal, so a newer request can
// cancel one it has made stale.

export class ApiError extends Error {
  constructor(status, detail) {
    const message =
      typeof detail === "string" ? detail : (detail?.error ?? `HTTP ${status}`);
    super(message);
    this.status = status;
    this.detail = detail;
  }

  // Where in the query the problem is, for a 422 about the query.
  get position() {
    return typeof this.detail === "object" && this.detail !== null
      ? (this.detail.position ?? null)
      : null;
  }
}

async function get(path, params, signal) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(name, value);
    }
  }
  const query = search.toString();
  let response;
  try {
    response = await fetch(query ? `${path}?${query}` : path, { signal });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "LogScope's API is not answering");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, body?.detail ?? `HTTP ${response.status}`);
  }
  return body;
}

export const api = {
  search: (params, signal) => get("/api/search", params, signal),
  histogram: (params, signal) => get("/api/aggregate/histogram", params, signal),
  fields: (params, signal) => get("/api/aggregate/fields", params, signal),
  values: (params, signal) => get("/api/aggregate/terms", params, signal),
  numbers: (params, signal) => get("/api/aggregate/numeric", params, signal),
};
