"""C3 backend — semantic LESSON recall over a vector index.

A `CogneeLessonStore` that keeps the proven bi-temporal CRUD (inherited from
InMemoryLessonStore, so every C3 contract test still holds) and adds
`semantic_recall` over a pluggable `VectorIndex`.

Two indexes implement the same seam:
  - `LanceDBVectorIndex`   — real on-disk vector DB (LanceDB direct, sync API).
  - `InProcessVectorIndex` — a pure-Python cosine index, zero deps, never blocks.

(A third, `CogneeVectorIndex`, went through cognee's own async LanceDB adapter
instead of talking to LanceDB directly; removed — its async writer hung on
Py3.14/Windows, it was never wired into the live path (`build_store()` always
used `LanceDBVectorIndex`), and it added a heavy import + a whole optional
dependency for zero behavioural difference from the index above.)

The embedding function is injected (the same DI pattern as the live judge/probe/
CLI runner): a hash embedder for tests (no key, no download, deterministic), a
real model for a live demo. So semantic recall runs and is testable token-free.

ponytail: default index is in-process (zero deps, never blocks). For a live vector
DB on this box, pass `LanceDBVectorIndex(url=...)`. Both share one seam, so the
backend is a constructor arg, not a rewrite.
"""
from __future__ import annotations

import hashlib
import math
from typing import Callable, Optional, Protocol

from mimir.models import Lesson
from mimir.store import InMemoryLessonStore

# --- embedding (injectable) --------------------------------------------------

Embed = Callable[[list[str]], list[list[float]]]
_DIM = 64


def hash_embed(texts: list[str], *, dim: int = _DIM) -> list[list[float]]:
    """A token-hashing bag-of-words embedder: no model, no network, deterministic.

    Good enough for tests and a zero-dependency demo — shared vocabulary between a
    query and a lesson lands them near each other in cosine space. Swap for a real
    sentence embedder via the `embed` arg for production recall.
    """
    vecs: list[list[float]] = []
    for text in texts:
        v = [0.0] * dim
        for tok in text.lower().split():
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
            v[h % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        vecs.append([x / norm for x in v])
    return vecs


def _unit(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"


def fastembed_embed(texts: list[str], *, model_name: str = DEFAULT_FASTEMBED_MODEL,
                    _model_cls: Optional[type] = None) -> list[list[float]]:
    """Real local semantic embedder (ONNX via fastembed — no torch, no network after
    the first model download). Opt-in: `pip install 'mimir[embed]'` and set
    MIMIR_EMBED_MODEL (see cli.py build_store()); hash_embed stays the zero-dependency
    default otherwise. Output is unit-normalized to match hash_embed's contract
    (`Embed`: unit vectors -> cosine metric), since fastembed doesn't guarantee it.
    `_model_cls` is a test seam (inject a fake TextEmbedding, no real download).
    """
    if _model_cls is None:
        from fastembed import TextEmbedding as _model_cls  # noqa: N812 - lazy, optional dep
    model = _model_cls(model_name=model_name)
    return [_unit(list(v)) for v in model.embed(texts)]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are unit vectors


# --- vector index seam -------------------------------------------------------

class VectorIndex(Protocol):
    def upsert(self, lesson_id: str, text: str) -> None: ...
    def query(self, text: str, k: int) -> list[tuple[str, float]]: ...


class InProcessVectorIndex:
    """Pure-Python cosine index. Zero deps, never blocks — the safe default."""

    def __init__(self, embed: Embed = hash_embed) -> None:
        self._embed = embed
        self._vecs: dict[str, list[float]] = {}

    def upsert(self, lesson_id: str, text: str) -> None:
        self._vecs[lesson_id] = self._embed([text])[0]

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        q = self._embed([text])[0]
        scored = [(lid, _cosine(q, v)) for lid, v in self._vecs.items()]
        scored.sort(key=lambda p: p[1], reverse=True)
        return [(lid, s) for lid, s in scored[:k] if s > 0.0]


class LanceDBVectorIndex:
    """LanceDB (Cognee's own vector engine) via its SYNC API — runs live here.

    Cognee stores vectors in LanceDB. Its async adapter hangs on this Py3.14 /
    Windows box, but LanceDB's sync writer works fine, so this talks to the same
    real on-disk vector database directly. Same seam as the in-process index;
    persists to `url`. Embedding is injected (unit vectors -> cosine metric).
    """

    TABLE = "mimir_lessons"

    def __init__(self, url: str, *, embed: Embed = hash_embed, dim: int = _DIM) -> None:
        import lancedb

        self._embed = embed
        self._dim = dim
        self._db = lancedb.connect(url)
        self._tbl = None  # created lazily on first upsert (needs a row for schema)

    def _row(self, lesson_id: str, text: str) -> dict:
        return {"lesson_id": lesson_id, "vector": self._embed([text])[0]}

    def upsert(self, lesson_id: str, text: str) -> None:
        row = self._row(lesson_id, text)
        if self._tbl is None:
            try:  # reuse a table left by a prior process (consolidate -> serve); else create
                self._tbl = self._db.open_table(self.TABLE)
            except (FileNotFoundError, ValueError):
                self._tbl = self._db.create_table(self.TABLE, [row])
                return
        # ponytail: lesson ids are our own slugs (no quotes), so this filter is safe;
        # switch to a parameterised delete if ids ever come from untrusted input.
        self._tbl.delete(f"lesson_id = '{lesson_id}'")
        self._tbl.add([row])

    def query(self, text: str, k: int) -> list[tuple[str, float]]:
        if self._tbl is None:
            return []
        q = self._embed([text])[0]
        rows = self._tbl.search(q).metric("cosine").limit(k).to_list()
        # cosine distance = 1 - similarity; keep only positive-similarity hits
        out = [(r["lesson_id"], 1.0 - float(r["_distance"])) for r in rows]
        return [(lid, s) for lid, s in out if s > 0.0]


# --- the store ---------------------------------------------------------------

class CogneeLessonStore(InMemoryLessonStore):
    """Bi-temporal lesson store + semantic recall over a vector index.

    CRUD/bi-temporal behaviour is inherited unchanged (C3 contract holds). Every
    active-making write mirrors the lesson's `rule` into the vector index so
    `semantic_recall` ranks lessons by meaning, not lexical overlap. The index is
    injected; it defaults to the no-dep in-process cosine index.
    """

    def __init__(self, index: Optional[VectorIndex] = None) -> None:
        super().__init__()
        self._index: VectorIndex = index or InProcessVectorIndex()

    def add(self, lesson: Lesson) -> str:
        lid = super().add(lesson)
        self._index.upsert(lid, lesson.rule)
        return lid

    def semantic_recall(self, query: str, *, k: int = 5) -> list[Lesson]:
        """Return ACTIVE lessons ranked by vector similarity to `query`.

        The gate stays authoritative: index hits are intersected with active(), so
        superseded/quarantined lessons never surface even if still in the index.
        """
        active = {lo.id: lo for lo in self.active()}
        ranked = self._index.query(query, k=k * 2)  # over-fetch; active() prunes
        out = [active[lid] for lid, _ in ranked if lid in active]
        return out[:k]
