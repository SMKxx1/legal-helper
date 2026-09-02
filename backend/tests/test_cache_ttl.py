"""Gated 1h prompt-cache TTL for the deep/Opus prefix (3.3 hardening).

The cache read/write COST accounting already shipped (pricing multipliers + adapter pass-through);
what this adds is the opt-in extended TTL — and the invariant that the accounting always matches
the TTL the request actually used: a 1h cache write bills 2x the base input rate, not 1.25x.
Flag off => request bytes identical to before (no ttl field at all).
"""

from __future__ import annotations

from app.ai.gateway import GatewayRequest, build_anthropic_request
from app.pricing import cost_for

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "string"}},
    "additionalProperties": False,
}


def _req() -> GatewayRequest:
    return GatewayRequest(
        role="t", schema=_SCHEMA, system="sys", task="task", stable_blocks=["stable"]
    )


def test_default_request_has_no_ttl_field():
    kw = build_anthropic_request(_req(), "claude-opus-4-8")
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}  # byte-identical


def test_1h_ttl_sets_the_extended_cache_control():
    kw = build_anthropic_request(_req(), "claude-opus-4-8", cache_ttl="1h")
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_1h_cache_write_bills_2x_not_1_25x():
    table = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}
    base = cost_for("claude-opus-4-8", 0, 0, table, cache_write_tokens=1_000_000)
    ext = cost_for(
        "claude-opus-4-8",
        0,
        0,
        table,
        cache_write_tokens=1_000_000,
        cache_write_ttl="1h",
    )
    assert base == 5.0 * 1.25
    assert ext == 5.0 * 2.0
    # Reads are TTL-independent (0.1x either way).
    read = cost_for(
        "claude-opus-4-8",
        0,
        0,
        table,
        cache_read_tokens=1_000_000,
        cache_write_ttl="1h",
    )
    assert read == 5.0 * 0.10


def test_deep_primary_gets_1h_only_when_flag_on(monkeypatch):
    from app.api import routes_v1

    routes_v1._build_gateways.cache_clear()

    # Flag OFF (default): every adapter keeps the 5m default.
    off = routes_v1._build_gateways("deep", "sk-test", False)
    assert off["primary"].adapter.cache_ttl == "5m"
    assert off["router"].adapter.cache_ttl == "5m"

    # Flag ON, deep tier: the Opus primary gets 1h; the Haiku router stays 5m.
    on = routes_v1._build_gateways("deep", "sk-test", True)
    assert on["primary"].adapter.model_id == "claude-opus-4-8"
    assert on["primary"].adapter.cache_ttl == "1h"
    assert on["router"].adapter.cache_ttl == "5m"

    # Quick tier is never extended even with the flag on (build_engine_gateways
    # additionally ANDs mode == "deep" before passing the flag).
    quick = routes_v1._build_gateways("quick", "sk-test", False)
    assert quick["primary"].adapter.cache_ttl == "5m"
    routes_v1._build_gateways.cache_clear()


def test_adapter_prices_with_its_own_ttl(monkeypatch):
    """The adapter must bill the SAME TTL it requested — a 1h request priced at 1.25x
    would let real spend outrun the soft monthly cap."""
    from app.ai.adapters import AnthropicAdapter

    seen: dict = {}

    def _spy_cost_for(model, inp, out, table=None, **kwargs):
        seen.update(kwargs)
        return 0.0

    monkeypatch.setattr("app.ai.adapters.cost_for", _spy_cost_for)

    adapter = AnthropicAdapter("sk-test", "claude-opus-4-8", cache_ttl="1h")

    class _U:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 100
        cache_creation_input_tokens = 200

    class _Block:
        type = "text"
        text = '{"x": "ok"}'

    class _R:
        stop_reason = "end_turn"
        content = [_Block()]
        usage = _U()
        model = "claude-opus-4-8"

    monkeypatch.setattr(
        adapter,
        "_client",
        type(
            "C",
            (),
            {"messages": type("M", (), {"create": staticmethod(lambda **kw: _R())})()},
        )(),
    )
    raw = adapter.complete(_req())
    assert seen["cache_write_ttl"] == "1h"
    assert seen["cache_read_tokens"] == 100
    assert seen["cache_write_tokens"] == 200
    assert raw.usage.cache_write_tokens == 200


# --------------------------------------------------------------------------- #
# Content-cache release keying (audit #3): a review is reused only by the SAME
# playbook release; a release change (or a legacy NULL row) misses.
# --------------------------------------------------------------------------- #
def test_playbook_release_id_tracks_the_pinned_source(tmp_path, monkeypatch):
    from app.config import settings
    from app.playbook import release

    release._release_id_for_path.cache_clear()
    pb = tmp_path / "pb.json"
    pb.write_text('{"positions": []}')
    monkeypatch.setattr(settings, "engine_playbook_path", str(pb), raising=False)

    first = release.playbook_release_id()
    assert first and len(first) == 16

    # Editing the pinned playbook changes the release id (after the per-path cache is cleared —
    # documents the process-restart invariant).
    pb.write_text('{"positions": [{"clause_type": "x"}]}')
    release._release_id_for_path.cache_clear()
    assert release.playbook_release_id() != first


def test_release_id_tracks_referenced_variant_content(tmp_path, monkeypatch):
    """A content-only edit to a manifest-referenced variant playbook (mapping unchanged) MUST
    change the release id — otherwise the content caches serve reviews graded under the old
    playbook. Runs entirely against tmp_path; the real playbook files are never touched."""
    import json

    from app.config import settings
    from app.playbook import release

    # Build a throwaway repo whose manifest references one variant playbook + baseline.
    repo = tmp_path
    (repo / "playbook" / "v4" / "baselines").mkdir(parents=True)
    variant = repo / "playbook" / "v4" / "US_Company.json"
    baseline = repo / "playbook" / "v4" / "baselines" / "US_Company.md"
    variant.write_text('{"positions": [{"clause_type": "x"}]}')
    baseline.write_text("# baseline v1\n")
    manifest = repo / "playbook" / "v4" / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "playbooks": [
                    {
                        "variant_key": "US_Company",
                        "playbook": "playbook/v4/US_Company.json",
                        "baseline": "playbook/v4/baselines/US_Company.md",
                    }
                ]
            }
        )
    )

    # Point release.py at the throwaway repo/manifest and clear all per-path caches.
    monkeypatch.setattr(release, "_REPO", repo)
    monkeypatch.setattr(release, "_V4_MANIFEST", manifest)
    monkeypatch.setattr(settings, "engine_playbook_path", "", raising=False)
    release._release_id_for_path.cache_clear()

    first = release.playbook_release_id()
    assert first and len(first) == 16

    # Edit ONLY the referenced variant's bytes (manifest mapping untouched) -> id must change.
    variant.write_text('{"positions": [{"clause_type": "x", "risk_weight": 5}]}')
    release._release_id_for_path.cache_clear()
    after_variant = release.playbook_release_id()
    assert after_variant != first

    # Same for a baseline .md content edit.
    baseline.write_text("# baseline v2 — reworded\n")
    release._release_id_for_path.cache_clear()
    assert release.playbook_release_id() != after_variant


def _save(reviews_repo, *, doc_sha256, norm_sha256, release_id):
    """Persist one review row directly with an explicit playbook release, bypassing the engine."""
    import uuid

    from app.models import EngineReview

    rid = uuid.uuid4().hex
    with reviews_repo.SessionLocal() as s:
        s.add(
            EngineReview(
                id=rid,
                mode="quick",
                doc_sha256=doc_sha256,
                norm_sha256=norm_sha256,
                playbook_release=release_id,
                payload_json={"review_id": rid},
            )
        )
        s.commit()
    return rid


def test_find_existing_review_keys_on_playbook_release(session_factory, monkeypatch):
    from app.api import reviews_repo

    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "rel-A")
    rid = _save(reviews_repo, doc_sha256="a" * 64, norm_sha256="", release_id="rel-A")

    # Same release -> hit.
    hit = reviews_repo.find_existing_review("a" * 64, "quick")
    assert hit is not None and hit["review_id"] == rid

    # Release changed -> miss (a stale review is never re-served).
    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "rel-B")
    assert reviews_repo.find_existing_review("a" * 64, "quick") is None


def test_find_similar_review_keys_on_playbook_release(session_factory, monkeypatch):
    from app.api import reviews_repo
    from app.config import settings

    monkeypatch.setattr(settings, "sim_cache_enabled", True, raising=False)
    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "rel-A")
    rid = _save(reviews_repo, doc_sha256="", norm_sha256="n" * 64, release_id="rel-A")

    hit = reviews_repo.find_similar_review("n" * 64, "quick")
    assert hit is not None and hit["review_id"] == rid

    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "rel-B")
    assert reviews_repo.find_similar_review("n" * 64, "quick") is None


def test_legacy_null_release_row_never_matches(session_factory, monkeypatch):
    """A pre-migration row (playbook_release NULL) must miss both tiers and get a fresh review."""
    from app.api import reviews_repo
    from app.config import settings

    monkeypatch.setattr(settings, "sim_cache_enabled", True, raising=False)
    monkeypatch.setattr(reviews_repo, "playbook_release_id", lambda: "rel-A")
    _save(reviews_repo, doc_sha256="c" * 64, norm_sha256="d" * 64, release_id=None)

    assert reviews_repo.find_existing_review("c" * 64, "quick") is None
    assert reviews_repo.find_similar_review("d" * 64, "quick") is None


def test_gateway_counts_cache_tokens():
    from app.ai.gateway import Gateway, RawResult, Usage

    class _A:
        name = "fake"
        model_id = "fake-model"

        def complete(self, req):
            return RawResult(
                text='{"x": "ok"}',
                usage=Usage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_tokens=100,
                    cache_write_tokens=200,
                ),
                model_version="fake-model",
            )

    gw = Gateway(_A())
    gw.run(_req(), max_retries=0)
    assert gw.usage_cache_read_tokens == 100
    assert gw.usage_cache_write_tokens == 200
