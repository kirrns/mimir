"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

Keeps the "should we consolidate now" check O(1) regardless of episode-log size:
capture() bumps a small integer counter on every FAIL episode instead of this module
ever re-scanning episodes.jsonl. See
docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("mimir.auto_consolidate")

DEFAULT_STATE = Path.home() / ".mimir" / "auto_consolidate_state.json"
DEFAULT_LOCK = Path.home() / ".mimir" / "auto_consolidate.lock"
DEFAULT_WORKER_LOG = Path.home() / ".mimir" / "auto_consolidate.log"

ENABLED_ENV = "MIMIR_AUTO_CONSOLIDATE"
THRESHOLD_ENV = "MIMIR_AUTO_CONSOLIDATE_THRESHOLD"
COOLDOWN_ENV = "MIMIR_AUTO_CONSOLIDATE_COOLDOWN_HOURS"
DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN_HOURS = 4.0
LOCK_STALE_HOURS = 2.0


def _read_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def bump_failure_count(state_path: Optional[Path] = None) -> None:
    """Called by capture() on every FAIL episode. Never raises."""
    path = state_path or DEFAULT_STATE
    try:
        state = _read_state(path)
        state["failure_count_total"] = state.get("failure_count_total", 0) + 1
        _write_state(path, state)
    except Exception:
        log.exception("mimir auto_consolidate failed to bump failure counter (non-fatal)")


def is_due(state_path: Optional[Path] = None, *, threshold: int, cooldown_hours: float) -> bool:
    path = state_path or DEFAULT_STATE
    state = _read_state(path)
    total = state.get("failure_count_total", 0)
    at_last_run = state.get("failure_count_at_last_run", 0)
    if total - at_last_run < threshold:
        return False
    last_run_ts = state.get("last_run_ts")
    if last_run_ts is None:
        return True
    last_run = datetime.fromisoformat(last_run_ts)
    return datetime.now(timezone.utc) - last_run >= timedelta(hours=cooldown_hours)


def _lock_is_stale(lock_path: Path) -> bool:
    age_seconds = time.time() - lock_path.stat().st_mtime
    return age_seconds >= LOCK_STALE_HOURS * 3600


def _acquire_lock(lock_path: Optional[Path] = None) -> bool:
    """Atomically create the lock file. True if acquired; False if a fresh lock
    already exists (a run is in flight). Reclaims a stale lock."""
    path = lock_path or DEFAULT_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        if _lock_is_stale(path):
            path.unlink(missing_ok=True)
            return _acquire_lock(path)
        return False
