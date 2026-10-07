import { expect, test } from "vitest";

import { niceCeiling, tickStep } from "../components/Timeline.jsx";
import { formatClock, formatCount, formatInterval } from "./format.js";

test("clock time keeps milliseconds of a microsecond timestamp", () => {
  const iso = "2026-10-06T21:04:08.123456Z";
  const local = new Date("2026-10-06T21:04:08.123Z");
  const pad = (n) => String(n).padStart(2, "0");
  expect(formatClock(iso)).toBe(
    `${pad(local.getHours())}:${pad(local.getMinutes())}:${pad(local.getSeconds())}.123`,
  );
});

test("counts and intervals read as people say them", () => {
  expect(formatCount(1284)).toBe("1,284");
  expect(formatInterval(30)).toBe("30 s");
  expect(formatInterval(300)).toBe("5 min");
  expect(formatInterval(10800)).toBe("3 h");
  expect(formatInterval(86400)).toBe("1 d");
});

test("the count axis tops out at a clean number", () => {
  expect([0, 1, 3, 7, 10, 11, 48, 150, 999].map(niceCeiling)).toEqual([
    1, 1, 5, 10, 10, 20, 50, 200, 1000,
  ]);
});

test("time-axis ticks fall on round times, a whole number of buckets apart", () => {
  expect(tickStep(10, 60, 7)).toBe(120); // 10 s bars over 10 min: every 2 min
  expect(tickStep(600, 36, 7)).toBe(3600); // 10 min bars over 6 h: hourly
  expect(tickStep(1, 30, 7)).toBe(5);
  expect(tickStep(1800, 48, 7)).toBe(21600); // 30 min bars over a day: every 6 h
  expect(tickStep(7, 100, 5)).toBe(140); // an odd interval: still whole buckets
});
