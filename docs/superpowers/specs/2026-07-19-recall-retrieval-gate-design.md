# Retrieval gate for `mimir.recall` — design

## Origin

Comparing Mimir against `github.com/ShenSeanChen/waku-agent` (a local-first
agent reference project) surfaced one real gap: waku-agent has an explicit
per-turn "Retrieval Gate" deciding whether memory lookup is worth doing
before spending tokens on it. Mimir's `recall()` (`mimir/mcp_server.py`)
already gates *what* comes back (tau confidence floor, contradiction
exclusion, an `uncertain` flag for thin support) but nothing gates *whether
to call it at all* — that decision is left entirely to the calling agent
(Claude Code / Cline / Hermes) deciding when to invoke the `mimir.recall`
MCP tool.

## What cost this actually saves

`recall()` itself is already cheap: pure Python, no LLM call, a local
lexical/semantic scan over lessons already in the store. There is no
expensive computation inside Mimir to gate against. The real cost lives on
the caller's side — the MCP tool-call round-trip itself (a wasted turn in
the calling agent's context when the answer was always going to be empty)
and, secondarily, token cost if recalled lessons get injected into a
prompt.

`recall()` already reports "this wasn't worth it" *after* running, via
`uncertain=True` / an empty `lessons` list. What's missing is a **pre-hoc**
signal cheap enough to skip the tool call entirely — most concretely, right
after install, before `mimir consolidate` has ever run, when the store is
guaranteed empty and every `mimir.recall` call is a wasted round-trip by
construction.

This design scopes exactly that case: skip the round-trip when the store
has zero active lessons. It does not attempt query-aware pre-filtering
(deciding relevance before calling recall) — that would duplicate the same
computation `recall()` already does internally to produce `uncertain`, so
there's nothing to save there. That remains recall()'s job.

## Design

### Where the check lives

To actually skip the round-trip — not just make the handler return fast —
the calling agent's LLM must never see `mimir.recall` as an available tool
in the first place. That means the check has to run when `build_tools()`
runs, not inside the `mimir.recall` handler. `build_tools()` is called once
per MCP server process, i.e. once per session.

A new pure function, next to `recall()` in `mimir/mcp_server.py`:

```python
def _has_active_lessons(store) -> bool:
    """Cheap pre-check for build_tools(): is there anything for mimir.recall
    to ever return? Fails open (True) on a store error -- this check must
    never be the reason a working capability silently disappears."""
    try:
        return bool(store.active())
    except Exception:
        return True
```

`build_tools()` computes this once, and conditionally omits the
`"mimir.recall"` key from its returned dict when it's `False` — the same
shape as the existing `handler=None` pattern already used for
`mimir.capture` / `mimir.consolidate` when `log_path` is missing, just
applied to the whole tool entry instead of just the handler:

```python
def build_tools(store, *, tau: float = TAU,
                log_path: Optional[Path] = None,
                consolidate_judge: Optional[Callable] = None,
                consolidate_probe: Optional[Callable] = None) -> dict[str, Tool]:
    tools = {}
    if _has_active_lessons(store):
        tools["mimir.recall"] = Tool(
            name="mimir.recall",
            description="Confidence-gated recall of active LESSONs relevant to a query (FR5).",
            input_schema=_schema({"query": {"type": "string"}}, ["query"]),
            handler=lambda query: recall(store, query, tau=tau),
        )
    tools["mimir.attribute"] = Tool(...)   # unchanged
    tools["mimir.capture"] = Tool(...)     # unchanged
    tools["mimir.consolidate"] = Tool(...) # unchanged
    tools["mimir.forget"] = Tool(...)      # unchanged
    return tools
```

(Exact diff in the implementation plan — the other four tool entries are
unchanged, just re-expressed as dict assignments instead of a single
dict-literal return so `mimir.recall` can be added conditionally.)

### Data flow / session granularity

`build_tools()` runs once at session start (MCP server process start,
triggered from Claude Code's `SessionStart`/`PreToolUse` hooks per the
module's existing docstring). So: empty store at session start →
`mimir.recall` isn't in the tool list the calling agent's LLM ever sees →
zero chance of a wasted round-trip for that whole session.

**Known tradeoff:** if lessons get added mid-session (e.g. auto-consolidate
firing later in the same session after enough failures accumulate),
`mimir.recall` stays hidden until the next session/server restart — the
tool list was already fixed when `build_tools()` ran. This is consistent
with the existing "every session after that, the agent pulls in whatever
lessons..." language in `docs/superpowers/plans/2026-07-17-auto-consolidate.md`,
which already describes recall as a session-granularity behavior, not a
live-mid-session one. No new inconsistency introduced.

### Error handling

`_has_active_lessons` fails open (returns `True`, i.e. exposes the tool) if
`store.active()` raises. A store error must surface as a store error
somewhere the caller can see it (e.g. the next real `mimir.recall` call
failing loudly), never as a silently vanished tool that leaves someone
wondering why recall stopped working.

### Testing

Three cases added to `tests/test_mcp_server.py`, using the existing
`InMemoryLessonStore` fixture pattern already in that file:

1. `build_tools()` with an empty store → `"mimir.recall" not in tools`.
2. `build_tools()` with a store seeded with ≥1 active lesson →
   `"mimir.recall" in tools` (regression guard — existing tests that assume
   `mimir.recall` is always present must keep passing, so any such test
   needs to seed a lesson first).
3. A store whose `.active()` raises → `"mimir.recall" in tools` (fail-open).

## Explicit non-goals

- No query-aware pre-filtering before calling `recall()` — duplicates work
  `recall()` already does to produce `uncertain`.
- No change to `recall()`'s own gating logic (tau, contradiction exclusion,
  `uncertain` flag) — untouched.
- No change to `mimir/hermes_memory.py` or the Claude Code hook wiring
  (`mimir/cli.py`) — this is scoped to the MCP tool surface
  (`mcp_server.py`) only, since that's the only place `build_tools()`
  decides what tools a session sees.
- No live re-check within a session (e.g. re-running `build_tools()` when
  auto-consolidate fires) — accepted tradeoff, see "Data flow" above.
