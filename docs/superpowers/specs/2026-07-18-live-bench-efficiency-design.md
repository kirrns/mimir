# Live benchmark concurrency + cost/latency reporting — design spec

Date: 2026-07-18
Status: approved, pending implementation plan

## Context

This is sub-project #3 (final) of the "fast, efficient, plug into anything"
initiative — #1 (auto-consolidate) and #2 (generic hook adapter) are shipped
on `main`. This one resumes a prior, explicitly-paused item: "Live benchmark
cost/latency (`bench/live.py`'s real Claude CLI call efficiency) — NOT
STARTED... user explicitly paused here due to elevated session cost/scope
warnings from the harness, not because #4 is unimportant" (memory:
`mimir-efficiency-initiative-progress`).

`bench/live.py` is the WARM/COLD attribution benchmark that spends real
tokens against the live `claude` CLI by design (it's how Mimir proves a
lesson actually helps, not just that the plumbing works). `harness.run()`
(`bench/harness.py:95-150`) executes one task's solver call at a time in a
plain `for` loop; `run_claude` (`bench/claude_cli.py:55-78`) shells out to
`subprocess.run`, which blocks on network I/O and releases the GIL. A single
`demo_band(3)` call fires 3 tasks × 3 arms (COLD/NAIVE/WARM) × 3 repeats = 27
sequential live calls, each with up to a 180s timeout — almost all of that
wall-clock time is idle waiting, not local computation.

Separately, `harness.py` already has cost/latency metrics fully built for
the C5 spec (`net_value()`, `mean_duration_s`, `added_latency_s` —
`bench/harness.py:166-188`), but `bench/live.py`'s `demo()`/`demo_band()`
never print them — only success-rate and score-lift.

## Scope

**In scope:**
- Opt-in concurrency for `harness.run()`'s per-task solver calls.
- Passthrough wiring of that concurrency knob through `bench/live.py`'s
  `run_live`/`run_live_repeated`/`demo`/`demo_band`.
- Printing the already-existing `net_value()`/latency metrics in
  `demo()`/`demo_band()` output.

**Out of scope (explicit, not silently deferred):**
- Reducing the *number* of live calls a benchmark run makes. COLD, NAIVE,
  and WARM each need their own live call per task (different prompts, and
  live-model non-determinism is exactly what `run_live_repeated`'s `repeats`
  exists to average out) — there is no safe caching/dedup available without
  corrupting the signal being measured.
- Any change to Mimir's own runtime (`mimir/`). That was sub-projects #1-#2
  and the earlier 3-part efficiency initiative (dev-loop speed, hot-path
  caching, storage backend) — all already done. This project only touches
  `bench/`.
- Concurrency in `bench/loop.py`'s deterministic/free demo path. It already
  runs in milliseconds with no I/O wait; concurrency would add complexity
  for zero measurable benefit there. `harness.run()`'s new parameter
  defaults to `None` (today's exact sequential behavior), so `loop.py` is
  unaffected either way.
- A retry/backoff redesign in `claude_cli.py`. The existing
  `RETRIES`/exponential-backoff logic is unrelated to per-task concurrency
  and works fine as-is.

## Design

### `harness.run()` concurrency

Add `max_workers: Optional[int] = None` to `run()`'s signature
(`bench/harness.py:95-105`).

- `None` or `1`: today's exact sequential `for task in tasks` loop, byte-
  for-byte unchanged codepath. Every existing caller and test is
  unaffected.
- `> 1`: dispatch each task's solver call to a `concurrent.futures.
  ThreadPoolExecutor(max_workers=max_workers)`. `subprocess.run` blocks
  without holding the GIL, so real wall-clock concurrency is achieved even
  under Python's GIL.
- Results are collected keyed by task index and reassembled into
  `report.records` in the original `tasks` order — log output and record
  order stay reproducible regardless of which future finishes first.
- `duration_s` is measured around each individual task's solver call (as
  today), not around the whole batch — an accurate per-task number whether
  running sequentially or concurrently.
- `ClaudeLimitError` keeps today's fail-fast contract ("session limit won't
  clear in seconds — fail the whole run fast", `bench/harness.py:132-133`):
  as soon as one task's call raises it, cancel every not-yet-started
  future (`Future.cancel()` — works for queued work, not for a subprocess
  already mid-call) and re-raise once the in-flight batch settles. Any
  results from calls that happened to complete in that window are
  discarded, not appended to the report, since the whole run is aborting.
  `# ponytail:` this doesn't kill an already-running OS subprocess — the
  in-flight `claude` calls finish in the background and their output is
  ignored; acceptable because a limit error means the run is already dead,
  and killing subprocesses cleanly needs process-group signaling this
  benchmark doesn't otherwise need.

### `bench/live.py` wiring

- `run_live` and `run_live_repeated` gain a passthrough `max_workers:
  Optional[int] = None` parameter, forwarded to every `run()` call inside
  them. Default unchanged (`None` = sequential).
- `demo()` and `demo_band()` read `MIMIR_BENCH_WORKERS` from the
  environment (default `"3"` if unset, `int(...)` parsed) and pass it as
  `max_workers` to `run_live`/`run_live_repeated`. `MIMIR_BENCH_WORKERS=1`
  restores today's exact sequential behavior. No new CLI flags — this
  module has no argument parser today and doesn't need one for one env
  var, consistent with how `MIMIR_CLAUDE_BIN` already works in
  `claude_cli.py`.
- Default of 3 is a deliberately conservative starting concurrency: the
  `claude` CLI runs against the caller's own subscription session, and
  concurrent calls hit any session-level rate limit (429 /
  `ClaudeLimitError`) faster than sequential ones would. This is a
  judgment call, not a measured optimum — easy to raise via the env var
  once real usage shows headroom.

### Metrics surfacing

`demo()` and `demo_band()` (`bench/live.py:269-296`) additionally compute
and print `net_value(warm, cold)` (already imported transitively — add the
import from `bench.harness`) alongside the existing success-rate/lift line:
`added_latency_s` (WARM's extra wall-clock vs COLD) and each arm's
`mean_duration_s`. No new metric logic — this only wires up
`bench/harness.py:174-188`, which the C5 spec already built and the C5
test suite (`tests/test_harness.py:146-171`) already covers.

### Testing plan

All new behavior is testable at zero token cost via the existing
injectable-solver pattern already used throughout `tests/test_harness.py`
and `tests/test_live.py`:

- `run()` with `max_workers=None`/`1`: existing tests continue to pass
  unmodified (regression coverage that the default path is untouched).
- `run()` with `max_workers>1`: a fake solver with an artificial `time.
  sleep()` proves wall-clock time drops relative to running the same
  fake sequentially; a fake solver returns each task's own id so report
  order can be asserted against `tasks` order regardless of completion
  order; a fake solver that raises `ClaudeLimitError` on one task proves
  the run still raises `ClaudeLimitError` and does not silently swallow it
  under concurrency.
- `run_live`/`run_live_repeated`: `max_workers` is correctly forwarded to
  the underlying `run()` calls (assert via a fake `run` or by checking the
  concurrency-sensitive timing behavior with an injected sleep, following
  whichever is simpler to assert without flakiness).
- `demo()`/`demo_band()`: not unit-testable without spending tokens (real
  `claude` CLI calls) — same as today, explicitly out of the automated
  suite. `MIMIR_BENCH_WORKERS` parsing (env var -> int, default 3) is
  tested as a small pure function extracted for this purpose, so the
  env-var logic itself is covered without needing `demo()` to run live.
