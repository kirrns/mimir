"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

See docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
import json
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
