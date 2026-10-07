import { expect, test } from "vitest";

import { isZoomed, resolveRange } from "./timeRange.js";

const NOW = Date.parse("2026-10-07T12:00:00Z");

test("a preset is relative to now", () => {
  expect(resolveRange({ preset: "1h" }, NOW)).toEqual({
    start: "2026-10-07T11:00:00.000Z",
    end: "2026-10-07T12:00:00.000Z",
  });
});

test("all time asks for no range at all", () => {
  expect(resolveRange({ preset: "all" }, NOW)).toEqual({});
  expect(resolveRange({ preset: "nonsense" }, NOW)).toEqual({});
});

test("a zoomed range is absolute", () => {
  const range = { preset: "1h", start: "2026-10-06T21:00:00Z", end: "2026-10-06T21:05:00Z" };
  expect(resolveRange(range, NOW)).toEqual({ start: range.start, end: range.end });
  expect(isZoomed(range)).toBe(true);
  expect(isZoomed({ preset: "1h" })).toBe(false);
});
