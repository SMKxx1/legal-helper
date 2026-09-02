"use strict";

// Unit tests for the add-in's TOKENIZE-mode pure helpers: the token-list parse, the {{…}} scan
// regex, the confirmation-message formatter, the combo→form-field mapping, and the draft-version
// parse. These carry NO network / Office.js / DOM, so they run under `node --test` (Node 18+,
// built-in runner — no dependencies) exactly like the redline + async transport tests. The live
// Word.run insert/scan and the fetch calls can only be verified inside Word.

const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  scanTokens,
  normalizePlaceholder,
  parseTokenList,
  classifyScanned,
  formatReplaceMsg,
  buildDraftFields,
  parseDraftVersion,
} = require("../taskpane.js");

test("scanTokens returns distinct token names in order, tolerating whitespace inside braces", () => {
  const text =
    "Between {{counterparty_name}} and Amperesand. Governed by {{ jurisdiction }}. " +
    "Signed by {{counterparty_name}} on {{effective_date}}.";
  // distinct + first-seen order; the repeated counterparty_name appears once; inner spaces ignored
  assert.deepEqual(scanTokens(text), ["counterparty_name", "jurisdiction", "effective_date"]);
  // no tokens / empty / nullish inputs
  assert.deepEqual(scanTokens("plain text, no braces"), []);
  assert.deepEqual(scanTokens(""), []);
  assert.deepEqual(scanTokens(null), []);
  // single braces and hyphen/space token bodies are NOT matched (snake/alnum only)
  assert.deepEqual(scanTokens("{not_a_token} and {{bad-name}} and {{good_1}}"), ["good_1"]);
});

test("scanTokens is stateless across calls (regex lastIndex never leaks)", () => {
  const t = "{{a}} {{b}}";
  assert.deepEqual(scanTokens(t), ["a", "b"]);
  assert.deepEqual(scanTokens(t), ["a", "b"]); // a global regex reused wrongly would drop matches here
});

test("normalizePlaceholder coerces any form to {{name}}", () => {
  assert.equal(
    normalizePlaceholder("{{counterparty_name}}", "counterparty_name"),
    "{{counterparty_name}}",
  );
  assert.equal(
    normalizePlaceholder("counterparty_name", "counterparty_name"),
    "{{counterparty_name}}",
  );
  assert.equal(normalizePlaceholder("  counterparty_name  ", "x"), "{{counterparty_name}}");
  // missing/blank placeholder → build from the token name
  assert.equal(normalizePlaceholder("", "effective_date"), "{{effective_date}}");
  assert.equal(normalizePlaceholder(null, "effective_date"), "{{effective_date}}");
  // stray/partial braces are stripped, then re-braced
  assert.equal(normalizePlaceholder("{foo}", "x"), "{{foo}}");
  assert.equal(normalizePlaceholder("{{foo", "x"), "{{foo}}");
});

test("parseTokenList normalizes a bare array of registry tokens", () => {
  const body = [
    {
      name: "counterparty_name",
      label: "Counterparty name",
      help_text: "Full legal name of the counterparty",
      placeholder: "{{counterparty_name}}",
      party: "counterparty",
      data_type: "text",
    },
    // label falls back to name; placeholder built from name; help_text absent
    { name: "effective_date" },
  ];
  const out = parseTokenList(body);
  assert.equal(out.length, 2);
  assert.deepEqual(out[0], {
    name: "counterparty_name",
    label: "Counterparty name",
    help: "Full legal name of the counterparty",
    placeholder: "{{counterparty_name}}",
    party: "counterparty",
    data_type: "text",
  });
  assert.equal(out[1].label, "effective_date");
  assert.equal(out[1].placeholder, "{{effective_date}}");
  assert.equal(out[1].help, "");
});

test("parseTokenList tolerates {tokens:[…]} / {items:[…]} envelopes and skips junk entries", () => {
  const wrapped = { tokens: [{ name: "a" }, null, "nope", { label: "no name" }, { name: "b" }] };
  assert.deepEqual(
    parseTokenList(wrapped).map((t) => t.name),
    ["a", "b"], // null / string / name-less entries dropped
  );
  assert.deepEqual(
    parseTokenList({ items: [{ name: "c" }] }).map((t) => t.name),
    ["c"],
  );
  // unrecognized / empty bodies → empty palette (never throws)
  assert.deepEqual(parseTokenList(null), []);
  assert.deepEqual(parseTokenList({}), []);
  assert.deepEqual(parseTokenList("nope"), []);
  // accepts `help` as an alias for help_text
  assert.equal(parseTokenList([{ name: "a", help: "hi" }])[0].help, "hi");
});

test("classifyScanned flags scanned tokens not in the registry, preserving order", () => {
  const scanned = ["counterparty_name", "unknown_one", "effective_date"];
  const known = ["counterparty_name", "effective_date", "term_years"];
  assert.deepEqual(classifyScanned(scanned, known), [
    { name: "counterparty_name", known: true },
    { name: "unknown_one", known: false },
    { name: "effective_date", known: true },
  ]);
  // accepts a Set for the known names
  assert.deepEqual(classifyScanned(["a"], new Set(["a"])), [{ name: "a", known: true }]);
  // empty registry → everything unknown; empty scan → empty result
  assert.deepEqual(classifyScanned(["a"], []), [{ name: "a", known: false }]);
  assert.deepEqual(classifyScanned([], known), []);
});

test("formatReplaceMsg builds the confirmation and truncates long / multiline selections", () => {
  assert.equal(
    formatReplaceMsg("Acme Corp", "{{counterparty_name}}"),
    "Replaced ‘Acme Corp’ with {{counterparty_name}}",
  );
  // whitespace/newlines collapse to single spaces
  assert.equal(formatReplaceMsg("Acme\n  Corp", "{{x}}"), "Replaced ‘Acme Corp’ with {{x}}");
  // long selection truncated with an ellipsis (≤ 60 shown chars from the selection)
  const long = "A".repeat(200);
  const msg = formatReplaceMsg(long, "{{y}}");
  assert.ok(msg.startsWith("Replaced ‘"));
  assert.ok(msg.endsWith("…’ with {{y}}"), `expected an ellipsis before the token, got: ${msg}`);
  assert.ok(msg.length < long.length, "truncated message is shorter than the raw selection");
});

test("buildDraftFields maps the four combos to canonical multipart field names", () => {
  assert.deepEqual(
    buildDraftFields({
      jurisdiction: "US",
      counterparty: "Company",
      mutuality: "NotApplicable",
      variant: "tokenised",
    }),
    {
      jurisdiction: "US",
      counterparty_type: "Company",
      mutuality: "NotApplicable",
      variant: "tokenised",
    },
  );
  // case-insensitive + separator-tolerant inputs normalize to canonical codes
  assert.deepEqual(
    buildDraftFields({
      jurisdiction: "sg",
      counterparty: "service provider",
      mutuality: "not_applicable",
      variant: "Tokenized",
    }),
    {
      jurisdiction: "SG",
      counterparty_type: "ServiceProvider",
      mutuality: "NotApplicable",
      variant: "tokenised",
    },
  );
  assert.equal(
    buildDraftFields({
      jurisdiction: "US",
      counterparty: "Individual",
      mutuality: "Mutual",
      variant: "empty",
    }).variant,
    "empty",
  );
});

test("buildDraftFields throws a field-named error on an invalid choice", () => {
  assert.throws(
    () =>
      buildDraftFields({
        jurisdiction: "FR",
        counterparty: "Company",
        mutuality: "Mutual",
        variant: "empty",
      }),
    /jurisdiction/,
  );
  assert.throws(
    () =>
      buildDraftFields({
        jurisdiction: "US",
        counterparty: "Partnership",
        mutuality: "Mutual",
        variant: "empty",
      }),
    /counterparty type/,
  );
  assert.throws(
    () =>
      buildDraftFields({
        jurisdiction: "US",
        counterparty: "Company",
        mutuality: "Mutual",
        variant: "pdf",
      }),
    /variant/,
  );
  // a missing field is treated as an invalid choice (never silently defaulted)
  assert.throws(() => buildDraftFields({}), /jurisdiction/);
});

test("parseDraftVersion pulls the version number from any reasonable envelope", () => {
  assert.equal(parseDraftVersion({ version_no: 3 }), 3);
  assert.equal(parseDraftVersion({ version: 2 }), 2);
  assert.equal(parseDraftVersion({ version: { version_no: 5 } }), 5);
  assert.equal(parseDraftVersion({ draft: { version_no: 7 } }), 7);
  assert.equal(parseDraftVersion({ template_version: { version_no: 9 } }), 9);
  // numeric strings coerce; non-positive / missing / non-object → null
  assert.equal(parseDraftVersion({ version_no: "4" }), 4);
  assert.equal(parseDraftVersion({ version_no: 0 }), null);
  assert.equal(parseDraftVersion({ nope: 1 }), null);
  assert.equal(parseDraftVersion(null), null);
  assert.equal(parseDraftVersion("nope"), null);
});
