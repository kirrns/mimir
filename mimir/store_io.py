"""Store persistence: build the served/consolidated LESSON store from disk,
and persist LESSON objects back to it. The vector index is a derived cache
rebuilt from the persisted lessons on load, never the source of truth.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from mimir.models import Lesson

DEFAULT_HOME = Path.home() / ".mimir"
DEFAULT_LESSONS = DEFAULT_HOME / "lessons.json"     # persisted LESSON objects (source of truth)
DEFAULT_LANCE = DEFAULT_HOME / "lance.db"           # LanceDB vector index (rebuilt from lessons)
EMBED_MODEL_ENV = "MIMIR_EMBED_MODEL"   # opt-in real semantic embedder (fastembed model name)

_DT_FIELDS = ("valid_from", "invalid_at", "last_validated")


def _embed_fn():
    """None -> LanceDBVectorIndex's own hash_embed default (zero deps, unchanged
    behaviour). Set MIMIR_EMBED_MODEL (e.g. 'BAAI/bge-small-en-v1.5') to opt into
    real local semantic embeddings via fastembed (pip install 'mimir[embed]')."""
    model_name = os.environ.get(EMBED_MODEL_ENV)
    if not model_name:
        return None
    from mimir.store_semantic import fastembed_embed
    return lambda texts: fastembed_embed(texts, model_name=model_name)


def _lesson_from_row(row: dict) -> Lesson:
    data = dict(row)
    for f in _DT_FIELDS:
        v = data.get(f)
        data[f] = datetime.fromisoformat(v) if isinstance(v, str) else None
    return Lesson(**data)


def load_lessons(store, path: Path) -> int:
    """Rehydrate persisted LESSONs into `store` (re-upserting each into its vector index)."""
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        store.add(_lesson_from_row(row))
    return len(rows)


def save_lessons(store, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.snapshot(), encoding="utf-8")  # deterministic JSON (store.snapshot)


def build_store(*, lance_url: Optional[Path] = None, lessons_path: Optional[Path] = None):
    """The served/consolidated store: LanceDB vector engine + persisted lessons."""
    from mimir.store_semantic import LanceDBVectorIndex, SemanticLessonStore

    lance_url = lance_url or DEFAULT_LANCE          # resolved at call time, not frozen at import
    lessons_path = lessons_path or DEFAULT_LESSONS
    embed = _embed_fn()
    index_kwargs = {"embed": embed} if embed is not None else {}
    store = SemanticLessonStore(index=LanceDBVectorIndex(url=str(lance_url), **index_kwargs))
    load_lessons(store, lessons_path)
    return store
