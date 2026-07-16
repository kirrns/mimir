# Dev-loop speed: skip environment-probe tests by default

Date: 2026-07-16
Status: approved, implementing directly (small/low-risk — see note below)

## Context

This is sub-project 1 of a 4-part "make Mimir efficient and fast" initiative
(the other three: runtime hot paths in recall()/consolidate(), Cognee/LanceDB
storage-backend efficiency, live-benchmark cost/latency — each gets its own
design before implementation).

Full suite: 123 passed, 1 skipped in ~33s. `pytest --durations=10` shows two
tests account for ~30s of that:

- `tests/test_store_cognee.py::test_cognee_vector_index_live_or_skip` (25s) —
  spawns a thread that exercises the real Cognee async adapter, joins with a
  25s timeout, and self-skips if it hangs (the known Py3.14/Windows hang
  documented in this repo's own memory). The 25s is a deliberate wait-out, not
  a bug.
- `tests/test_hermes_memory.py::test_is_available_reflects_cognee_and_lancedb_importability`
  (5.4s) — does a real `import cognee` / `import lancedb`, which is
  legitimately slow to import.

Neither is algorithmically wrong; both are real environment probes that
happen to be expensive on every single local run. Everything else in the
suite runs in under 2s combined.

## Scope

In scope: make the default local `pytest` run fast by skipping these two
tests, while keeping full coverage in CI.

Out of scope: shrinking the 25s timeout itself (rejected in the design
Q&A — the timeout is correct for the case it's guarding against; the fix is
not paying it every run, not making the wait shorter).

## Design

1. `pyproject.toml`: register a `slow` marker under
   `[tool.pytest.ini_options]` and change `addopts` to
   `"-q -m 'not slow'"` — bare local `pytest` now skips slow tests by default.
2. Tag the two tests above with `@pytest.mark.slow`.
3. `.github/workflows/tests.yml`: change the run step to
   `pytest -m ""` so CI's marker selection overrides the local default and
   the full suite (including both slow tests) still runs on every push/PR —
   no coverage lost, only the local dev loop gets faster.

Devs who want the full suite locally run `pytest -m ""` (or `pytest -m slow`
for just the two).

## Testing

This is pytest configuration, not application logic — verified empirically
(ponytail: YAGNI applies to tests too; a marker isn't a code path to unit
test). Check: `pytest` (bare) completes in ~2s and shows the 2 tests as
deselected; `pytest -m ""` still shows 123 passed, 1 skipped as before.

## Open questions

None — confirmed during brainstorming.

---
Note on process: given the small, low-risk, purely-config nature of this
change (3 files, no application logic), implementing directly with an
empirical before/after timing check rather than a separate formal
writing-plans pass — consistent with how the rest of this session's smaller
tasks have been handled.
