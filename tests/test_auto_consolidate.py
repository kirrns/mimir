"""C1.5 -- the auto-trigger gate between capture (C1) and consolidate (C2).

See docs/superpowers/specs/2026-07-17-auto-consolidate-design.md.
"""
import json

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
