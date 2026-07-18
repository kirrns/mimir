# Generic Hook Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user plug a brand-new agent tool into Mimir's capture pipeline by writing a small declarative JSON config, with zero Mimir code changes, as long as that tool's hook mechanism can run a shell command with a JSON payload on stdin.

**Architecture:** A new `from_config_hook(config: dict) -> Callable[[dict], Episode]` factory in `mimir/capture.py` builds a mapper from dotted-path field lookups, matching the exact `Callable[[dict], Episode]` contract `run_hook` already accepts from `from_cline_hook`/`from_hook`. `mimir/cli.py`'s existing `hook_main` (the `mimir-hook` / `mimir hook` entrypoint) gains a `--config PATH` flag / `MIMIR_HOOK_CONFIG` env var that loads this config and swaps in the generic mapper. No new console script, no new dependency.

**Tech Stack:** Python stdlib only (`json`, `logging`) — same as the rest of `mimir/capture.py` and `mimir/cli.py`.

## Global Constraints

- Config format is plain JSON. No new dependency (no YAML/TOML parser).
- `from_config_hook(config: dict) -> Callable[[dict], Episode]` — same mapper contract as `from_hook`/`from_cline_hook`/`from_hermes_call`.
- Dotted-path lookup (`"result.status"`) does nested-dict traversal only — no list/array indexing (e.g. `items.0.status` is explicitly out of scope).
- Outcome logic is exactly one `outcome_path` plus one `fail_values` list — no combining multiple fields. A resolved value that is a member of `fail_values` is `OUTCOME_FAIL`; anything else (including an unresolved/`None` path) is `OUTCOME_PASS`.
- `action_path` / `session_id_path` / `task_id_path` default to `""` when the config key is absent or the path is unreachable in the payload (matches how `from_hook`/`from_cline_hook` already default missing fields).
- `context_path` / `consequence_path` are always JSON-serialized (`json.dumps(value, default=str)`), including when the resolved value is `None` — same as the existing mappers.
- On a missing/malformed `--config`/`MIMIR_HOOK_CONFIG` file: log the exception loudly via stdlib `logging` (never `print`), **skip the capture entirely** (do not fall back to `from_hook`), and still return `0`. The never-block-the-agent-loop contract holds in every branch.
- No new `pyproject.toml` entry point / console script. Wiring goes through the existing `hook_main` (`mimir-hook` and `mimir hook`).
- Out of scope, not touched by this plan: Hermes' in-process `MemoryProvider` integration, Cline's existing bespoke mapper.

---

### Task 1: `from_config_hook` mapper in `mimir/capture.py`

**Files:**
- Modify: `mimir/capture.py` (insert two new functions after `from_hermes_call`, which currently ends at line 86, and before `def capture(` at line 89)
- Test: `tests/test_capture.py` (append new tests at end of file, after line 165)

**Interfaces:**
- Consumes: `Episode` (from `mimir.models`), `OUTCOME_FAIL`/`OUTCOME_PASS` constants, `json` (all already imported in `capture.py`), `Callable` (already imported from `typing`).
- Produces: `_resolve_path(data: dict, path: str)` — returns the value at a dotted path, or `None` if the path is empty or any segment is missing/non-dict. `from_config_hook(config: dict) -> Callable[[dict], Episode]` — Task 2 imports and calls this directly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture.py`:

```python
# --- Generic hook adapter: declarative config-driven mapper for arbitrary tools ---

from mimir.capture import _resolve_path, from_config_hook


def test_resolve_path_finds_nested_value():
    assert _resolve_path({"result": {"status": "error"}}, "result.status") == "error"


def test_resolve_path_returns_none_for_missing_key():
    assert _resolve_path({"result": {}}, "result.status") is None


def test_resolve_path_returns_none_through_non_dict_intermediate():
    assert _resolve_path({"result": "not a dict"}, "result.status") is None


def test_resolve_path_returns_none_for_empty_path():
    assert _resolve_path({"result": {"status": "error"}}, "") is None


def test_from_config_hook_maps_full_config():
    config = {
        "action_path": "tool_name",
        "context_path": "input",
        "consequence_path": "result",
        "session_id_path": "session.id",
        "task_id_path": "task.id",
        "outcome_path": "result.status",
        "fail_values": ["error"],
    }
    mapper = from_config_hook(config)
    event = {
        "tool_name": "foo.run",
        "input": {"cmd": "build"},
        "result": {"status": "error", "message": "boom"},
        "session": {"id": "s1"},
        "task": {"id": "t1"},
    }
    ep = mapper(event)
    assert ep.action == "foo.run"
    assert json.loads(ep.context) == {"cmd": "build"}
    assert json.loads(ep.consequence) == {"status": "error", "message": "boom"}
    assert ep.session_id == "s1"
    assert ep.task_id == "t1"
    assert ep.outcome_score == OUTCOME_FAIL


def test_from_config_hook_defaults_missing_paths_to_empty_string():
    mapper = from_config_hook({})
    ep = mapper({"anything": "here"})
    assert ep.action == ""
    assert ep.session_id == ""
    assert ep.task_id == ""
    assert ep.outcome_score == OUTCOME_PASS  # unresolved outcome_path -> None -> not in [] -> PASS


def test_from_config_hook_fail_values_matches_boolean_false():
    config = {"outcome_path": "ok", "fail_values": [False]}
    mapper = from_config_hook(config)
    assert mapper({"ok": False}).outcome_score == OUTCOME_FAIL
    assert mapper({"ok": True}).outcome_score == OUTCOME_PASS


def test_from_config_hook_fail_values_matches_string_status():
    config = {"outcome_path": "status", "fail_values": ["error", "failed"]}
    mapper = from_config_hook(config)
    assert mapper({"status": "failed"}).outcome_score == OUTCOME_FAIL
    assert mapper({"status": "ok"}).outcome_score == OUTCOME_PASS
```

`json`, `OUTCOME_FAIL`, `OUTCOME_PASS` are already imported at the top of `tests/test_capture.py` — no new imports needed there beyond the `from mimir.capture import _resolve_path, from_config_hook` line above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_capture.py -v -k "resolve_path or from_config_hook"`
Expected: FAIL — `ImportError: cannot import name '_resolve_path' from 'mimir.capture'`

- [ ] **Step 3: Write the minimal implementation**

Insert into `mimir/capture.py`, after `from_hermes_call` (line 86) and before `def capture(` (line 89):

```python
def _resolve_path(data: dict, path: str):
    """Dotted-path lookup into a nested dict (e.g. "result.status"). None if the path
    is empty, or any segment along the way is missing or not itself a dict."""
    if not path:
        return None
    node = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def from_config_hook(config: dict) -> Callable[[dict], Episode]:
    """Build a mapper for an arbitrary tool's hook payload from a declarative field-mapping
    config (see docs/integrations/generic.md). Lets a user plug in a new tool by writing
    JSON, not Python -- same Callable[[dict], Episode] contract as from_cline_hook etc."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_capture.py -v`
Expected: all tests in the file PASS, including the 8 new ones.

- [ ] **Step 5: Commit**

```bash
git add mimir/capture.py tests/test_capture.py
git commit -m "feat: add declarative config-driven hook mapper (from_config_hook)"
```

---

### Task 2: `--config` / `MIMIR_HOOK_CONFIG` wiring in `mimir/cli.py`

**Files:**
- Modify: `mimir/cli.py` — module docstring (lines 1-22), imports (line 33), constants (after line 44), `hook_main` (lines 243-248)
- Test: `tests/test_cli.py` (append new tests after line 353, i.e. after `test_hook_main_cline_calls_auto_consolidate_maybe_trigger`, before the auto-consolidate worker tests)

**Interfaces:**
- Consumes: `from_config_hook` from Task 1 (`mimir.capture`), signature `from_config_hook(config: dict) -> Callable[[dict], Episode]`.
- Produces: `_extract_flag_value(args: list, flag: str) -> Optional[str]` and the updated `hook_main(argv: Optional[list] = None) -> int` behavior — no other task consumes these directly, this is the user-facing surface.

- [ ] **Step 1: Write the failing tests for `_extract_flag_value`**

Append to `tests/test_cli.py`, after line 353 (after `test_hook_main_cline_calls_auto_consolidate_maybe_trigger`):

```python
def test_extract_flag_value_returns_following_token():
    import mimir.cli as cli

    assert cli._extract_flag_value(["--config", "path.json"], "--config") == "path.json"


def test_extract_flag_value_returns_none_when_absent():
    import mimir.cli as cli

    assert cli._extract_flag_value([], "--config") is None


def test_extract_flag_value_returns_none_when_flag_is_last_token():
    import mimir.cli as cli

    assert cli._extract_flag_value(["--config"], "--config") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v -k extract_flag_value`
Expected: FAIL — `AttributeError: module 'mimir.cli' has no attribute '_extract_flag_value'`

- [ ] **Step 3: Implement `_extract_flag_value` and supporting cli.py infrastructure**

In `mimir/cli.py`, add `import logging` to the import block (after `import json`, before `import os`, keeping the existing alphabetical-ish grouping):

```python
import json
import logging
import os
import sys
```

Add a module-level logger, right after the imports and before `DEFAULT_HOME` (line 36) — matches the pattern already used in `mimir/capture.py` and `mimir/auto_consolidate.py`:

```python
log = logging.getLogger("mimir.cli")
```

Add a new env var constant next to the existing `CITATION_KEY_ENV`/`EMBED_MODEL_ENV` (after line 44):

```python
HOOK_CONFIG_ENV = "MIMIR_HOOK_CONFIG"       # generic adapter: path to a field-mapping config
```

Add the helper function immediately before `hook_main` (before line 243):

```python
def _extract_flag_value(args: list, flag: str) -> Optional[str]:
    """Return the token following `flag` in args, or None if the flag isn't present
    (or has nothing after it). No argparse needed for one optional flag."""
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v -k extract_flag_value`
Expected: all 3 PASS.

- [ ] **Step 5: Write the failing tests for `hook_main` config wiring**

Append to `tests/test_cli.py`, after the `_extract_flag_value` tests just added:

```python
def test_hook_main_uses_config_mapper_when_config_flag_given(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    config_path = tmp_path / "foo.json"
    config_path.write_text(json.dumps({
        "action_path": "tool_name",
        "outcome_path": "result.status",
        "fail_values": ["error"],
    }), encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin",
                        io.StringIO(json.dumps({"tool_name": "foo.run",
                                                "result": {"status": "error"}})))
    rc = cli.hook_main(["--config", str(config_path)])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "foo.run"
    assert row["outcome_score"] == cli.OUTCOME_FAIL


def test_hook_main_uses_config_mapper_from_env_var(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    config_path = tmp_path / "foo.json"
    config_path.write_text(json.dumps({"action_path": "tool_name"}), encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setenv("MIMIR_HOOK_CONFIG", str(config_path))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"tool_name": "foo.run"})))
    rc = cli.hook_main([])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "foo.run"


def test_hook_main_skips_capture_on_malformed_config(tmp_path, monkeypatch, caplog):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("not json{", encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"tool_name": "foo.run"})))
    with caplog.at_level(logging.ERROR):
        rc = cli.hook_main(["--config", str(bad_config)])
    assert rc == 0
    assert not log.exists()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_hook_main_without_config_still_uses_claude_code_mapper(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    event = {"tool_name": "Bash", "is_error": True, "session_id": "s1"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    rc = cli.hook_main([])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "Bash"
    assert row["outcome_score"] == cli.OUTCOME_FAIL
```

`json` and `io` are already imported in `tests/test_cli.py` (`io` at line 331, `json` at line 1). `logging` is not yet imported in the test file — add `import logging` near the top of `tests/test_cli.py` alongside the existing `import json` (line 1).

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v -k hook_main_uses_config or hook_main_skips or hook_main_without_config`
Expected: FAIL — `TypeError: hook_main() takes from 0 to 1 positional arguments` is NOT the failure (argv already existed); the real failure is the mapper never switching, e.g. `AssertionError` on `row["action"] == "foo.run"` because it's still empty (the Claude Code mapper doesn't know `tool_name` maps the same way once a config's fields diverge) — for `test_hook_main_uses_config_mapper_from_env_var` specifically, confirm it fails before Step 7's implementation exists.

- [ ] **Step 7: Implement the `hook_main` config wiring**

In `mimir/cli.py`, update the capture import (line 33). `hook_main` currently relies on `run_hook`'s own default mapper rather than naming `from_hook` directly — since the new code below needs to name `from_hook` explicitly as the fallback, add it to the import alongside `from_config_hook`:

```python
from mimir.capture import OUTCOME_FAIL, from_cline_hook, from_config_hook, from_hook, run_hook
```

Replace `hook_main` (lines 243-248):

```python
def hook_main(argv: Optional[list] = None) -> int:
    """`mimir-hook` — what a Claude Code hook invokes. Never raises, always 0.

    Pass --config PATH (or set MIMIR_HOOK_CONFIG) to capture from any tool whose hook
    payload isn't Claude Code's shape -- see docs/integrations/generic.md.
    """
    _ensure_utf8_stdio()
    args = argv if argv is not None else sys.argv[1:]
    mapper = from_hook
    config_path = _extract_flag_value(args, "--config") or os.environ.get(HOOK_CONFIG_ENV)
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

Also update the module docstring (after line 16, the `mimir install-hook --cline` line, before line 17's blank line):

```python
    mimir install-hook --cline   # write the PostToolUse hook script Cline picks up
                                 #   (capture only; ~/Documents/Cline/Rules/Hooks/)
    mimir hook --config PATH     # capture from any tool via a declarative field-mapping
                                 #   config (see docs/integrations/generic.md); or set
                                 #   MIMIR_HOOK_CONFIG instead of --config
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: all tests PASS, including the 4 new `hook_main` tests, the 3 `_extract_flag_value` tests, and no regressions in the pre-existing `test_hook_main_calls_auto_consolidate_maybe_trigger` / `test_hook_main_cline_calls_auto_consolidate_maybe_trigger`.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest`
Expected: all tests PASS (baseline was 154 passed, 2 deselected before Task 1; Task 1 added 8, Task 2 added 7 — expect 169 passed, 2 deselected).

- [ ] **Step 10: Commit**

```bash
git add mimir/cli.py tests/test_cli.py
git commit -m "feat: wire --config/MIMIR_HOOK_CONFIG into hook_main for the generic adapter"
```

---

### Task 3: Docs — worked example and README pointer

**Files:**
- Create: `docs/integrations/generic.md`
- Modify: `README.md` (the "Also on Cline and Hermes" paragraph, originally at lines 145-151 as of commit `2cba3fc` — confirm current line numbers with `grep -n "Also on Cline and Hermes" README.md` before editing, since Tasks 1-2 don't touch README but other commits between now and execution might)

**Interfaces:**
- Consumes: nothing (docs only, no code dependency on Tasks 1-2's internals beyond their public config schema, which this doc explains to end users).
- Produces: nothing consumed by other tasks — this is the plan's terminal, user-facing deliverable.

- [ ] **Step 1: Write `docs/integrations/generic.md`**

Create `docs/integrations/generic.md`:

```markdown
# Plugging in a new tool (generic hook adapter)

Mimir ships bespoke mappers for Claude Code, Cline, and Hermes. For
anything else, if your tool's hook mechanism can run an arbitrary shell
command with a JSON payload on stdin — which covers most "hook"
mechanisms — you can wire it up yourself with a small JSON config, no
Mimir code changes needed.

## How it works

1. Write a config file describing where the fields Mimir needs live in
   your tool's own hook payload.
2. Point `mimir hook` at it, either with `--config <path>` or by setting
   `MIMIR_HOOK_CONFIG=<path>` once in your tool's own hook environment.
3. Wire your tool's hook to run `mimir hook --config <path>` (or just
   `mimir hook` if you used the env var), with the payload piped to stdin.

## Config format

```json
{
  "action_path": "tool",
  "context_path": "input",
  "consequence_path": "output",
  "session_id_path": "session.id",
  "task_id_path": "task.id",
  "outcome_path": "output.status",
  "fail_values": ["error", "failed", false]
}
```

Each `*_path` is a dotted path into your tool's JSON payload
(`"output.status"` -> `payload["output"]["status"]`). Missing or
unreachable paths resolve to an empty string for
`action`/`session_id`/`task_id`. The outcome is `FAIL` when the value at
`outcome_path` is a member of `fail_values`; anything else (including an
unresolved path) is `PASS`.

**Limitations:** dict traversal only (no `items.0.status` list indexing);
one `outcome_path` plus one `fail_values` list (no combining multiple
fields). If your tool needs either, write a Python mapper instead —
`from_cline_hook` in `mimir/capture.py` is a template.

## Worked example: a tool called "Foo"

Say Foo's hook payload looks like this:

```json
{
  "tool": "foo.run",
  "input": {"target": "build"},
  "output": {"status": "error", "message": "build failed: missing dependency"}
}
```

The config, saved as `~/.mimir/hooks/foo.json`:

```json
{
  "action_path": "tool",
  "context_path": "input",
  "consequence_path": "output",
  "outcome_path": "output.status",
  "fail_values": ["error"]
}
```

Foo's own hook configuration then runs:

```
mimir hook --config ~/.mimir/hooks/foo.json
```

(or set `MIMIR_HOOK_CONFIG=~/.mimir/hooks/foo.json` in Foo's hook
environment and just run `mimir hook`.)

Every hook firing appends one EPISODE to `~/.mimir/episodes.jsonl`,
feeding the same auto-consolidate pipeline as Claude Code, Cline, and
Hermes.
```

- [ ] **Step 2: Update README's integrations paragraph**

Run `grep -n "Also on Cline and Hermes" README.md` to find the current line number, then add one sentence to the end of that paragraph (after "...something doesn't map correctly." and before the next `---`):

```markdown
**Anything else:** if your tool can run a shell command with JSON on
stdin, `mimir hook --config <path>` plugs it in without touching Mimir's
code — see [docs/integrations/generic.md](docs/integrations/generic.md).
```

- [ ] **Step 3: Self-verify against the plan's exact text**

Run: `python -m pytest` (docs-only change; confirm no regressions — expected 169 passed, 2 deselected, same as after Task 2).

Read back both `docs/integrations/generic.md` and the edited `README.md` paragraph to confirm they match this step's text exactly (no typos in the JSON examples — paste them into a scratch `python -c "import json,sys; json.loads(sys.stdin.read())"` check if unsure).

- [ ] **Step 4: Commit**

```bash
git add docs/integrations/generic.md README.md
git commit -m "docs: add generic hook adapter worked example and README pointer"
```
