"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

See docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from mimir import auto_consolidate as ac


def test_bump_failure_count_creates_state_file_starting_at_one(tmp_path):
    state_path = tmp_path / "state.json"
    ac.bump_failure_count(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_total"] == 1


def test_bump_failure_count_accumulates_across_calls(tmp_path):
    state_path = tmp_path / "state.json"
    ac.bump_failure_count(state_path)
    ac.bump_failure_count(state_path)
    ac.bump_failure_count(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["failure_count_total"] == 3


def test_bump_failure_count_never_raises_on_unwritable_path(tmp_path):
    bad = tmp_path  # a directory, not a file -- the write must fail internally
    ac.bump_failure_count(bad)  # must not raise


def _write_state_file(path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_is_due_false_below_threshold(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state_file(state_path, failure_count_total=3, failure_count_at_last_run=0)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is False


def test_is_due_true_on_first_ever_check_once_threshold_met(tmp_path):
    state_path = tmp_path / "state.json"  # no last_run_ts -- never run before
    _write_state_file(state_path, failure_count_total=5, failure_count_at_last_run=0)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is True


def test_is_due_false_when_cooldown_not_elapsed(tmp_path):
    state_path = tmp_path / "state.json"
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_state_file(state_path, failure_count_total=10, failure_count_at_last_run=0,
                      last_run_ts=recent)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is False


def test_is_due_true_when_threshold_met_and_cooldown_elapsed(tmp_path):
    state_path = tmp_path / "state.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    _write_state_file(state_path, failure_count_total=10, failure_count_at_last_run=0,
                      last_run_ts=old)
    assert ac.is_due(state_path, threshold=5, cooldown_hours=4) is True


def test_acquire_lock_succeeds_when_absent(tmp_path):
    lock_path = tmp_path / "lock"
    assert ac._acquire_lock(lock_path) is True
    assert lock_path.exists()


def test_acquire_lock_fails_when_fresh_lock_exists(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    assert ac._acquire_lock(lock_path) is False


def test_acquire_lock_reclaims_stale_lock(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    stale_time = time.time() - (ac.LOCK_STALE_HOURS * 3600 + 60)
    os.utime(lock_path, (stale_time, stale_time))
    assert ac._acquire_lock(lock_path) is True


def test_acquire_lock_backs_off_if_lock_refreshed_during_reclaim(tmp_path, monkeypatch):
    """Simulates two processes racing to reclaim the same stale lock: if the
    lock's mtime changes between the staleness check and the unlink, back off
    instead of stomping the other process's fresh lock."""
    lock_path = tmp_path / "lock"
    lock_path.write_text("", encoding="utf-8")
    stale_time = time.time() - (ac.LOCK_STALE_HOURS * 3600 + 60)
    fresh_time = time.time()

    mtimes = iter([stale_time, fresh_time])
    monkeypatch.setattr(ac, "_stat_mtime", lambda path: next(mtimes))

    assert ac._acquire_lock(lock_path) is False
    assert lock_path.exists()  # not stomped -- still the original file
