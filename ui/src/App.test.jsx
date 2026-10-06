import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import App from "./App.jsx";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(response) {
  const fetch = vi.fn(() => response);
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

test("shows the API version when /api/health answers", async () => {
  const fetch = stubFetch(
    Promise.resolve(new Response(JSON.stringify({ status: "ok", version: "1.2.3" }))),
  );
  render(<App />);
  expect(await screen.findByText("API ok, version 1.2.3")).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledWith("/api/health");
});

test("says the API is unreachable on an error status", async () => {
  stubFetch(Promise.resolve(new Response("", { status: 503 })));
  render(<App />);
  expect(await screen.findByText("API unreachable (HTTP 503)")).toBeInTheDocument();
});

test("says the API is unreachable when the request fails", async () => {
  stubFetch(Promise.reject(new TypeError("Failed to fetch")));
  render(<App />);
  expect(await screen.findByText("API unreachable (Failed to fetch)")).toBeInTheDocument();
});
