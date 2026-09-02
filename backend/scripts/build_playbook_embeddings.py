"""Offline builder for the precomputed playbook embedding index.

Embeds, per v4 variant, the texts the alignment substrate matches against, and writes ONE npz at
``playbook/v4/embeddings.npz``:

  * baseline clauses  — the variant's baseline .md, segmented by the production segmenter;
  * positions         — each position's ``standard_position`` text (carries ``clause_type``);
  * triggers          — every ``walk_away_triggers`` entry across positions;
  * fallbacks         — every ``acceptable_fallbacks`` entry across positions;
  * carveouts         — the variant's required carveouts.

Layout (see ``app.engine.embeddings.load_index``): per ``(variant_key, category)`` a 2D float32
array under ``vec::<variant_key>::<category>`` and a JSON string array under
``meta::<variant_key>::<category>`` mapping row -> {text, sha, clause_type?, clause_index?}. A
top-level ``__meta__`` JSON object records the playbook release the index was built against, so a
stale index (built under a different release) is detected and ignored at load time.

Run from the ``backend/`` directory with the embeddings provider CONFIGURED, e.g.:

    EMBEDDINGS_PROVIDER=voyage VOYAGE_API_KEY=... .venv/bin/python scripts/build_playbook_embeddings.py

This performs live embedding API calls; it is NOT run in CI or by the test suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]  # scripts/ -> backend/ -> repo
sys.path.insert(0, str(_REPO / "backend"))

from app.engine.embeddings import get_provider  # noqa: E402
from app.engine.simcache import norm_sha256  # noqa: E402
from app.ingestion.segmenter import segment_clauses  # noqa: E402
from app.playbook.release import playbook_release_id  # noqa: E402

_V4 = _REPO / "playbook" / "v4"
_MANIFEST = _V4 / "manifest.json"
_OUT = _V4 / "embeddings.npz"


def _row(text: str, **extra: object) -> dict:
    """A metadata record: source text + its normalized-text sha (+ any category-specific fields)."""
    return {"text": text, "sha": norm_sha256(text), **extra}


def _collect(
    variant_key: str, playbook: dict, baseline_md: str
) -> dict[str, list[str]]:
    """Return {category: [text, ...]} and stash the metadata rows on the module-level buffer."""
    texts: dict[str, list[str]] = {}
    meta: dict[str, list[dict]] = {}

    # baseline clauses (segmented like production input).
    clauses = segment_clauses(baseline_md)
    texts["baseline"] = [c.text for c in clauses]
    meta["baseline"] = [_row(c.text, clause_index=c.index) for c in clauses]

    positions = playbook.get("positions", [])
    texts["positions"] = [p.get("standard_position", "") for p in positions]
    meta["positions"] = [
        _row(p.get("standard_position", ""), clause_type=p.get("clause_type", ""))
        for p in positions
    ]

    triggers = [t for p in positions for t in p.get("walk_away_triggers", [])]
    texts["triggers"] = triggers
    meta["triggers"] = [_row(t) for t in triggers]

    fallbacks = [f for p in positions for f in p.get("acceptable_fallbacks", [])]
    texts["fallbacks"] = fallbacks
    meta["fallbacks"] = [_row(f) for f in fallbacks]

    carveouts = playbook.get("defaults", {}).get("required_carveouts", [])
    texts["carveouts"] = list(carveouts)
    meta["carveouts"] = [_row(c) for c in carveouts]

    _META_BUF[variant_key] = meta
    return texts


_META_BUF: dict[str, dict[str, list[dict]]] = {}


def main() -> int:
    provider = get_provider()
    if provider is None:
        print(
            "no embeddings provider configured — set EMBEDDINGS_PROVIDER (voyage) and its key",
            file=sys.stderr,
        )
        return 2

    manifest = json.loads(_MANIFEST.read_text())
    out: dict[str, np.ndarray] = {}

    for entry in manifest["playbooks"]:
        variant_key = entry["variant_key"]
        playbook = json.loads((_REPO / entry["playbook"]).read_text())
        baseline_md = (_REPO / entry["baseline"]).read_text()
        texts = _collect(variant_key, playbook, baseline_md)

        for category, cat_texts in texts.items():
            if not cat_texts:
                continue
            vecs = provider.embed(cat_texts)
            if vecs is None or vecs.shape[0] != len(cat_texts):
                print(
                    f"embedding failed for {variant_key}/{category}; aborting",
                    file=sys.stderr,
                )
                return 1
            out[f"vec::{variant_key}::{category}"] = vecs.astype(np.float32)
            # Store metadata as a single JSON string (unicode array, pickle-free so the loader can
            # read it with allow_pickle=False).
            out[f"meta::{variant_key}::{category}"] = np.array(
                json.dumps(_META_BUF[variant_key][category])
            )
        print(
            f"embedded {variant_key}: "
            + ", ".join(f"{k}={len(v)}" for k, v in texts.items())
        )

    out["__meta__"] = np.array(
        json.dumps(
            {
                "playbook_release": playbook_release_id(),
                "model": getattr(provider, "model", ""),
            }
        )
    )
    np.savez_compressed(_OUT, **out)
    print(f"wrote {_OUT} ({len(manifest['playbooks'])} variants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
