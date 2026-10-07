import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import App, { readView } from "./App.jsx";

const EVENTS = [
  {
    id: 3,
    time: "2026-10-06T21:00:02.000000Z",
    event: { level: "error", service: "surgiserve", event: "job failed", job_id: "3f2a-77", attempt: 2 },
  },
  {
    id: 2,
    time: "2026-10-06T21:00:01.000000Z",
    event: { level: "ERROR", service: "gatekeeper", msg: "upstream timeout", status: 504 },
  },
];

const STATS = {
  segments_total: 2,
  segments_read: 1,
  segments_skipped: 1,
  events_read: 2,
  candidates: 2,
  checked: 0,
  took_ms: 1.2,
};

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// A fake API: answers by path, and records every request.
function fakeApi(overrides = {}) {
  const requests = [];
  const routes = {
    "/api/search": (params) =>
      json(200, { query: params.get("q") ?? "", events: EVENTS, next_cursor: null, stats: STATS }),
    "/api/aggregate/histogram": () =>
      json(200, {
        query: "",
        interval_seconds: 1,
        total: 2,
        buckets: [
          { start: "2026-10-06T21:00:01.000000Z", count: 1 },
          { start: "2026-10-06T21:00:02.000000Z", count: 1 },
        ],
        stats: STATS,
      }),
    "/api/aggregate/fields": () =>
      json(200, {
        query: "",
        matched: 2,
        fields: [
          { field: "level", count: 2 },
          { field: "service", count: 2 },
        ],
        stats: STATS,
      }),
    "/api/aggregate/terms": () =>
      json(200, {
        query: "",
        field: "service",
        matched: 2,
        with_field: 2,
        distinct: 2,
        values: [
          { value: "gatekeeper", count: 1 },
          { value: "surgiserve", count: 1 },
        ],
        stats: STATS,
      }),
    "/api/aggregate/numeric": () =>
      json(200, { query: "", field: "service", matched: 2, count: 0, min: null, max: null, mean: null, percentiles: {}, stats: STATS }),
    ...overrides,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) => {
      const [path, search = ""] = url.split("?");
      const params = new URLSearchParams(search);
      requests.push({ path, params });
      return routes[path] ? routes[path](params) : json(404, { detail: "Not Found" });
    }),
  );
  return requests;
}

class FakeEventSource {
  static opened = [];

  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.opened.push(this);
  }

  addEventListener(kind, listener) {
    (this.listeners[kind] ??= []).push(listener);
  }

  emit(kind, data) {
    for (const listener of this.listeners[kind] ?? []) listener({ data: JSON.stringify(data) });
  }

  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  FakeEventSource.opened = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function lastRequest(requests, path) {
  return requests.filter((r) => r.path === path).at(-1);
}

test("reads the view from the URL and loads results, timeline and fields", async () => {
  window.history.replaceState(null, "", "/?q=level:error&range=1h");
  const requests = fakeApi();
  render(<App />);
  expect(await screen.findByText("upstream timeout")).toBeInTheDocument();
  expect(screen.getByText("job failed")).toBeInTheDocument();
  expect(screen.getByLabelText("Query")).toHaveValue("level:error");
  const search = lastRequest(requests, "/api/search").params;
  expect(search.get("q")).toBe("level:error");
  expect(search.get("start")).toBeTruthy(); // the last hour, resolved
  for (const path of ["/api/aggregate/histogram", "/api/aggregate/fields"]) {
    expect(lastRequest(requests, path).params.get("start")).toBe(search.get("start"));
  }
  expect(screen.getByRole("button", { name: /level/ })).toBeInTheDocument();
});

test("a search runs on Enter and lands in the URL", async () => {
  const requests = fakeApi();
  render(<App />);
  await screen.findByText("upstream timeout");
  fireEvent.change(screen.getByLabelText("Query"), { target: { value: "status>=500" } });
  fireEvent.submit(screen.getByRole("search"));
  await waitFor(() => expect(lastRequest(requests, "/api/search").params.get("q")).toBe("status>=500"));
  expect(readView().q).toBe("status>=500");
});

test("a bad query points at the problem", async () => {
  fakeApi({
    "/api/search": (params) =>
      params.get("q")
        ? json(422, { detail: { error: "this ( is never closed", position: 12 } })
        : json(200, { query: "", events: EVENTS, next_cursor: null, stats: STATS }),
  });
  render(<App />);
  await screen.findByText("upstream timeout");
  fireEvent.change(screen.getByLabelText("Query"), { target: { value: "level:error (status>=500" } });
  fireEvent.submit(screen.getByRole("search"));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("this ( is never closed (at character 13)");
  // The caret sits under the "(", twelve characters in.
  expect(alert.querySelector("pre").textContent).toBe("level:error (status>=500\n" + " ".repeat(12) + "^");
  expect(screen.getByLabelText("Query")).toHaveAttribute("aria-invalid", "true");
  // The previous results stay on screen, and say so.
  expect(screen.getByText("upstream timeout")).toBeInTheDocument();
  expect(alert).toHaveTextContent("Below: the results from before this query.");
});

test("how a query was read is shown only for the query on screen", async () => {
  let release;
  const slow = new Promise((resolve) => (release = resolve));
  fakeApi({
    "/api/search": async (params) => {
      if (params.get("q") === "a OR b c") {
        return json(200, { query: "a OR (b c)", events: EVENTS, next_cursor: null, stats: STATS });
      }
      await slow; // the next search hangs until released
      return json(200, { query: "level:error", events: EVENTS, next_cursor: null, stats: STATS });
    },
  });
  window.history.replaceState(null, "", "/?q=a+OR+b+c");
  render(<App />);
  expect(await screen.findByText("a OR (b c)")).toBeInTheDocument(); // "Read as"
  fireEvent.change(screen.getByLabelText("Query"), { target: { value: "level:error" } });
  // Edited but not yet run: the old reading would describe the wrong query.
  expect(screen.queryByText("a OR (b c)")).not.toBeInTheDocument();
  fireEvent.submit(screen.getByRole("search"));
  expect(screen.queryByText(/Read as/)).not.toBeInTheDocument(); // still loading
  release();
  await waitFor(() => expect(screen.getByText("upstream timeout").closest("section")).not.toHaveClass("is-busy"));
  expect(screen.queryByText(/Read as/)).not.toBeInTheDocument(); // read as written
});

test("a field's values filter the search", async () => {
  const requests = fakeApi();
  render(<App />);
  await screen.findByText("upstream timeout");
  const sidebar = screen.getByRole("complementary");
  fireEvent.click(within(sidebar).getByRole("button", { name: /service/ }));
  const values = await within(sidebar).findByRole("button", { name: /^gatekeeper/ });
  expect(lastRequest(requests, "/api/aggregate/terms").params.get("field")).toBe("service");
  fireEvent.click(values);
  await waitFor(() =>
    expect(lastRequest(requests, "/api/search").params.get("q")).toBe("service:gatekeeper"),
  );
  expect(screen.getByLabelText("Query")).toHaveValue("service:gatekeeper");
});

test("an event opens into its JSON, and its values filter too", async () => {
  const requests = fakeApi();
  render(<App />);
  fireEvent.click(await screen.findByText("upstream timeout"));
  const hide = screen.getByRole("button", { name: "Hide events where status is 504" });
  const detail = hide.closest(".row-detail");
  expect(within(detail).getByText('"gatekeeper"')).toBeInTheDocument();
  fireEvent.click(hide);
  await waitFor(() =>
    expect(lastRequest(requests, "/api/search").params.get("q")).toBe("NOT status:504"),
  );
});

test("following live adds new matching events at the top", async () => {
  fakeApi();
  render(<App />);
  await screen.findByText("upstream timeout");
  fireEvent.change(screen.getByLabelText("Query"), { target: { value: "level:error" } });
  fireEvent.submit(screen.getByRole("search"));
  await waitFor(() => expect(readView().q).toBe("level:error"));
  fireEvent.click(screen.getByRole("button", { name: /follow live/i }));
  const source = FakeEventSource.opened.at(-1);
  expect(source.url).toBe("/api/tail?q=level%3Aerror");
  act(() => source.onopen());
  expect(screen.getByRole("button", { name: "Live" })).toHaveAttribute("aria-pressed", "true");
  act(() =>
    source.emit("event", {
      id: 9,
      time: "2026-10-06T21:00:09.000000Z",
      event: { level: "error", msg: "just arrived" },
    }),
  );
  const row = (await screen.findByText("just arrived")).closest("li");
  expect(row).toHaveClass("is-fresh");
  expect(screen.getAllByRole("listitem").filter((li) => li.classList.contains("row"))[0]).toBe(row);
  fireEvent.click(screen.getByRole("button", { name: "Live" }));
  expect(source.closed).toBe(true);
});

test("an API that does not answer is reported", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }),
  );
  render(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent("LogScope's API is not answering");
});
