"""C3 backend — SemanticLessonStore: bi-temporal parity + semantic recall.

- the store keeps the InMemory bi-temporal contract (supersede/rollback/active)
- semantic_recall ranks ACTIVE lessons by vector similarity and respects the gate
"""
import pytest

from mimir.models import Lesson
from mimir.store_semantic import SemanticLessonStore, InProcessVectorIndex, hash_embed


def _lesson(rule, confidence=0.7, **kw):
    return Lesson(rule=rule, confidence=confidence, **kw)


# --- inherited bi-temporal contract still holds ------------------------------

def test_supersede_and_active_parity():
    store = SemanticLessonStore()
    old = store.add(_lesson("retry network calls once", confidence=0.5))
    new = store.supersede(old, _lesson("retry with exponential backoff", confidence=0.8))
    assert store.get(old).status == "superseded"
    assert store.get(old).invalid_at is not None
    assert {lo.id for lo in store.active()} == {new}


def test_rollback_reactivates():
    store = SemanticLessonStore()
    old = store.add(_lesson("a"))
    store.supersede(old, _lesson("b"))
    store.rollback(old)
    assert old in {lo.id for lo in store.active()}


# --- semantic recall ---------------------------------------------------------

def test_semantic_recall_ranks_by_meaning_over_lexical_position():
    store = SemanticLessonStore()
    store.add(_lesson("flush the buffer before reading or the read returns empty", id="L-flush"))
    store.add(_lesson("paginate the list endpoint, it caps at two items per page", id="L-page"))
    store.add(_lesson("the timer schedule argument is milliseconds not seconds", id="L-unit"))

    hits = store.semantic_recall("my read comes back empty, do I need to flush the buffer", k=2)
    assert hits, "expected at least one semantic hit"
    assert hits[0].id == "L-flush"       # closest by shared vocabulary in vector space


def test_semantic_recall_excludes_superseded_even_if_indexed():
    store = SemanticLessonStore()
    old = store.add(_lesson("flush the buffer before reading", id="L-old"))
    store.supersede(old, _lesson("call sync=True; flushing the buffer is not enough", id="L-new"))
    hits = store.semantic_recall("buffer flush read", k=5)
    ids = {h.id for h in hits}
    assert "L-old" not in ids            # gate is authoritative over the index
    assert "L-new" in ids


def test_hash_embed_is_unit_and_deterministic():
    a, b = hash_embed(["flush the buffer"]), hash_embed(["flush the buffer"])
    assert a == b
    import math
    assert abs(math.sqrt(sum(x * x for x in a[0])) - 1.0) < 1e-9


# --- fastembed: opt-in real local semantic embedder --------------------------

def test_fastembed_embed_normalizes_output_via_injected_model():
    """Pure logic test, no real model: fastembed_embed must unit-normalize whatever
    the model returns, since fastembed doesn't guarantee it and the Embed contract
    (unit vectors -> cosine metric) requires it."""
    import math

    from mimir.store_semantic import fastembed_embed

    class _FakeModel:
        def __init__(self, model_name):
            self.model_name = model_name

        def embed(self, texts):
            return [[3.0, 4.0] for _ in texts]  # 3-4-5 triangle, not unit length

    vecs = fastembed_embed(["a", "b"], model_name="fake-model", _model_cls=_FakeModel)
    assert vecs == [[0.6, 0.8], [0.6, 0.8]]
    for v in vecs:
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9


@pytest.mark.slow
def test_fastembed_embed_live_or_skip():
    pytest.importorskip("fastembed")
    import math

    from mimir.store_semantic import fastembed_embed

    vecs = fastembed_embed(["flush the buffer before reading"])
    assert len(vecs) == 1
    assert abs(math.sqrt(sum(x * x for x in vecs[0])) - 1.0) < 1e-6


def test_injected_index_is_used():
    idx = InProcessVectorIndex()
    store = SemanticLessonStore(index=idx)
    store.add(_lesson("flush the buffer", id="L1"))
    assert idx.query("flush buffer", k=1)[0][0] == "L1"


def test_recall_routes_through_semantic_ranking_and_keeps_tau_gate():
    from mimir.mcp_server import recall

    store = SemanticLessonStore()
    store.add(_lesson("flush the buffer before reading or the read returns empty",
                      confidence=0.9, supporting_episodes=["e1"], id="L-flush"))
    store.add(_lesson("the timer schedule argument is milliseconds not seconds",
                      confidence=0.9, supporting_episodes=["e2"], id="L-unit"))
    # below tau: must be gated out even if semantically closest
    store.add(_lesson("flush flush buffer read empty",
                      confidence=0.1, supporting_episodes=["e3"], id="L-weak"))

    res = recall(store, "read comes back empty, need to flush the buffer", tau=0.5, k=2)
    ids = [lo.id for lo in res.lessons]
    assert ids and ids[0] == "L-flush"   # semantic winner
    assert "L-weak" not in ids           # tau gate still authoritative


# --- live LanceDB, sync API — runs on this box ----

def test_lancedb_vector_index_live(tmp_path):
    pytest.importorskip("lancedb")
    from mimir.store_semantic import LanceDBVectorIndex

    idx = LanceDBVectorIndex(url=str(tmp_path / "lance.db"))
    idx.upsert("L-flush", "flush the buffer before reading or the read returns empty")
    idx.upsert("L-unit", "the timer schedule argument is milliseconds not seconds")
    hits = idx.query("read comes back empty, need to flush the buffer", k=2)
    assert hits and hits[0][0] == "L-flush"          # real on-disk vector search
    # re-upsert is an update, not a duplicate
    idx.upsert("L-flush", "flush the buffer, call sync=True")
    ids = [lid for lid, _ in idx.query("flush buffer sync", k=5)]
    assert ids.count("L-flush") == 1


def test_lancedb_backed_store_keeps_gate(tmp_path):
    pytest.importorskip("lancedb")
    from mimir.store_semantic import LanceDBVectorIndex

    store = SemanticLessonStore(index=LanceDBVectorIndex(url=str(tmp_path / "s.db")))
    old = store.add(_lesson("flush the buffer before reading", id="L-old"))
    store.supersede(old, _lesson("call sync True flush is not enough for the buffer", id="L-new"))
    ids = {h.id for h in store.semantic_recall("flush the buffer", k=5)}
    assert "L-old" not in ids and "L-new" in ids     # gate authoritative over live index
