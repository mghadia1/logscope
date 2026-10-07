import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import Timeline from "./Timeline.jsx";

const HISTOGRAM = {
  interval_seconds: 60,
  total: 9,
  buckets: [
    { start: "2026-10-06T21:00:00.000000Z", count: 2 },
    { start: "2026-10-06T21:01:00.000000Z", count: 0 },
    { start: "2026-10-06T21:02:00.000000Z", count: 7 },
    { start: "2026-10-06T21:03:00.000000Z", count: 0 },
  ],
};

function chart() {
  return screen.getByRole("img", { name: /events over time/i });
}

test("one bar per bucket with events, the tallest reaching the clean top", () => {
  const { container } = render(<Timeline histogram={HISTOGRAM} onZoom={() => {}} />);
  const bars = container.querySelectorAll("path.bar");
  expect(bars).toHaveLength(2);
  // 7 is drawn against a top of 10 (the clean ceiling): 70% of 120 px.
  const heights = [...bars].map((bar) => {
    const ys = bar.getAttribute("d").match(/V([\d.]+)/g).map((v) => Number(v.slice(1)));
    return Math.round(Math.max(...ys) - Math.min(...ys) + 4);
  });
  expect(heights).toEqual([24, 84]);
  expect(screen.getByText("9 events", { exact: false })).toBeInTheDocument();
});

test("arrow keys read the buckets, Enter zooms in to one", () => {
  const onZoom = vi.fn();
  render(<Timeline histogram={HISTOGRAM} onZoom={onZoom} />);
  fireEvent.keyDown(chart(), { key: "ArrowRight" });
  fireEvent.keyDown(chart(), { key: "ArrowRight" });
  fireEvent.keyDown(chart(), { key: "ArrowRight" });
  expect(screen.getByRole("status")).toHaveTextContent("7 events");
  fireEvent.keyDown(chart(), { key: "Enter" });
  expect(onZoom).toHaveBeenCalledWith("2026-10-06T21:02:00.000000Z", "2026-10-06T21:03:00.000Z");
});

test("dragging across bars zooms in to the span", () => {
  const onZoom = vi.fn();
  render(<Timeline histogram={HISTOGRAM} onZoom={onZoom} />);
  // The chart is 720 px wide with 48 px of axis on the left and 10 on the
  // right: four buckets of 165.5 px.
  chart().getBoundingClientRect = () => ({ left: 0, width: 720, top: 0, height: 154 });
  fireEvent.pointerDown(chart(), { clientX: 60, pointerId: 1 });
  fireEvent.pointerMove(chart(), { clientX: 400, pointerId: 1 });
  fireEvent.pointerUp(chart(), { pointerId: 1 });
  expect(onZoom).toHaveBeenCalledWith("2026-10-06T21:00:00.000000Z", "2026-10-06T21:03:00.000Z");
});

test("the counts are available as a table", () => {
  render(<Timeline histogram={HISTOGRAM} onZoom={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Show counts as a table" }));
  const rows = screen.getAllByRole("row");
  expect(rows).toHaveLength(3); // the heading, and the two buckets with events
  expect(rows[2]).toHaveTextContent("7");
});

test("an empty range says so", () => {
  render(<Timeline histogram={{ interval_seconds: 60, total: 0, buckets: [] }} onZoom={() => {}} />);
  expect(screen.getByText("No events in this range.")).toBeInTheDocument();
});
