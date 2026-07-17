# Auto-consolidate: silent, on-by-default lesson consolidation

Date: 2026-07-17
Status: approved, ready for implementation planning

## Context

First of a three-part initiative ("make Mimir fast, plug into anything, and
require zero thought to use") — this is part 1, zero-thought simplicity.
Parts 2 (broader non-MCP integrations) and 3 (bench/live.py cost/latency,
the still-open item from the 2026-07-16 efficiency initiative) follow as
separate specs.

Today, `mimir consolidate` is a deliberate manual step by design (see
README: "It's a deliberate step, not a black box") — capture is passive via
hooks, but turning captured failures into lessons requires the user to
remember to run a command. That's the last piece of friction standing
between "install once" and "never think about it again."

`SessionEnd` is already a registered hook event (`HOOK_EVENTS =
("PostToolUse", "SessionEnd")` in `mimir/cli.py`), wired to the same
`mimir-hook` command as `PostToolUse`. Per `run_hook`'s own docstring,
`SessionEnd` today is a no-op (empty/non-tool-call stdin). This means the
work below is purely additive — nothing existing changes behavior.

## Scope

In scope:
1. Auto-trigger `mimir consolidate` in the background, gated by a
   threshold-of-new-failures + cooldown-time policy, on by default.
2. An incrementally-maintained failure counter so the gating check stays
   O(1) regardless of log size (see "Efficiency correction" below —
   caught during design review before any code was written).
3. Escape hatch (`MIMIR_AUTO_CONSOLIDATE=0`) and tunable thresholds via env
   vars, matching the existing `MIMIR_EPISODE_LOG` / `MIMIR_EMBED_MODEL`
   convention.
4. Wiring into both `hook_main` (Claude Code) and `hook_main_cline`.

Out of scope (considered, rejected):
- Detecting `SessionEnd` specifically via payload inspection to trigger
  only at session close — fragile (depends on exact Claude Code hook JSON
  shape for that event) for no real benefit over checking unconditionally,
  since the check itself is O(1) after the efficiency correction below.
- Hermes integration — `hermes_memory.py` captures via an in-process
  Python callback, not a subprocess-friendly hook invocation. Belongs in
  part 2 (plug into anything), not here.
- File locking on the failure counter increment — theoretically racy under
  concurrent hook invocations, but worst case is an undercounted increment
  that delays a trigger by one cycle, not a correctness failure. Not worth
  the complexity for a local, single-user file (ponytail: revisit if
  concurrent hook firing is ever observed in practice).
- Notifying the user in-session that auto-consolidate ran — output goes to
  a log file, not stdout/stderr, to keep the hook silent. A "N lessons
  learned since last time" banner is a plausible future nicety, not needed
  for this increment.

## Design

### Efficiency correction (caught during design review)

The first pass of this design proposed computing "new failures since last
run" by reusing `_episodes_from_log()` (already used by manual
`consolidate`) inside the due-check — i.e., a full read + JSON-parse of
every line in `episodes.jsonl` on every gating check. Since the gating
check runs on every `PostToolUse` call (the hottest path in the system),
that cost would grow with total historical log size and run far more often
than the O(1) fast-path contract `mimir/capture.py` promises. Rejected in
favor of an incrementally-maintained counter (below) — the gating check
becomes a read of two small integers out of an already-small state file,
with no dependency on log size at all.

### Components

**State file** `~/.mimir/auto_consolidate_state.json`:
```json
{"last_run_ts": "2026-07-17T10:00:00+00:00",
 "failure_count_total": 17,
 "failure_count_at_last_run": 12}
```
- `failure_count_total` — bumped by exactly 1 every time `capture()`
  appends a FAIL episode. Absent/0 initially.
- `failure_count_at_last_run` — snapshot of `failure_count_total` taken by
  the worker when a consolidate run completes.
- `last_run_ts` — absent means "never run" (cooldown auto-satisfied).

**`mimir/auto_consolidate.py`** (new module):
- `bump_failure_count(state_path=DEFAULT_STATE) -> None` — read-modify-write
  the state file, incrementing `failure_count_total`. Creates the file/dir
  on first use. Internally swallows all exceptions and logs (never raises),
  so `capture()` can call it unconditionally without extra try/except
  nesting around it.
- `is_due(state_path, *, threshold, cooldown_hours) -> bool` — reads the
  state file once; due iff
  `failure_count_total - failure_count_at_last_run >= threshold` AND
  (`last_run_ts` absent OR `now - last_run_ts >= cooldown_hours`).
- `maybe_trigger(log_path, state_path=DEFAULT_STATE) -> None` — reads
  `MIMIR_AUTO_CONSOLIDATE` (default enabled; `"0"` disables),
  `MIMIR_AUTO_CONSOLIDATE_THRESHOLD` (default `5`),
  `MIMIR_AUTO_CONSOLIDATE_COOLDOWN_HOURS` (default `4`). Calls `is_due`; if
  due, atomically creates `~/.mimir/auto_consolidate.lock`
  (`O_CREAT|O_EXCL`) — if it exists and is fresher than 2 hours, skip (a
  run is already in flight); if staler, reclaim it. On successful lock
  acquisition, spawns a detached subprocess (`start_new_session=True`
  POSIX / `DETACHED_PROCESS` Windows) running
  `[sys.executable, "-m", "mimir.cli", "_auto-consolidate-worker"]` with
  stdout/stderr redirected (append) to `~/.mimir/auto_consolidate.log`, and
  returns immediately. Entire function body wrapped in try/except — never
  raises, matching `run_hook`'s existing contract.

**`mimir/capture.py`**: `capture()` calls
`auto_consolidate.bump_failure_count()` immediately after a successful
append, only when `episode.outcome_score == OUTCOME_FAIL`.

**`mimir/cli.py`**:
- New hidden subcommand `_auto-consolidate-worker` (not listed in help
  text — it's an implementation detail the hook spawns, not a
  user-facing command): calls the existing `consolidate_main()` unchanged,
  then in a `finally` block sets `failure_count_at_last_run` to the
  current `failure_count_total` and `last_run_ts` to now, and removes the
  lock file — so state/lock bookkeeping happens whether or not
  `consolidate_main` succeeded.
- `hook_main()` and `hook_main_cline()` both call
  `auto_consolidate.maybe_trigger(_log_path())` after `run_hook(...)`
  returns, before their own `return 0`.

### Data flow

```
capture (every tool call)         gate (every hook call)        worker (spawned, rare)
──────────────────────────        ───────────────────────       ───────────────────────
append EPISODE to episodes.jsonl  read state file (2 ints)   →  consolidate_main()
  if FAIL: bump_failure_count()   due? spawn detached worker     (existing logic, unchanged)
  (O(1), same file every time)    else: no-op, return 0          on completion: update state,
                                                                  release lock
```

### Error handling

Same philosophy as the existing fast path: swallow, log loudly, never
propagate into the agent loop. `bump_failure_count` and `maybe_trigger`
both wrap their entire bodies in try/except. A crashed worker's traceback
lands in `auto_consolidate.log`, not the console. The 2-hour lock-staleness
ceiling prevents a crashed worker from permanently blocking future
auto-triggers.

## Testing

New `tests/test_auto_consolidate.py`, matching the repo's existing style
(plain pytest, injected fakes, `tmp_path`-isolated paths, no real
subprocess spawned — `Popen` monkeypatched to a recording fake):

- `bump_failure_count`: increments `failure_count_total` from absent/0;
  repeated calls accumulate; never raises even if the state path is
  unwritable (e.g. patched to a directory that doesn't exist and can't be
  created).
- `is_due`: false below threshold; false when threshold met but cooldown
  not elapsed; true when both conditions hold; true on first-ever check
  with no state file (cooldown auto-satisfied) once threshold met.
- `maybe_trigger`: spawns (via the fake `Popen`) when due and enabled;
  never spawns when `MIMIR_AUTO_CONSOLIDATE=0` even if due; skips when a
  fresh lock file exists; reclaims and spawns when the lock file is older
  than the staleness ceiling.
- `_auto-consolidate-worker` (or the underlying function it calls):
  after running with injected judge/probe fakes (same pattern as existing
  `test_cli.py` consolidate tests, zero real LLM calls), state file's
  `failure_count_at_last_run` and `last_run_ts` are updated and the lock
  file no longer exists — including when the injected judge/probe raises,
  to confirm the `finally` cleanup still runs.

## Open questions

None — trigger policy (threshold+cooldown), defaults (5 episodes / 4
hours), on-by-default behavior, and the efficiency correction were all
confirmed during brainstorming (see conversation log 2026-07-17).
