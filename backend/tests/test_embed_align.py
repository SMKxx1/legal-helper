"""Tests for the embedding-alignment substrate (provider "fake" + a synthetic in-test index).

These exercise the primitives WITHOUT the real npz or any network: a tiny hand-built
:class:`PlaybookIndex` embedded with the deterministic "fake" provider. They assert the
escalate-only contract — identity detection, best-match association, trigger thresholding — and
the degrade-to-empty behavior when the provider is off or raises.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.engine import embeddings
from app.engine.embed_align import NO_REPORT, embed_and_match
from app.engine.embeddings import PlaybookIndex
from app.engine.simcache import norm_sha256

VARIANT = "US_Company"

# Three well-separated baseline clauses + a matching set of trigger texts.
_BASELINE = [
    "Confidential Information means any and all information relating to the Purpose.",
    "The Receiving Party shall hold the information in strict confidence for two years.",
    "This Agreement is governed by the laws of the State of Delaware.",
]
# Position texts are the baseline clauses verbatim so the (semantics-free) fake provider gives
# cosine 1.0 and clause_type attribution is deterministic in-test.
_POSITIONS = [
    ("definition", _BASELINE[0]),
    ("term", _BASELINE[1]),
    ("governing_law", _BASELINE[2]),
]
_TRIGGERS = [
    "Definition narrowed to require written marking before disclosure to qualify.",
    "Perpetual survival with no fixed end date for the confidentiality obligation.",
]


def _fake_provider() -> embeddings.EmbeddingProvider:
    return embeddings._FakeProvider(model="fake-test")


def _synthetic_index() -> PlaybookIndex:
    """Build a PlaybookIndex whose vectors come from the fake provider (deterministic)."""
    prov = _fake_provider()

    def block(texts, extra_key=None, extra_vals=None):
        vecs = prov.embed(list(texts))
        meta = []
        for i, t in enumerate(texts):
            rec = {"text": t, "sha": norm_sha256(t)}
            if extra_key is not None:
                rec[extra_key] = extra_vals[i]
            meta.append(rec)
        return vecs, meta

    base_v, base_m = block(_BASELINE)
    pos_v, pos_m = block(
        [p[1] for p in _POSITIONS], "clause_type", [p[0] for p in _POSITIONS]
    )
    trig_v, trig_m = block(_TRIGGERS)

    return PlaybookIndex(
        release="test-release",
        vectors={
            (VARIANT, "baseline"): base_v,
            (VARIANT, "positions"): pos_v,
            (VARIANT, "triggers"): trig_v,
        },
        meta={
            (VARIANT, "baseline"): base_m,
            (VARIANT, "positions"): pos_m,
            (VARIANT, "triggers"): trig_m,
        },
    )


def test_identity_detection_on_verbatim_clause():
    """An incoming doc that IS a baseline clause verbatim is flagged as verbatim + self-matched."""
    index = _synthetic_index()
    report = embed_and_match(_BASELINE[0], VARIANT, _fake_provider(), index)

    assert report is not NO_REPORT
    assert report.verbatim == [0]
    # cosine(identical) == 1.0 -> best baseline match is itself at score ~1.
    assert report.matched and report.matched[0].baseline_idx == 0
    assert report.matched[0].score == pytest.approx(1.0, abs=1e-5)
    assert report.matched[0].clause_type == "definition"


def test_best_match_association():
    """A clause that is not verbatim still associates with the nearest baseline clause."""
    index = _synthetic_index()
    # Feed the second baseline clause -> should best-match baseline_idx 1, not verbatim of clause 0.
    report = embed_and_match(_BASELINE[1], VARIANT, _fake_provider(), index)

    # verbatim lists the INCOMING clause index (0 — single-clause doc); it best-matches baseline 1.
    assert report.verbatim == [0]
    assert report.matched[0].baseline_idx == 1
    assert report.matched[0].clause_type == "term"


def test_trigger_hits_thresholding():
    """A clause matching a trigger verbatim clears the trigger floor; an unrelated clause does not."""
    index = _synthetic_index()

    hit = embed_and_match(_TRIGGERS[0], VARIANT, _fake_provider(), index)
    assert any(
        idx == 0 and score == pytest.approx(1.0, abs=1e-5)
        for idx, _, score in hit.trigger_hits
    )

    # An unrelated clause: fake vectors are near-orthogonal, so it stays below the 0.70 floor.
    clean = embed_and_match(
        "The parties agree to meet for lunch on the first Tuesday of the month.",
        VARIANT,
        _fake_provider(),
        index,
    )
    assert clean.trigger_hits == []


def test_provider_off_returns_empty_report():
    """Provider None (embeddings off) -> the shared empty NO_REPORT, never a raise."""
    index = _synthetic_index()
    assert embed_and_match(_BASELINE[0], VARIANT, None, index) is NO_REPORT
    # Index missing likewise degrades.
    assert embed_and_match(_BASELINE[0], VARIANT, _fake_provider(), None) is NO_REPORT


def test_provider_that_raises_returns_empty_report():
    """A provider whose embed() blows up degrades to an empty report (never propagates)."""

    class _Boom:
        model = "boom"

        def embed(self, texts):
            raise RuntimeError("provider exploded")

    index = _synthetic_index()
    report = embed_and_match(_BASELINE[0], VARIANT, _Boom(), index)
    assert report is NO_REPORT


def test_fake_provider_is_deterministic_and_unit():
    """Same text -> identical unit vector across calls (stable across processes)."""
    a = _fake_provider().embed(["hello world"])
    b = _fake_provider().embed(["hello world"])
    assert np.allclose(a, b)
    assert np.linalg.norm(a[0]) == pytest.approx(1.0, abs=1e-5)


def test_get_provider_off(monkeypatch):
    """The factory returns None when embeddings are off."""
    monkeypatch.setattr(embeddings.settings, "embeddings_provider", "off")
    embeddings.get_provider.cache_clear()
    assert embeddings.get_provider() is None
    embeddings.get_provider.cache_clear()
