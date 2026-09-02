"use strict";

// Unit tests for the Word add-in's PURE redline/diff helpers (no Office.js / DOM). These are the
// load-bearing legal logic that places tracked changes in the document; they had zero automated
// coverage. Run with `node --test` (Node 18+, built-in test runner — no dependencies).

const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  diffTokenize,
  lcsOps,
  renderRedlineHTML,
  normMap,
  esc,
  selectContextAnchors,
  windowPin,
} = require("../taskpane.js");

test("diffTokenize splits into whitespace / non-whitespace tokens", () => {
  assert.deepEqual(diffTokenize("the cat sat"), ["the", " ", "cat", " ", "sat"]);
  assert.deepEqual(diffTokenize(""), []);
  assert.deepEqual(diffTokenize(null), []);
});

test("lcsOps produces a minimal, coalesced word-level diff", () => {
  assert.deepEqual(lcsOps(diffTokenize("the cat sat"), diffTokenize("the dog sat")), [
    { op: "=", text: "the " },
    { op: "-", text: "cat" },
    { op: "+", text: "dog" },
    { op: "=", text: " sat" },
  ]);
  // identical token streams collapse to a single equal op
  assert.deepEqual(lcsOps(diffTokenize("same text"), diffTokenize("same text")), [
    { op: "=", text: "same text" },
  ]);
});

test("renderRedlineHTML marks deletions/insertions and escapes HTML (no XSS)", () => {
  const html = renderRedlineHTML("please keep cat now", "please keep dog now");
  assert.match(html, /<del class="rl-del">cat<\/del>/);
  assert.match(html, /<ins class="rl-ins">dog<\/ins>/);
  // identical -> just the escaped text, no del/ins
  assert.equal(renderRedlineHTML("x", "x"), "x");
  // empty original -> pure insertion (e.g. restoring a deleted clause)
  assert.equal(renderRedlineHTML("", "added"), '<ins class="rl-ins">added</ins>');
  // model text with markup is escaped, never emitted raw
  const evil = renderRedlineHTML("a", "<script>alert(1)</script>");
  assert.ok(!evil.includes("<script>"), "raw <script> must not survive");
  assert.match(evil, /&lt;script&gt;/);
});

test("normMap lowercases, collapses whitespace, and maps back to source indices", () => {
  const { norm, map } = normMap("  Hello   World  ");
  assert.equal(norm, "hello world");
  assert.equal(map.length, norm.length); // one source index per normalized char
  for (let i = 1; i < map.length; i++) {
    assert.ok(map[i] >= map[i - 1], "source indices are monotonic non-decreasing");
  }
  assert.ok(map[map.length - 1] < "  Hello   World  ".length, "indices stay in range");
});

test("esc escapes & < > and double-quote", () => {
  assert.equal(esc(`<a href="x">&`), "&lt;a href=&quot;x&quot;&gt;&amp;");
  assert.equal(esc(null), "");
});

// --- diff-context anchoring fallback (place a redline when the span isn't verbatim in the doc) ---
test("selectContextAnchors keeps an interior edit with two-sided context anchors", () => {
  // A substitution in the MIDDLE of the span: stable words bracket it on both sides, so both a
  // start and an end context anchor exist -> usable to re-find the edit when the span drifted.
  const sel = selectContextAnchors(
    "the parties shall keep it in strict confidence at all times",
    "the parties shall keep it in absolute confidence at all times",
  );
  assert.equal(sel.ok, true);
  assert.equal(sel.regions.length, 1);
  const r = sel.regions[0];
  assert.ok(r.startAnchor && r.endAnchor, "both context anchors present");
  assert.match(r.newRegion, /absolute/);
  assert.match(r.oldRegion, /strict/);
});

test("selectContextAnchors refuses an edit at the span edge (one-sided context)", () => {
  // Change at the very START of the span: nothing stable precedes it, so there is no left context
  // anchor to pin against -> refuse rather than risk redlining the wrong text.
  const sel = selectContextAnchors(
    "strict confidence applies here",
    "absolute confidence applies here",
  );
  assert.equal(sel.ok, false);
  assert.match(sel.reason, /span edge/);
  // Identical text: nothing to anchor.
  assert.equal(selectContextAnchors("same words here", "same words here").ok, false);
});

// --- window pin (rescue an imprecise cross-paragraph window by scoping the search to it) ---
test("windowPin pins a span that crosses two paragraphs within the window", () => {
  const paras = [
    "The first paragraph here.",
    "The confidential clause spans",
    "into the second one entirely.",
  ];
  const pin = windowPin(paras, "clause spans into the");
  assert.equal(pin.reason, undefined);
  assert.equal(pin.startPi, 1);
  assert.equal(pin.endPi, 2);
  assert.ok(pin.startAnchor, "start anchor derived from the first paragraph");
  assert.ok(pin.endAnchor, "end anchor derived from the last paragraph");
  // Raw offsets map back to the actual paragraph text.
  assert.equal(paras[pin.startPi].slice(pin.startRaw, pin.startRaw + 6), "clause");
  assert.equal(paras[pin.endPi].slice(pin.endRaw - 3, pin.endRaw), "the");
});

test("windowPin reports missing / ambiguous spans instead of guessing", () => {
  assert.match(windowPin(["a b c", "d e f"], "not present at all").reason, /not found/);
  // Same span twice in the window -> ambiguous, never pinned.
  assert.match(
    windowPin(["repeat token here", "repeat token here"], "repeat token here").reason,
    /ambiguous/,
  );
});
