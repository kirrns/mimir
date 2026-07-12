"""MimirMemoryProvider — Hermes' MemoryProvider ABC, backed by mimir.recall.

Uses InMemoryLessonStore (token-free, no lancedb) since prefetch only needs
`.active()` — the same duck-type recall() already works against.
"""
from mimir.hermes_memory import MimirMemoryProvider
from mimir.models import ACTIVE, Lesson
from mimir.store import InMemoryLessonStore


def _store_with(rule: str, confidence: float = 0.9) -> InMemoryLessonStore:
    store = InMemoryLessonStore()
    store.add(Lesson(rule=rule, confidence=confidence, status=ACTIVE,
                     supporting_episodes=["e1"], id="L1"))
    return store


def test_is_available_reflects_cognee_and_lancedb_importability():
    provider = MimirMemoryProvider()
    assert isinstance(provider.is_available(), bool)  # environment-dependent; just must not raise


def test_prefetch_returns_empty_before_initialize():
    provider = MimirMemoryProvider()
    assert provider.prefetch("anything") == ""


def test_prefetch_surfaces_matching_lesson_rules():
    provider = MimirMemoryProvider()
    provider.initialize("s1", store=_store_with("always run pytest before committing"))
    text = provider.prefetch("pytest committing")
    assert "always run pytest before committing" in text


def test_prefetch_empty_when_nothing_clears_the_recall_gate():
    provider = MimirMemoryProvider()
    provider.initialize("s1", store=_store_with("unrelated rule", confidence=0.9))
    assert provider.prefetch("completely different topic xyz") == ""


def test_sync_turn_and_shutdown_are_safe_no_ops():
    provider = MimirMemoryProvider()
    provider.initialize("s1", store=_store_with("a rule"))
    provider.sync_turn("hi", "hello", session_id="s1")  # must not raise
    provider.shutdown()  # must not raise


def test_get_tool_schemas_and_system_prompt_block_are_empty_for_now():
    provider = MimirMemoryProvider()
    assert provider.get_tool_schemas() == []
    assert provider.system_prompt_block() == ""
