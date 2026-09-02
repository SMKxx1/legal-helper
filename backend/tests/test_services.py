"""Pure-logic unit tests for ``app.pricing`` (no HTTP).

Covers rate lookup, cost computation, the ``_coerce_table`` sanitization, and the AppSetting
override merge (the only piece that needs the ``db`` fixture) by calling the functions directly
with crafted inputs and asserting exact outputs.
"""

from __future__ import annotations

from app import pricing


# --------------------------------------------------------------------------- #
# app.pricing — rate_for / cost_for (default table, no DB)
# --------------------------------------------------------------------------- #
def test_rate_for_exact_and_dated_suffix():
    table = pricing.DEFAULT_PRICING
    assert pricing.rate_for("claude-opus-4-8", table) == {"input": 5.0, "output": 25.0}
    # Dated suffix matches by longest known prefix.
    assert pricing.rate_for("claude-opus-4-8-20251101", table) == {
        "input": 5.0,
        "output": 25.0,
    }
    # Sonnet family rate.
    assert pricing.rate_for("claude-sonnet-4-6", table) == {
        "input": 3.0,
        "output": 15.0,
    }


def test_rate_for_family_keyword_fallback():
    table = pricing.DEFAULT_PRICING
    # Bare family word resolves to the flagship canon.
    assert pricing.rate_for("opus", table) == table["claude-opus-4-8"]
    # An unlisted claude-opus id falls back via the family keyword.
    assert pricing.rate_for("claude-opus-9-9", table) == table["claude-opus-4-8"]


def test_rate_for_unknown_and_empty():
    table = pricing.DEFAULT_PRICING
    assert pricing.rate_for("gpt-4", table) is None
    assert pricing.rate_for("", table) is None
    assert pricing.rate_for("llama-3", table) is None


def test_cost_for_basic_and_rounding():
    table = pricing.DEFAULT_PRICING
    # 1M in + 1M out at $5/$25 = $30.
    assert pricing.cost_for("claude-opus-4-8", 1_000_000, 1_000_000, table) == 30.0
    # Sub-cent usage rounds to 6 places: 1000 in * $5/MTok = 0.005.
    assert pricing.cost_for("claude-opus-4-8", 1000, 0, table) == 0.005


def test_cost_for_cache_multipliers():
    table = pricing.DEFAULT_PRICING
    # cache read 0.10x + cache write 1.25x of the $5 input rate, per 1M tokens.
    cost = pricing.cost_for(
        "claude-opus-4-8",
        0,
        0,
        table,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == round(5.0 * 0.10 + 5.0 * 1.25, 6) == 6.75


def test_cost_for_non_priceable_returns_none():
    table = pricing.DEFAULT_PRICING
    # Non-Anthropic ids are unpriced (billed as $0 -> None), never mispriced.
    assert pricing.cost_for("gpt-4", 1000, 1000, table) is None
    assert pricing.cost_for("", 1000, 1000, table) is None


def test_cost_for_priceable_but_unrated_returns_none():
    # A claude-looking id absent from a (deliberately empty) table -> None.
    assert pricing.cost_for("claude-unknown-model", 1000, 1000, table={}) is None


# --------------------------------------------------------------------------- #
# app.pricing — _coerce_table sanitization (pure)
# --------------------------------------------------------------------------- #
def test_coerce_table_rejects_bad_and_normalizes_good():
    raw = {
        "  ClAuDe-Foo  ": {
            "input": 2,
            "output": 3,
        },  # normalized: stripped+lowercased, coerced float
        "neg": {"input": -1, "output": 5},  # negative rejected
        "nan": {"input": float("nan"), "output": 5},  # non-finite rejected
        "missing": {"input": 1},  # missing output rejected
        "bad-shape": "not-a-dict",  # non-dict rate rejected
    }
    out = pricing._coerce_table(raw)
    assert out == {"claude-foo": {"input": 2.0, "output": 3.0}}


# --------------------------------------------------------------------------- #
# app.pricing — load_pricing override merge (needs the db fixture)
# --------------------------------------------------------------------------- #
def test_load_pricing_defaults_when_no_override(db):
    # With an empty app_settings table, the effective table is the defaults.
    table = pricing.load_pricing(db)
    assert table["claude-opus-4-8"] == {"input": 5.0, "output": 25.0}
    # It is a copy, not the module-level dict.
    assert table is not pricing.DEFAULT_PRICING


def test_save_and_load_pricing_override_merges(db):
    # Saving an override row changes the matched key but leaves the rest at defaults.
    merged = pricing.save_pricing(
        {"claude-opus-4-8": {"input": 9.0, "output": 99.0}}, db=db
    )
    assert merged["claude-opus-4-8"] == {"input": 9.0, "output": 99.0}
    assert merged["claude-sonnet-4-6"] == {"input": 3.0, "output": 15.0}

    # load_pricing reads back the same override.
    reloaded = pricing.load_pricing(db)
    assert reloaded["claude-opus-4-8"] == {"input": 9.0, "output": 99.0}

    # cost_for honours the stored override when given the merged table.
    assert pricing.cost_for("claude-opus-4-8", 1_000_000, 0, reloaded) == 9.0


def test_clear_pricing_reverts_to_defaults(db):
    pricing.save_pricing({"claude-opus-4-8": {"input": 1.0, "output": 1.0}}, db=db)
    assert pricing.load_pricing(db)["claude-opus-4-8"] == {"input": 1.0, "output": 1.0}
    pricing.clear_pricing(db=db)
    assert pricing.load_pricing(db)["claude-opus-4-8"] == {"input": 5.0, "output": 25.0}
