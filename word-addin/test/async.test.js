"use strict";

// Unit tests for the add-in's ASYNC deep-review transport helpers (PLAN §3.1): deep mode submits
// async (202 + job id) and polls GET /v1/reviews/jobs/{id} to completion, because a deep review can
// outlast the platform's ingress request timeout. These cover the PURE pieces — response-shape
// validation, poll backoff, and job-status interpretation — with NO network / Office.js / DOM, so
// they run under `node --test` (Node 18+, built-in runner — no dependencies) like the redline tests.

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { isReviewBody, parseJsonSafe, nextPollDelayMs, jobOutcome } = require("../taskpane.js");

test("isReviewBody accepts a well-formed review and rejects malformed ones", () => {
  const ok = { review_id: "abc", findings: [], coverage: { absent_required: [] } };
  assert.equal(isReviewBody(ok), true);
  // missing review_id
  assert.equal(isReviewBody({ findings: [], coverage: {} }), false);
  // findings not an array
  assert.equal(isReviewBody({ review_id: "x", findings: "no", coverage: {} }), false);
  // missing / non-object coverage
  assert.equal(isReviewBody({ review_id: "x", findings: [] }), false);
  assert.equal(isReviewBody({ review_id: "x", findings: [], coverage: null }), false);
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

test("jobOutcome walks pending -> running -> done|failed and inlines the review when done", () => {
  const review = { review_id: "r1", findings: [], coverage: {} };
  assert.deepEqual(jobOutcome({ status: "done", review }), { state: "done", review });
  // done but the review hasn't been inlined yet -> review null (caller then rejects the shape)
  assert.deepEqual(jobOutcome({ status: "done" }), { state: "done", review: null });
  // failed carries the sanitized error string
  assert.deepEqual(jobOutcome({ status: "failed", error: "RuntimeError" }), {
    state: "failed",
    error: "RuntimeError",
  });
  // pending / running / unknown all read as "keep polling"; case-insensitive
  assert.deepEqual(jobOutcome({ status: "pending" }), { state: "pending" });
  assert.deepEqual(jobOutcome({ status: "RUNNING" }), { state: "pending" });
  assert.deepEqual(jobOutcome({ status: "" }), { state: "pending" });
  assert.deepEqual(jobOutcome(null), { state: "pending" });
});
