# Generic hook adapter — design spec

Date: 2026-07-18
Status: approved, pending implementation plan

## Context

Mimir currently plugs into three specific agent runtimes: Claude Code
(native hook, `from_hook`), Cline (`from_cline_hook`), and Hermes (two
plugins, `from_hermes_call` plus a `MemoryProvider` registration). Each new
runtime today means shipping a new Python mapper function inside Mimir
itself, reading that runtime's docs, and guessing at field names (see the
`ponytail:` comments on `from_cline_hook` and `from_hermes_call` — both are
still unverified against a live payload).

This is sub-project #2 of the "fast, efficient, plug into anything"
initiative (#1, auto-consolidate, shipped in commit `2cba3fc`). The goal
here is narrower than "integrate with every tool": let a user wire up a
**new** tool themselves, with zero Mimir code changes, as long as that
tool's hook mechanism can run an arbitrary shell command with a JSON
payload — which covers the common case (Claude Code, Cline, and most
CLI-hook-based tools all work this way already).

## Scope

**In scope:**
- A declarative, per-tool JSON config that maps a tool's own hook payload
  shape onto Mimir's `Episode` fields via dotted-path lookups.
- Wiring that config into the existing `mimir-hook` / `hook_main` CLI
  entrypoint via a `--config PATH` flag or `MIMIR_HOOK_CONFIG` env var.
- Docs with one full worked example for a hypothetical new tool.

**Out of scope (explicit, not silently deferred):**
- Array/list indexing in dotted paths (e.g. `items.0.status`) — nested
  dict traversal only. A tool whose relevant fields live inside a list
  isn't covered; document the limitation.
- Multi-field or boolean (AND/OR) fail logic — one `outcome_path` plus one
  `fail_values` list only. A tool that needs to combine two fields to
  determine pass/fail isn't covered by config alone.
- In-process / library-style integrations (Hermes' `MemoryProvider`
  pattern). Those need per-tool glue code no matter what — a declarative
  config can't stand in for a foreign plugin API. Existing Hermes plugins
  are untouched by this project.
- Retrofitting Cline's existing bespoke mapper onto the new config system.
  It already works; leave it alone.

## Design

### Config format

Plain JSON (no new dependency — the codebase already uses `json`
everywhere and deliberately avoids adding a YAML/TOML dependency for it).
One file per tool the user wires up, typically under `~/.mimir/hooks/`.

```json
{
  "action_path": "tool_name",
  "context_path": "input",
  "consequence_path": "result",
  "session_id_path": "session.id",
  "task_id_path": "task.id",
  "outcome_path": "result.status",
  "fail_values": ["error", "failed", false]
}
```

- Each `*_path` is a dotted path into the tool's raw JSON payload
  (`"result.status"` → `payload["result"]["status"]`). Absent from the
  config, or unreachable in the actual payload (missing key, or a
  non-dict value in the middle of the path) → resolves to `None`.
- `action_path` / `session_id_path` / `task_id_path` default to `""` when
  unresolved, matching how `from_hook` and `from_cline_hook` already
  default missing fields.
- `context_path` / `consequence_path` are JSON-serialized
  (`json.dumps(value, default=str)`) whatever they resolve to, `None`
  included — same as the existing mappers.
- `outcome_path` resolves to a raw value; if that value is a member of
  `fail_values` (default `[]`), the episode is `OUTCOME_FAIL`, otherwise
  `OUTCOME_PASS`. An unresolved `outcome_path` (missing from config, or
  not found in the payload) resolves to `None`, which is `OUTCOME_PASS`
  unless the user explicitly put `null`/`None` in `fail_values`.

No dual-path guessing (the way `from_cline_hook` defensively checks both
`tool_input` and `toolInput`) — the user who writes the config knows their
own tool's exact field names, so a single path per field is enough.

### Components

**`mimir/capture.py`** — add two functions, alongside the existing
`from_hook` / `from_cline_hook` / `from_hermes_call`:

```python
def _resolve_path(data: dict, path: str):
    """Dotted-path lookup into a nested dict. None if any segment is
    missing or the node at that point isn't a dict."""
    if not path:
        return None
    node = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def from_config_hook(config: dict) -> Callable[[dict], Episode]:
    """Build a mapper for an arbitrary tool's hook payload from a
    declarative field-mapping config (see docs/integrations/generic.md).
    Lets a user plug in a new tool by writing JSON, not Python."""
    fail_values = config.get("fail_values", [])

    def mapper(event: dict) -> Episode:
        outcome_value = _resolve_path(event, config.get("outcome_path", ""))
        failed = outcome_value in fail_values
        return Episode(
            action=_resolve_path(event, config.get("action_path", "")) or "",
            context=json.dumps(
                _resolve_path(event, config.get("context_path", "")), default=str),
            consequence=json.dumps(
                _resolve_path(event, config.get("consequence_path", "")), default=str),
            outcome_score=OUTCOME_FAIL if failed else OUTCOME_PASS,
            session_id=_resolve_path(event, config.get("session_id_path", "")) or "",
            task_id=_resolve_path(event, config.get("task_id_path", "")) or "",
        )
    return mapper
```

`from_config_hook` returns the same `Callable[[dict], Episode]` shape
`run_hook`'s `mapper` parameter already expects — it plugs in with no
changes to `run_hook` or `capture()`.

**`mimir/cli.py`** — `hook_main` gains config loading:

```python
def hook_main(argv: Optional[list] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    mapper = from_hook
    config_path = _extract_flag_value(args, "--config") or os.environ.get("MIMIR_HOOK_CONFIG")
    if config_path:
        try:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            mapper = from_config_hook(config)
        except Exception:
            log.exception(
                "mimir hook: failed to load --config %s, skipping this capture", config_path)
            auto_consolidate.maybe_trigger(_log_path())
            return 0
    rc = run_hook(sys.stdin.read(), log_path=_log_path(), mapper=mapper)
    auto_consolidate.maybe_trigger(_log_path())
    return rc
```

`_extract_flag_value(args, flag)` is a small stdlib-only helper (loop over
`args`, return the token after `flag` if present) — no `argparse` needed
for one optional flag, consistent with the rest of `cli.py`'s hand-rolled
dispatch.

No new console script, no `pyproject.toml` change — any tool that can run
`mimir hook --config <path>` (or set `MIMIR_HOOK_CONFIG` once in its own
env and run `mimir hook`) is wired in.

### Data flow

Identical to every existing hook path past the mapper: tool's own hook
mechanism → shells out to `mimir hook --config ~/.mimir/hooks/<tool>.json`
→ JSON payload on stdin → `from_config_hook`'s closure resolves the
configured paths → `Episode` → `capture()` → same JSONL log, same
auto-consolidate failure counter, same everything downstream. Nothing
after the mapper changes; this is purely a new way to produce the
`Callable[[dict], Episode]` `run_hook` already accepts.

### Error handling

- Missing or malformed `--config`/`MIMIR_HOOK_CONFIG` file: log the
  exception loudly (stdlib `logging`, never print — matches the rest of
  the codebase's never-raise-into-agent-loop contract) and **skip the
  capture** for that call, rather than falling back to the Claude Code
  mapper (`from_hook`). Falling back would silently map a foreign
  payload through the wrong schema, producing a garbage episode that
  still counts toward the auto-consolidate failure threshold — wasting a
  future consolidate cycle (an LLM call) on noise. Skipping is the
  zero-token-safe choice.
- Still returns `0` unconditionally in every branch — the fast-path
  contract (never block the agent loop) is unchanged.
- A well-formed config with a bad path (typo, wrong nesting) is not an
  error case — it resolves to `None`/default-empty per field, same as an
  `Episode` built from a payload missing those fields today. No special
  handling needed; this is the existing default-on-missing behavior.

### Testing plan

- `_resolve_path`: nested hit, missing key, non-dict intermediate value,
  empty path string.
- `from_config_hook`: full config produces correct `Episode`; missing
  `*_path` entries fall back to the documented defaults; `fail_values`
  matches a string status and a boolean `False`; unresolved
  `outcome_path` defaults to `OUTCOME_PASS`.
- `hook_main`: end-to-end with `--config` pointing at a temp file; same
  via `MIMIR_HOOK_CONFIG`; a malformed/missing config file results in no
  episode written to the log (capture skipped) and `rc == 0`; a
  regression test confirming no flag/env var still uses `from_hook`
  unchanged.

### Docs

Add `docs/integrations/generic.md`: one full worked example wiring a
hypothetical tool ("Foo") whose hook payload looks like
`{"tool": "foo.run", "input": {...}, "output": {"status": "error", "message": "..."}}`
via a shell hook command `mimir hook --config ~/.mimir/hooks/foo.json`,
plus the config file that maps it. README's existing "Also on Cline and
Hermes" section gets one added sentence pointing here for "anything
else."
