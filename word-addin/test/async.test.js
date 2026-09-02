"use strict";

// Unit tests for the add-in's ASYNC deep-review transport helpers (plan §4.2): deep mode submits
// async (POST /api/reviews -> 202 + id) and polls GET /api/reviews/{id} to completion, because a
// deep review can outlast a typical ingress request timeout. These cover the PURE pieces —
// response-shape validation, poll backoff, and status interpretation — with NO network / Office.js
// / DOM, so they run under `node --test` (Node 18+, built-in runner — no dependencies) like the
// redline tests.

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { isReviewBody, parseJsonSafe, nextPollDelayMs, jobOutcome } = require("../taskpane.js");

test("isReviewBody accepts a finished review and rejects malformed ones", () => {
  const ok = { id: "abc", status: "done", findings: [], coverage: null };
  assert.equal(isReviewBody(ok), true);
  // missing id
  assert.equal(isReviewBody({ status: "done", findings: [] }), false);
  // not yet done (a bare status poll, still queued/running)
  assert.equal(isReviewBody({ id: "x", status: "running" }), false);
  // findings not an array
  assert.equal(isReviewBody({ id: "x", status: "done", findings: "no" }), false);
  // missing findings entirely
  assert.equal(isReviewBody({ id: "x", status: "done" }), false);
  // null / non-object
  assert.equal(isReviewBody(null), false);
  assert.equal(isReviewBody("nope"), false);
});

test("parseJsonSafe returns parsed JSON, or null for empty / invalid bodies", () => {
  assert.deepEqual(parseJsonSafe('{"a":1}'), { a: 1 });
  assert.equal(parseJsonSafe(""), null);
  assert.equal(parseJsonSafe(null), null);
  assert.equal(parseJsonSafe("<html>not json</html>"), null);
});

test("nextPollDelayMs backs off monotonically and caps at 5s", () => {
  const d0 = nextPollDelayMs(0);
  assert.ok(d0 >= 1000 && d0 <= 2000, `first delay ~1.5s, got ${d0}`);
  // non-decreasing as attempts grow
  for (let a = 1; a < 12; a++) {
    assert.ok(nextPollDelayMs(a) >= nextPollDelayMs(a - 1), `attempt ${a} not smaller`);
  }
  // capped
  assert.equal(nextPollDelayMs(50), 5000);
  // a negative/garbage attempt clamps to the first-delay floor (never below base)
  assert.equal(nextPollDelayMs(-3), nextPollDelayMs(0));
});

test("jobOutcome walks queued -> running -> done|failed; a 'done' poll IS the review", () => {
  const done = { id: "r1", status: "done", findings: [], coverage: null };
  assert.deepEqual(jobOutcome(done), { state: "done", review: done });
  // done but malformed (e.g. findings missing) -> review null, caller then rejects the shape
  assert.deepEqual(jobOutcome({ id: "r1", status: "done" }), { state: "done", review: null });
  // failed carries the sanitized error code
  assert.deepEqual(jobOutcome({ id: "r1", status: "failed", error: "no_zdr_route" }), {
    state: "failed",
    error: "no_zdr_route",
  });
  // queued / running / unknown all read as "keep polling"; case-insensitive
  assert.deepEqual(jobOutcome({ id: "r1", status: "queued" }), { state: "pending" });
  assert.deepEqual(jobOutcome({ id: "r1", status: "RUNNING" }), { state: "pending" });
  assert.deepEqual(jobOutcome({ id: "r1", status: "" }), { state: "pending" });
  assert.deepEqual(jobOutcome(null), { state: "pending" });
});
