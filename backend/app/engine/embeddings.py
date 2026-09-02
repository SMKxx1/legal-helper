"""Embedding provider wrapper + precomputed index loader (escalate-only substrate).

This is the low-level substrate for playbook-embedding alignment. It provides two primitives
and nothing else — no wiring into ``run_review`` (a follow-up does that), and every path is a
no-op when ``settings.embeddings_provider == "off"`` (the default):

  * :func:`get_provider` — a factory returning an :class:`EmbeddingProvider` (or ``None`` when
    disabled). A provider's ``embed(texts)`` returns L2-normalized float32 row vectors so a plain
    dot product IS the cosine similarity. ANY failure inside a provider degrades to ``None`` /
    an empty array and is logged — an embedding call NEVER raises out (matches the
    router-failure norm in ``routes_v1``: a broken optional signal must not fail the review).
  * :func:`load_index` — the lru-cached loader for the single precomputed ``.npz`` built offline
    by ``scripts/build_playbook_embeddings.py``. The index records the playbook release it was
    built against; if that does not match the running process the index is logged and IGNORED
    (a stale index must never be silently used).

Design constraint (decided, not relitigated here): the corpus is ~1,500 STATIC vectors, so there
is NO vector database — the index is one dict of per-category 2D arrays, brute-forced with a single
numpy matmul at the call site.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.config import settings
from app.playbook.release import playbook_release_id

log = logging.getLogger(__name__)

_REPO = (
    Path(__file__).resolve().parents[3]
)  # embeddings.py -> engine/ -> app/ -> backend/ -> repo


class EmbeddingProvider(Protocol):
    """A text -> unit-vector embedder. ``model`` names the underlying model (audit/observability)."""

    model: str

    def embed(self, texts: list[str]) -> np.ndarray | None:
        """Embed ``texts`` into an ``(n, dim)`` float32 array of L2-normalized rows.

        Returns ``None`` on ANY failure (never raises); an empty input yields an empty array.
        """
        ...


def _l2_normalize(vecs: np.ndarray) -> np.ndarray:
    """Row-normalize so a dot product is the cosine. A zero row is left at zero (cosine 0)."""
    vecs = np.asarray(vecs, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (vecs / norms).astype(np.float32)


@dataclass(slots=True)
class _FakeProvider:
    """Deterministic hash-seeded unit vectors — stable across processes, no network.

    Identical text always maps to the identical vector (so ``cosine(x, x) == 1.0``); the seed is
    ``sha256(text)`` so vectors are reproducible in tests without pinning a real API.
    """

    model: str
    dim: int = 64

    def embed(self, texts: list[str]) -> np.ndarray | None:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        try:
            rows = np.empty((len(texts), self.dim), dtype=np.float32)
            for i, text in enumerate(texts):
                digest = hashlib.sha256((text or "").encode("utf-8")).digest()
                # Seed a per-text PRNG from the digest — deterministic across processes/platforms.
                seed = struct.unpack("<Q", digest[:8])[0]
                rng = np.random.default_rng(seed)
                rows[i] = rng.standard_normal(self.dim, dtype=np.float32)
            return _l2_normalize(rows)
        except Exception:  # noqa: BLE001 — an embedder never raises out (router-failure norm)
            log.exception("fake embedding provider failed")
            return None


@dataclass(slots=True)
class _VoyageProvider:
    """Voyage AI embeddings. The ``voyageai`` SDK is imported lazily so it is not a hard dep."""

    model: str
    api_key: str
    _client: Any = (
        None  # cached voyageai.Client — built once per provider, not per embed call
    )

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import voyageai  # noqa: PLC0415 — lazy: optional, not in requirements.txt
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise RuntimeError(
                    "embeddings_provider='voyage' requires the voyageai SDK; run "
                    "`pip install voyageai` (it is intentionally not pinned in requirements.txt)"
                ) from exc
            self._client = voyageai.Client(api_key=self.api_key)
        return self._client

    def embed(self, texts: list[str]) -> np.ndarray | None:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        try:
            client = self._get_client()
            result = client.embed(texts, model=self.model, input_type="document")
            return _l2_normalize(np.asarray(result.embeddings, dtype=np.float32))
        except Exception:  # noqa: BLE001 — degrade, never fail the caller
            log.exception("voyage embedding provider failed")
            return None


@lru_cache(maxsize=1)
def get_provider() -> EmbeddingProvider | None:
    """The configured provider, or ``None`` when embeddings are off / misconfigured.

    lru-cached: like the engine gateways, flipping ``embeddings_provider`` takes effect on a
    process restart (matches the deploy invariant that a config change ships as a new process).
    """
    provider = (settings.embeddings_provider or "off").strip().lower()
    if provider == "off":
        return None
    if provider == "fake":
        return _FakeProvider(model=settings.embeddings_model)
    if provider == "voyage":
        key = settings.voyage_api_key or ""
        if not key:
            log.error(
                "embeddings_provider='voyage' but voyage_api_key is unset; disabling embeddings"
            )
            return None
        return _VoyageProvider(model=settings.embeddings_model, api_key=key)
    log.error("unknown embeddings_provider %r; disabling embeddings", provider)
    return None


@dataclass(slots=True)
class PlaybookIndex:
    """The precomputed embedding index for the whole v4 playbook.

    ``vectors`` maps ``(variant_key, category) -> (rows, dim)`` float32 arrays whose rows are the
    embeddings for that variant/category, in the same order as ``meta`` for that key. ``meta`` maps
    the same key to the per-row metadata records (each a dict with at least ``text``, ``sha`` and,
    for positions, ``clause_type``). Categories: ``baseline`` | ``positions`` | ``triggers`` |
    ``fallbacks`` | ``carveouts``.
    """

    release: str
    vectors: dict[tuple[str, str], np.ndarray]
    meta: dict[tuple[str, str], list[dict]]

    def get(self, variant_key: str, category: str) -> tuple[np.ndarray, list[dict]]:
        """Vectors + metadata for a ``(variant_key, category)``; empty pair when absent."""
        key = (variant_key, category)
        vecs = self.vectors.get(key)
        if vecs is None:
            return np.empty((0, 0), dtype=np.float32), []
        return vecs, self.meta.get(key, [])

    def baseline_hashes(self, variant_key: str) -> set[str]:
        """The set of normalized-text shas for the variant's baseline clauses (identity check)."""
        return {
            r["sha"]
            for r in self.meta.get((variant_key, "baseline"), [])
            if r.get("sha")
        }


# The metadata JSON key under which the offline build records the playbook release id.
_META_RELEASE_KEY = "playbook_release"


@lru_cache(maxsize=1)
def load_index() -> PlaybookIndex | None:
    """Load the precomputed index, or ``None`` when it is missing / stale / unreadable.

    The npz stores per ``(variant_key, category)`` a 2D float32 array under the array name
    ``vec::<variant_key>::<category>`` and its metadata under ``meta::<variant_key>::<category>``
    (a JSON string array), plus a top-level ``__meta__`` JSON object recording the playbook release.
    If that release does not match the running process's :func:`playbook_release_id` the index is
    logged and IGNORED — a stale index is never silently used.
    """
    path = Path(settings.embed_playbook_index_path)
    if not path.is_absolute():
        path = _REPO / path
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as npz:
            top = json.loads(str(npz["__meta__"].item())) if "__meta__" in npz else {}
            release = str(top.get(_META_RELEASE_KEY, ""))
            current = playbook_release_id()
            if release != current:
                log.warning(
                    "playbook embedding index at %s is stale (index release %r != current %r); ignoring",
                    path,
                    release,
                    current,
                )
                return None
            vectors: dict[tuple[str, str], np.ndarray] = {}
            meta: dict[tuple[str, str], list[dict]] = {}
            for name in npz.files:
                if name.startswith("vec::"):
                    _, variant_key, category = name.split("::", 2)
                    vectors[(variant_key, category)] = np.asarray(
                        npz[name], dtype=np.float32
                    )
                elif name.startswith("meta::"):
                    _, variant_key, category = name.split("::", 2)
                    meta[(variant_key, category)] = json.loads(str(npz[name].item()))
        return PlaybookIndex(release=release, vectors=vectors, meta=meta)
    except Exception:  # noqa: BLE001 — a broken index degrades to None, never raises out
        log.exception("failed to load playbook embedding index at %s; ignoring", path)
        return None
