# Runtime hot paths: memoize lesson tokenization

Date: 2026-07-16
Status: implemented

## Context

Sub-project 2 of the "make Mimir efficient and fast" initiative (see
[[2026-07-16-dev-loop-test-speed-design.md]] for #1).

Measured `recall()` (mimir/mcp_server.py) and `detect_contradiction()`
(mimir/consolidate.py) before touching anything: both are O(active-lesson
count) per call because they re-tokenize every active lesson's rule text
from scratch every time (regex findall + set build). At realistic v1 scale
(hundreds of lessons) this is sub-millisecond and not worth touching. At a
5,000-lesson stress test it was ~28ms/call — noticeable but not severe.
Rejected building an inverted index (bigger diff, only pays off past
thousands of lessons, a scale this v1 system isn't expected to hit) in favor
of the minimal fix.

## Design

`@functools.lru_cache` on both modules' `_tokens(text: str) -> set[str]`
helpers. Safe because `Lesson.rule` is immutable once written (bi-temporal
supersede always creates a new `Lesson`, never mutates `.rule`), and every
call site only reads the returned set via `&`/`-` (verified via grep —
nothing calls `.add()`/`.discard()` on it, which would corrupt the shared
cached object for all future callers).

**Correction made during verification:** the first pass used
`maxsize=4096`. Re-benchmarking at the same 5,000-lesson stress case showed
`recall()` got *slower* (28.7ms -> 37.4ms) — maxsize was below the working
set size, so the cache was thrashing (LRU eviction bookkeeping cost, on top
of cache misses) instead of helping. Raised to `maxsize=20000` (comfortably
above any realistic v1 store size); re-verified the win holds at n=5000:
recall 28.7ms -> 6.2ms, contradiction-scan 27.1ms -> 9.2ms (~3-4x, consistent
across n=100/1000/5000 now).

## Testing

Existing `test_mcp_server.py`/`test_consolidate.py` correctness tests pass
unchanged (123 passed, 1 skipped via `pytest -m ""`) — caching a pure
function is behavior-preserving by construction; verified via the scaling
benchmark script (ad hoc, not committed) rather than a new unit test, since
there's no new branch/loop/parser logic to guard (ponytail: YAGNI applies to
tests too).

## Open questions

None.
