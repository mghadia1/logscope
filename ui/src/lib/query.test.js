import { describe, expect, test } from "vitest";

import { addFilter, canFilter, quoteValue } from "./query.js";

describe("addFilter", () => {
  test.each([
    ["", "level", "error", false, "level:error"],
    ["timeout", "level", "error", false, "timeout level:error"],
    ["timeout", "level", "info", true, "timeout NOT level:info"],
    // AND binds tighter than OR: the existing query is grouped first.
    ["a OR b", "level", "error", false, "(a OR b) level:error"],
    ["", "route", "GET /users/{id}", false, 'route:"GET /users/{id}"'],
    ["", "status", 503, false, "status:503"],
    ["", "user", null, false, "user:null"],
    ["", "ok", true, true, "NOT ok:true"],
    ["", "note", 'say "hi"', false, 'note:"say \\"hi\\""'],
  ])("%j + %s:%j (exclude %s) → %s", (query, field, value, exclude, expected) => {
    expect(addFilter(query, field, value, exclude)).toBe(expected);
  });
});

describe("quoteValue", () => {
  test("quotes what the language would read differently", () => {
    expect(quoteValue("*")).toBe('"*"'); // bare, it means "the field exists"
    expect(quoteValue(">=500")).toBe('">=500"'); // bare, a range
    expect(quoteValue("a(b")).toBe('"a(b"');
    expect(quoteValue("")).toBe('""');
    expect(quoteValue("/healthz")).toBe("/healthz");
    expect(quoteValue("back\\slash")).toBe('"back\\\\slash"');
  });
});

describe("canFilter", () => {
  test("only fields the language can name, values it indexes exactly", () => {
    expect(canFilter("http.method", "GET")).toBe(true);
    expect(canFilter("time", "x")).toBe(false); // searched by the time range
    expect(canFilter("a b", "x")).toBe(false);
    expect(canFilter("weird:key", "x")).toBe(false);
    expect(canFilter("msg", "x".repeat(257))).toBe(false);
    expect(canFilter("msg", "x".repeat(256))).toBe(true);
  });
});
