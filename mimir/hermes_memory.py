"""Hermes Agent MemoryProvider — the real recall integration.

Distinct from hermes_plugin/ (a lifecycle-hook plugin that only *captures*
failures via post_tool_call). This is Mimir serving as Hermes' actual memory
backend: `prefetch` is mimir.recall reaching Hermes' prompt.

Maps onto Mimir's existing two-speed design rather than fighting it:
- `prefetch` (called before each turn) -> mimir.recall against the same
  LanceDB-backed store everything else reads from.
- `sync_turn` (called after each turn) is a deliberate no-op here. A turn
  transcript has no failure signal to gate a LESSON on — Mimir only turns
  *failures* into lessons, and the tool-call-level failure signal already
  has a home: hermes_plugin/'s post_tool_call hook. Consolidation
  (LLM judge, ε-gate) stays a separate, deliberate step either way — run
  `mimir consolidate` yourself or on a cron, same as every other integration.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class MimirMemoryProvider:
    """Implements Hermes' MemoryProvider ABC (agent/memory_provider.py)."""

    name = "mimir"

    def __init__(self) -> None:
        self._store = None

    def is_available(self) -> bool:
        try:
            import lancedb  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self, session_id: str, *, store: Optional[object] = None, **kwargs) -> None:
        """`store` is a test-only injection point; real Hermes calls this with just
        session_id + hermes_home, so a normal call builds the standard store."""
        if store is not None:
            self._store = store
        else:
            from mimir.cli import build_store

            self._store = build_store()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []  # ponytail: prefetch already injects lessons per-turn; no tool surface yet

    def system_prompt_block(self) -> str:
        return ""  # ponytail: prefetch covers injection; add a static block if that's not enough

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._store is None:
            return ""
        from mimir.mcp_server import recall

        result = recall(self._store, query)
        if not result.lessons:
            return ""
        return "\n".join(f"- {lesson.rule}" for lesson in result.lessons)

    def sync_turn(self, user_content, assistant_content, *,
                 session_id: str = "", messages=None) -> None:
        pass  # see module docstring: no failure signal at turn granularity

    def shutdown(self) -> None:
        pass  # consolidation stays a deliberate, separate step (mimir consolidate)
