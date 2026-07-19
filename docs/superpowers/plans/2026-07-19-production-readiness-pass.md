# Production-Readiness Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the stale Cognee-named semantic store, split `cli.py`'s
three tangled responsibilities into three files, add a build-trail README
to `docs/superpowers/`, and update version/maturity framing across
`pyproject.toml`/`SECURITY.md`/`CONTRIBUTING.md` — the four items from the
production-readiness audit the user approved.

**Architecture:** Task 1 renames `CogneeLessonStore`/`store_cognee.py` to
`SemanticLessonStore`/`store_semantic.py` everywhere it's referenced, then
splits `cli.py` into `store_io.py` (persistence), `hook_install.py`
(settings-file management), and a thinner `cli.py` (entry points), fixing
one real behavior-preservation trap along the way (see Task 1 Part B's
"Critical correctness note"). The rename and the split are two parts of
one task, not two tasks — the split's `store_io.py` imports the *new*
`store_semantic` module name directly, and `cli.py` briefly imports a
now-deleted module in between, so the tree isn't in a buildable,
independently-testable state until both parts are done. Tasks 2 and 3 are
independent docs/metadata edits.

**Tech Stack:** Python, stdlib only. No new dependencies.

## Global Constraints

- Every task's testing step runs the FULL suite (`pytest -q` from repo
  root), not just the touched file — Task 1's call sites (rename + split)
  are spread across multiple files a narrow test run could miss regressing.
- Baseline: **185 tests collected** as of this plan's writing (verified via
  `pytest --collect-only -q`). This count must hold exactly after every
  task — no test is added, removed, or skipped by this plan; only renamed
  or relocated.
- No `README.md` changes in this plan (out of scope per the spec — a later,
  separate spec).
- Pre-existing test file `tests/test_cli.py` is NOT split in this plan,
  even though its production counterpart is — the spec's cli.py-split
  section judged it not yet large enough to warrant it (37 tests, one
  file); it's updated in place with new import paths only.

---

## Task 1: Rename `CogneeLessonStore` → `SemanticLessonStore`, then split `cli.py`

**Files:**
- Rename: `mimir/store_cognee.py` → `mimir/store_semantic.py`
- Rename: `tests/test_store_cognee.py` → `tests/test_store_semantic.py`
- Modify: `mimir/store.py` (module docstring, lines 1-7)
- Modify: `mimir/__init__.py` (lines 7, 10)
- Modify: `mimir/mcp_server.py` (line 64, comment only)
- Modify: `mimir/hermes_memory.py` (lines 9, 39, comments only)
- Modify: `README.md` (lines 56, 164)
- Create: `mimir/store_io.py`
- Create: `mimir/hook_install.py`
- Modify: `mimir/cli.py` (full rewrite of imports and the two moved
  sections; entry-point functions stay but three call sites change)
- Modify: `tests/test_cli.py` (lines 6, 168, 178, and the
  `test_export_main_prints_digest_from_store` body)

**Interfaces:**
- Produces: `mimir.store_semantic.SemanticLessonStore` (replaces
  `mimir.store_cognee.CogneeLessonStore` everywhere). `LanceDBVectorIndex`,
  `InProcessVectorIndex`, `VectorIndex`, `hash_embed`, `fastembed_embed`,
  `DEFAULT_FASTEMBED_MODEL` — all unchanged names, same module (new path).
- Produces (for any future code, and for `mcp_server.py`/`hermes_memory.py`
  which already do `from mimir.cli import build_store` /
  `from mimir.cli import DEFAULT_LESSONS, ...` — both keep working
  unchanged since `cli.py` re-imports these names, see Part B):
  - `mimir.store_io.build_store(*, lance_url=None, lessons_path=None)`
  - `mimir.store_io.load_lessons(store, path) -> int`
  - `mimir.store_io.save_lessons(store, path) -> None`
  - `mimir.store_io.DEFAULT_HOME`, `DEFAULT_LESSONS`, `DEFAULT_LANCE`,
    `EMBED_MODEL_ENV`
  - `mimir.hook_install.install_hook(...)`, `install_cline_hook(...)`,
    `add_hook_command(...)`, `hook_block(...)`, `cline_hook_script(...)`
  - `mimir.hook_install.DEFAULT_SETTINGS`, `HOOK_COMMAND`, `HOOK_EVENTS`,
    `DEFAULT_CLINE_HOOKS_DIR`, `CLINE_HOOK_NAME`, `CLINE_HOOK_COMMAND`
- Consumes: nothing from other tasks — this is the first task, and the
  only one Tasks 2 and 3 don't depend on for anything code-related.

Two parts, done in one task because the tree isn't buildable between them
(Part B's `store_io.py` needs the *new* `store_semantic` name from Part
A; `cli.py` briefly references a module Part A deletes). Do not commit
between Part A and Part B — one test run and one commit at the very end
of Part B.

### Part A: the rename

- [ ] **Step 1: Rename the module file and update its internal content**

```bash
git mv mimir/store_cognee.py mimir/store_semantic.py
```

In `mimir/store_semantic.py`, apply these exact replacements:

Docstring (lines 1-24), change:
```python
"""C3 backend — semantic LESSON recall over a vector index.

A `CogneeLessonStore` that keeps the proven bi-temporal CRUD (inherited from
InMemoryLessonStore, so every C3 contract test still holds) and adds
`semantic_recall` over a pluggable `VectorIndex`.
```
to:
```python
"""C3 backend — semantic LESSON recall over a vector index.

A `SemanticLessonStore` that keeps the proven bi-temporal CRUD (inherited
from InMemoryLessonStore, so every C3 contract test still holds) and adds
`semantic_recall` over a pluggable `VectorIndex`.
```

The `LanceDBVectorIndex` class docstring (around line 109-116), change:
```python
class LanceDBVectorIndex:
    """LanceDB (Cognee's own vector engine) via its SYNC API — runs live here.

    Cognee stores vectors in LanceDB. Its async adapter hangs on this Py3.14 /
    Windows box, but LanceDB's sync writer works fine, so this talks to the same
    real on-disk vector database directly. Same seam as the in-process index;
    persists to `url`. Embedding is injected (unit vectors -> cosine metric).
    """
```
to:
```python
class LanceDBVectorIndex:
    """LanceDB via its SYNC API — a real on-disk vector database, talked to
    directly (no Cognee involved; see the module docstring's history note
    on why an earlier Cognee-routed adapter was removed). Same seam as the
    in-process index; persists to `url`. Embedding is injected (unit
    vectors -> cosine metric).
    """
```

The store class itself (around line 156-172), change:
```python
class CogneeLessonStore(InMemoryLessonStore):
    """Bi-temporal lesson store + semantic recall over a vector index.
```
to:
```python
class SemanticLessonStore(InMemoryLessonStore):
    """Bi-temporal lesson store + semantic recall over a vector index.
```

- [ ] **Step 2: Rename and update the test file**

```bash
git mv tests/test_store_cognee.py tests/test_store_semantic.py
```

In `tests/test_store_semantic.py`:
- Line 1 docstring: `"""C3 backend — CogneeLessonStore: bi-temporal parity + semantic recall.` → `"""C3 backend — SemanticLessonStore: bi-temporal parity + semantic recall.`
- Line 9: `from mimir.store_cognee import CogneeLessonStore, InProcessVectorIndex, hash_embed` → `from mimir.store_semantic import SemanticLessonStore, InProcessVectorIndex, hash_embed`
- Every other occurrence of `CogneeLessonStore(` → `SemanticLessonStore(` (lines 19, 28, 38, 49, 102, 110, 146 per the current file — replace all, don't miss any)
- Lines 73, 93: `from mimir.store_cognee import fastembed_embed` → `from mimir.store_semantic import fastembed_embed`
- Lines 129, 144: `from mimir.store_cognee import LanceDBVectorIndex` → `from mimir.store_semantic import LanceDBVectorIndex`

- [ ] **Step 3: Update `mimir/store.py`'s stale docstring**

Current (lines 1-7):
```python
"""C3 — bi-temporal LESSON store.

A thin, swappable interface. This in-memory reference implementation lets the
rest of the system (C2 consolidation, C4 retrieval, C5 benchmark) be built and
tested without standing up Cognee. The Cognee-backed adapter implements the same
surface later (PRD §8: storage behind a thin `mimir` interface, backend swappable).
"""
```
Replace with:
```python
"""C3 — bi-temporal LESSON store.

A thin, swappable interface. This in-memory reference implementation lets the
rest of the system (C2 consolidation, C4 retrieval, C5 benchmark) be built and
tested without a vector index. `SemanticLessonStore` (store_semantic.py)
implements the same surface with semantic recall added (PRD §8: storage
behind a thin `mimir` interface, backend swappable).
"""
```

- [ ] **Step 4: Update `mimir/__init__.py`**

Current:
```python
from mimir.store_cognee import CogneeLessonStore

__all__ = [
    "Episode", "Lesson", "InMemoryLessonStore", "CogneeLessonStore",
```
Replace with:
```python
from mimir.store_semantic import SemanticLessonStore

__all__ = [
    "Episode", "Lesson", "InMemoryLessonStore", "SemanticLessonStore",
```

- [ ] **Step 5: Update `mimir/mcp_server.py`'s comment**

Current (line 64):
```python
    # Prefer vector/semantic ranking when the store provides it (CogneeLessonStore);
```
Replace with:
```python
    # Prefer vector/semantic ranking when the store provides it (SemanticLessonStore);
```

- [ ] **Step 6: Update `mimir/hermes_memory.py`'s comments**

Current (line 9, inside the module docstring):
```python
- `prefetch` (called before each turn) -> mimir.recall against the same
  Cognee/LanceDB-backed store everything else reads from.
```
Replace with:
```python
- `prefetch` (called before each turn) -> mimir.recall against the same
  LanceDB-backed store everything else reads from.
```

Current (line 39):
```python
        """`store` is a test-only injection point; real Hermes calls this with just
        session_id + hermes_home, so a normal call builds the standard Cognee store."""
```
Replace with:
```python
        """`store` is a test-only injection point; real Hermes calls this with just
        session_id + hermes_home, so a normal call builds the standard store."""
```

- [ ] **Step 7: Update `README.md`**

Current (line 56, inside the ASCII diagram):
```
context / consequence)       contradiction check, then        Cognee/LanceDB
```
Replace with:
```
context / consequence)       contradiction check, then        LanceDB
```

Current (line 164):
```
Semantic storage and retrieval run directly on [LanceDB](https://github.com/lancedb/lancedb)
(`mimir/store_cognee.py`). Lessons are embedded and recalled through a thin,
```
Replace with:
```
Semantic storage and retrieval run directly on [LanceDB](https://github.com/lancedb/lancedb)
(`mimir/store_semantic.py`). Lessons are embedded and recalled through a thin,
```

- [ ] **Step 8: Update `tests/test_cli.py`'s two remaining `store_cognee` imports**

Line 168: `from mimir.store_cognee import hash_embed` → `from mimir.store_semantic import hash_embed`

Line 178: `import mimir.store_cognee as sc` → `import mimir.store_semantic as sc`

(`mimir/cli.py`'s own `store_cognee` imports at lines 75, 109, 115 are
deliberately NOT touched here — Part B rewrites those lines from scratch
as part of the file split, using the new name directly.)

Part A is now staged in the working tree but NOT committed and NOT
independently tested — `mimir/cli.py` still references the just-deleted
`mimir.store_cognee` module (Part B rewrites `cli.py` from scratch). Do
not run the test suite yet; it would fail on that dangling import, which
is expected at this intermediate point and not a bug to chase. Continue
directly into Part B.

### Part B: split `cli.py`

**Critical correctness note (read before writing code):** today,
`consolidate_main`/`serve_main`/`export_main` call `build_store()` bare
(no arguments) and it works because `build_store` is defined *inside*
`cli.py`, so its internal `lance_url or DEFAULT_LANCE` default resolves
against `cli.py`'s own module globals — which is exactly what several
existing tests rely on via `monkeypatch.setattr(cli, "DEFAULT_LANCE", ...)`
/ `monkeypatch.setattr(cli, "DEFAULT_LESSONS", ...)` before calling
`cli.consolidate_main()` / `cli.export_main()`. Once `build_store` moves to
`store_io.py`, a bare `build_store()` call from `cli.py`'s functions would
resolve `DEFAULT_LANCE`/`DEFAULT_LESSONS` from `store_io.py`'s *own*
globals instead — a Python function always resolves its module-level names
via its own `__globals__` (the module it's defined in), never the
caller's, no matter how it was imported. Monkeypatching `cli.DEFAULT_LANCE`
would silently become a no-op, and a test could end up writing to the real
`~/.mimir/lance.db` during a test run instead of a `tmp_path`.

**The fix, applied in Step 3 below:** `consolidate_main`, `serve_main`,
and `export_main` call `build_store(lance_url=DEFAULT_LANCE,
lessons_path=DEFAULT_LESSONS)` **explicitly** — referencing `cli.py`'s own
imported (and therefore still-monkeypatchable) names — instead of bare
`build_store()`. This preserves every existing test's monkeypatch target
(`cli.DEFAULT_LANCE`/`cli.DEFAULT_LESSONS`) exactly as-is.

- [ ] **Step 1: Create `mimir/store_io.py`**

```python
"""Store persistence: build the served/consolidated LESSON store from disk,
and persist LESSON objects back to it. The vector index is a derived cache
rebuilt from the persisted lessons on load, never the source of truth.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from mimir.models import Lesson

DEFAULT_HOME = Path.home() / ".mimir"
DEFAULT_LESSONS = DEFAULT_HOME / "lessons.json"     # persisted LESSON objects (source of truth)
DEFAULT_LANCE = DEFAULT_HOME / "lance.db"           # LanceDB vector index (rebuilt from lessons)
EMBED_MODEL_ENV = "MIMIR_EMBED_MODEL"   # opt-in real semantic embedder (fastembed model name)

_DT_FIELDS = ("valid_from", "invalid_at", "last_validated")


def _embed_fn():
    """None -> LanceDBVectorIndex's own hash_embed default (zero deps, unchanged
    behaviour). Set MIMIR_EMBED_MODEL (e.g. 'BAAI/bge-small-en-v1.5') to opt into
    real local semantic embeddings via fastembed (pip install 'mimir[embed]')."""
    model_name = os.environ.get(EMBED_MODEL_ENV)
    if not model_name:
        return None
    from mimir.store_semantic import fastembed_embed
    return lambda texts: fastembed_embed(texts, model_name=model_name)


def _lesson_from_row(row: dict) -> Lesson:
    data = dict(row)
    for f in _DT_FIELDS:
        v = data.get(f)
        data[f] = datetime.fromisoformat(v) if isinstance(v, str) else None
    return Lesson(**data)


def load_lessons(store, path: Path) -> int:
    """Rehydrate persisted LESSONs into `store` (re-upserting each into its vector index)."""
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        store.add(_lesson_from_row(row))
    return len(rows)


def save_lessons(store, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.snapshot(), encoding="utf-8")  # deterministic JSON (store.snapshot)


def build_store(*, lance_url: Optional[Path] = None, lessons_path: Optional[Path] = None):
    """The served/consolidated store: LanceDB vector engine + persisted lessons."""
    from mimir.store_semantic import LanceDBVectorIndex, SemanticLessonStore

    lance_url = lance_url or DEFAULT_LANCE          # resolved at call time, not frozen at import
    lessons_path = lessons_path or DEFAULT_LESSONS
    embed = _embed_fn()
    index_kwargs = {"embed": embed} if embed is not None else {}
    store = SemanticLessonStore(index=LanceDBVectorIndex(url=str(lance_url), **index_kwargs))
    load_lessons(store, lessons_path)
    return store
```

- [ ] **Step 2: Create `mimir/hook_install.py`**

```python
"""Hook installation: merge Mimir's capture hook into Claude Code's
settings.json, or write the PostToolUse script Cline picks up automatically.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_COMMAND = "mimir-hook"
HOOK_EVENTS = ("PostToolUse", "SessionEnd")

# Cline has no settings.json to merge into — it picks up an executable script named after
# the hook event from this directory (global scope; see docs.cline.bot/features/hooks).
DEFAULT_CLINE_HOOKS_DIR = Path.home() / "Documents" / "Cline" / "Rules" / "Hooks"
CLINE_HOOK_NAME = "PostToolUse"
CLINE_HOOK_COMMAND = "mimir-hook-cline"


# ---- pure, testable settings merge ----------------------------------------

def add_hook_command(settings: dict, event: str, command: str) -> dict:
    """Return a new settings dict with `command` registered under hook `event`.

    Idempotent: if the command is already present for that event, the *same*
    input object is returned unchanged. Never mutates the argument.
    """
    hooks = dict(settings.get("hooks", {}))
    groups = [dict(g) for g in hooks.get(event, [])]
    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == command:
                return settings  # already registered — no-op
    groups.append({"hooks": [{"type": "command", "command": command}]})
    hooks[event] = groups
    return {**settings, "hooks": hooks}


def hook_block(command: str = HOOK_COMMAND,
               events: Iterable[str] = HOOK_EVENTS) -> dict:
    """The settings.json fragment that registers the capture hook."""
    settings: dict = {}
    for event in events:
        settings = add_hook_command(settings, event, command)
    return settings


# ---- IO around the merge ---------------------------------------------------

def _load_settings(path: Path) -> dict:
    """Load settings.json, or {} if absent/empty. Refuse to touch invalid JSON
    so we never clobber a file we can't safely round-trip."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); refusing to overwrite. "
            "Fix it, or run `mimir install-hook --print` and paste the block yourself."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; refusing to overwrite.")
    return data


def install_hook(settings_path: Path = DEFAULT_SETTINGS, *,
                 command: str = HOOK_COMMAND,
                 events: Iterable[str] = HOOK_EVENTS) -> str:
    """Merge the capture hook into settings.json. Idempotent; backs up first."""
    settings = _load_settings(settings_path)
    updated = settings
    for event in events:
        updated = add_hook_command(updated, event, command)
    if updated is settings:
        return f"already registered in {settings_path}"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.parent / (settings_path.name + ".bak")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return f"registered {command} for {', '.join(events)} in {settings_path}"


def cline_hook_script(command: str = CLINE_HOOK_COMMAND) -> str:
    """The executable script Cline invokes for PostToolUse (POSIX shell)."""
    return f"#!/usr/bin/env sh\nexec {command}\n"


def install_cline_hook(hooks_dir: Path = DEFAULT_CLINE_HOOKS_DIR, *,
                       command: str = CLINE_HOOK_COMMAND) -> str:
    """Write the PostToolUse hook script Cline picks up automatically. It's mimir's own
    file (nothing to merge, unlike Claude Code's settings.json), so this just overwrites."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script_path = hooks_dir / CLINE_HOOK_NAME
    # newline="\n": the shebang line must stay LF-only even when written on Windows,
    # or a POSIX `sh` fails to resolve "/usr/bin/env sh\r" (bad-interpreter error).
    script_path.write_text(cline_hook_script(command), encoding="utf-8", newline="\n")
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass  # ponytail: no POSIX exec bit on Windows; Cline's Windows invocation is unconfirmed
    return f"wrote {script_path}"
```

- [ ] **Step 3: Rewrite `mimir/cli.py`**

Replace the entire file with:

```python
"""Mimir CLI — the zero-hustle entry points (PRD G5).

Installed by `pip install mimir`:

    mimir-hook                   # the command a Claude Code hook calls (reads
                                 #   stdin, appends one EPISODE, always exits 0)
    mimir consolidate            # slow path (C2): turn logged failure EPISODEs into
                                 #   gated LESSONs and persist them (needs a live judge)
    mimir-serve                  # serve the MCP tool surface over stdio, backed by
                                 #   the LanceDB LESSON store (pip install 'mimir[mcp]')
    mimir export --digest        # print active lessons as a markdown digest to stdout
    mimir install-hook           # register mimir-hook into ~/.claude/settings.json
                                 #   (idempotent, backs up the old file)
    mimir install-hook --print   # just print the settings block to paste yourself
    mimir install-hook --cline   # write the PostToolUse hook script Cline picks up
                                 #   (capture only; ~/Documents/Cline/Rules/Hooks/)
    mimir hook --config PATH     # capture from any tool via a declarative field-mapping
                                 #   config (see docs/integrations/generic.md); or set
                                 #   MIMIR_HOOK_CONFIG instead of --config

The end-to-end demo: install-hook (capture) -> use Claude -> `mimir consolidate`
(distill lessons into the LanceDB-backed store) -> `mimir-serve` (gated recall
over MCP). The store is the same on both sides, so what you consolidate is what
gets served.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from mimir import auto_consolidate
from mimir.capture import OUTCOME_FAIL, from_cline_hook, from_config_hook, from_hook, run_hook
from mimir.hook_install import (
    DEFAULT_CLINE_HOOKS_DIR,
    DEFAULT_SETTINGS,
    HOOK_COMMAND,
    HOOK_EVENTS,
    add_hook_command,
    cline_hook_script,
    hook_block,
    install_cline_hook,
    install_hook,
)
from mimir.models import Episode, Lesson
from mimir.store_io import (
    DEFAULT_HOME,
    DEFAULT_LANCE,
    DEFAULT_LESSONS,
    EMBED_MODEL_ENV,
    build_store,
    save_lessons,
)

log = logging.getLogger("mimir.cli")

DEFAULT_LOG = DEFAULT_HOME / "episodes.jsonl"
CITATION_KEY_ENV = "MIMIR_CITATION_KEY"
HOOK_CONFIG_ENV = "MIMIR_HOOK_CONFIG"       # generic adapter: path to a field-mapping config


def _log_path() -> Path:
    return Path(os.environ.get("MIMIR_EPISODE_LOG", str(DEFAULT_LOG)))


def _citation_key() -> str:
    return os.environ.get(CITATION_KEY_ENV, "mimir-dev")  # HMAC key for FR7 citations


def _episodes_from_log(path: Path, *, failures_only: bool = True) -> list[Episode]:
    """Read EPISODEs from the JSONL log. A MISTAKE (outcome 0.0) is what earns a lesson."""
    if not path.exists():
        return []
    episodes: list[Episode] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        row.pop("timestamp", None)  # judge/consolidate don't use it; skip the datetime parse
        ep = Episode(**row)
        if failures_only and ep.outcome_score != OUTCOME_FAIL:
            continue
        episodes.append(ep)
    return episodes


def _split_for_probe(episodes: list[Episode]) -> tuple[list[Episode], list[Episode]]:
    """Held-out vs extraction split for the live epsilon-gate probe (FR3): last third
    (minimum 1) held out. Fewer than 2 episodes -> no held-out evidence, extract from
    all (the probe will then fail-closed to 0.0 -- see make_live_counterfactual_probe).
    """
    if len(episodes) < 2:
        return [], episodes
    n_held_out = max(1, len(episodes) // 3)
    return episodes[-n_held_out:], episodes[:-n_held_out]


# ---- entry points ----------------------------------------------------------

def _ensure_utf8_stdio() -> None:
    """Windows terminals often default to a legacy codepage (e.g. cp1252) that can't
    encode the em-dashes used throughout Mimir's CLI text, mangling output into
    mojibake on the very first run. Reconfigure once per entry point instead of
    avoiding non-ASCII in every print()."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass  # non-reconfigurable stream (e.g. redirected to a pipe); leave as-is


def _extract_flag_value(args: list, flag: str) -> Optional[str]:
    """Return the token following `flag` in args, or None if the flag isn't present
    (or has nothing after it). No argparse needed for one optional flag."""
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


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
            config = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
            mapper = from_config_hook(config)
        except Exception:
            log.exception(
                "mimir hook: failed to load --config %s, skipping this capture", config_path)
            auto_consolidate.maybe_trigger(_log_path())
            return 0
    rc = run_hook(sys.stdin.read(), log_path=_log_path(), mapper=mapper)
    auto_consolidate.maybe_trigger(_log_path())
    return rc


def hook_main_cline(argv: Optional[list] = None) -> int:
    """`mimir-hook-cline` — what the Cline PostToolUse hook script invokes."""
    _ensure_utf8_stdio()
    rc = run_hook(sys.stdin.read(), log_path=_log_path(), mapper=from_cline_hook)
    auto_consolidate.maybe_trigger(_log_path())
    return rc


def consolidate_main(argv: Optional[list] = None, *, judge: Optional[Callable] = None,
                     probe: Optional[Callable] = None) -> int:
    """`mimir consolidate` — C2 slow path: logged failures -> gated LESSONs -> persist.

    Builds the LanceDB-backed store, runs EXTRACT (FR1 judge) -> ADMIT (FR3
    live counterfactual epsilon-gate) -> RESOLVE (FR2 contradiction) -> WRITE with an
    HMAC citation (FR7), then saves the store. `judge`/`probe` are real Claude calls
    by default (subscription auth, no API key); inject fakes to exercise the wiring
    token-free.
    """
    from mimir.consolidate import consolidate, sweep_episodes

    episodes = _episodes_from_log(_log_path())
    if not episodes:
        print(f"no failure EPISODEs in {_log_path()} yet; nothing to consolidate")
        return 0

    store = build_store(lance_url=DEFAULT_LANCE, lessons_path=DEFAULT_LESSONS)
    total = len(episodes)

    if judge is None:
        try:
            from bench.claude_judge import make_live_judge  # lazy: only the live path needs it
        except ImportError as exc:
            print(f"live consolidation needs the bench judge (run from the repo tree): {exc}",
                  file=sys.stderr)
            return 1
        judge = make_live_judge()

    if probe is None:
        try:
            from bench.claude_judge import make_live_counterfactual_probe  # lazy, same reason
        except ImportError as exc:
            print(f"live consolidation needs the bench probe (run from the repo tree): {exc}",
                  file=sys.stderr)
            return 1
        held_out, episodes = _split_for_probe(episodes)
        probe = make_live_counterfactual_probe(held_out)

    before = len(store.active())
    admitted = consolidate(episodes, store, judge=judge, probe=probe, key=_citation_key())

    # FR4: sweep the full log (not just failures) for lessons whose real-world adoption
    # correlates with regressions -- catches what ADMIT's ε-gate can't (it only sees the
    # probe set at write time, not how the lesson performs once actually used later).
    all_episodes = _episodes_from_log(_log_path(), failures_only=False)
    quarantined = sweep_episodes(store, all_episodes)

    save_lessons(store, DEFAULT_LESSONS)
    print(f"consolidated {total} failure episodes -> {len(admitted)} new lesson(s); "
          f"{len(quarantined)} lesson(s) quarantined by the FR4 circuit breaker; "
          f"store now {len(store.active())} active (was {before}); saved {DEFAULT_LESSONS}")
    return 0


def _auto_consolidate_worker_main(argv: Optional[list] = None) -> int:
    """Spawned by auto_consolidate.maybe_trigger as a detached background process; not a
    user-facing command (deliberately absent from the module docstring/usage text). Runs
    the same consolidate_main() as `mimir consolidate`, then always updates the
    auto-trigger state and releases the lock, even if consolidation itself raised. Only
    advances the failure-count baseline when consolidate_main actually returned success
    (0) -- a nonzero return (e.g. the bench judge/probe couldn't be imported) or a raised
    exception means no real work happened, so the accumulated failures stay pending for
    the next eligible retry instead of being silently forgotten."""
    rc = None
    try:
        rc = consolidate_main()
        return rc
    finally:
        auto_consolidate.finish_run(advance_baseline=(rc == 0))


def serve_main(argv: Optional[list] = None) -> int:
    """`mimir-serve` — serve the MCP tool surface over stdio, on the LanceDB store."""
    _ensure_utf8_stdio()
    from mimir.serve import build_server
    try:
        store = build_store(lance_url=DEFAULT_LANCE, lessons_path=DEFAULT_LESSONS)
        server = build_server(store, log_path=_log_path())
    except ImportError as exc:
        print(f"mimir-serve needs the serve deps: pip install 'mimir[mcp]' ({exc})",
              file=sys.stderr)
        return 1
    print(f"mimir-serve: {len(store.active())} active lessons loaded from {DEFAULT_LESSONS}",
          file=sys.stderr)
    server.run()
    return 0


def render_digest(lessons: list[Lesson]) -> str:
    """Human-readable markdown snapshot of active lessons, sorted by confidence desc."""
    if not lessons:
        return "# Mimir digest\n\nno active lessons yet.\n"
    lines = ["# Mimir digest", ""]
    for lesson in sorted(lessons, key=lambda lo: lo.confidence, reverse=True):
        lines.append(f"- **{lesson.rule}** (confidence: {lesson.confidence:.2f}, id: {lesson.id})")
    return "\n".join(lines) + "\n"


def export_main(argv: Optional[list] = None) -> int:
    """`mimir export --digest` — markdown snapshot of active lessons, printed to stdout.
    Redirect with `>` for a file; no new file-writing path (same stdout convention as
    `install-hook --print`)."""
    argv = argv or []
    if "--digest" not in argv:
        print("usage: mimir export --digest", file=sys.stderr)
        return 2
    try:
        store = build_store(lance_url=DEFAULT_LANCE, lessons_path=DEFAULT_LESSONS)
    except ImportError as exc:
        print(f"mimir export needs the serve deps: pip install 'mimir[mcp]' ({exc})",
              file=sys.stderr)
        return 1
    print(render_digest(store.active()))
    return 0


def main(argv: Optional[list] = None) -> int:
    """`mimir` — top-level dispatcher."""
    _ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "install-hook":
        if "--cline" in rest:
            if "--print" in rest:
                print(cline_hook_script())
            else:
                print(install_cline_hook())
            return 0
        if "--print" in rest:
            print(json.dumps(hook_block(), indent=2))
            return 0
        print(install_hook())
        return 0
    # ponytail: manual consolidate intentionally shares no lock with the auto-consolidate
    # worker -- a manual run during an in-flight background run can race it (both write
    # lessons.json, last-writer-wins). Low probability on a single-dev machine; add
    # locking here if that stops being true.
    if cmd == "consolidate":
        return consolidate_main(rest)
    if cmd == "serve":
        return serve_main(rest)
    if cmd == "export":
        return export_main(rest)
    if cmd == "hook":
        return hook_main(rest)
    if cmd == "_auto-consolidate-worker":
        return _auto_consolidate_worker_main()
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # `python -m mimir.cli <cmd>` when running from the repo tree
    raise SystemExit(main())
```

Note what changed from the original vs. what stayed identical: `main`,
`hook_main`, `hook_main_cline`, `_auto_consolidate_worker_main`,
`render_digest`, `_ensure_utf8_stdio`, `_extract_flag_value`,
`_log_path`, `_citation_key`, `_episodes_from_log`, `_split_for_probe` are
byte-for-byte unchanged. `consolidate_main`, `serve_main`, `export_main`
each have exactly one line changed: `build_store()` →
`build_store(lance_url=DEFAULT_LANCE, lessons_path=DEFAULT_LESSONS)`
(the correctness fix described above). Everything else is import
reorganization and the removal of the code that moved to the two new
files.

- [ ] **Step 4: Update `tests/test_cli.py`'s import line**

Line 6, current:
```python
from mimir.cli import add_hook_command, cline_hook_script, install_cline_hook, install_hook
```
Replace with:
```python
from mimir.hook_install import add_hook_command, cline_hook_script, install_cline_hook, install_hook
```

- [ ] **Step 5: Fix the one test with a direct bare `build_store()` call**

In `tests/test_cli.py`, find `test_export_main_prints_digest_from_store`
(currently around line 297-311). Current:

```python
def test_export_main_prints_digest_from_store(tmp_path, monkeypatch, capsys):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    store = cli.build_store()
    store.add(Lesson(rule="pin tool versions before release", confidence=0.8, id="L1"))
    cli.save_lessons(store, cli.DEFAULT_LESSONS)

    assert cli.export_main(["--digest"]) == 0
    out = capsys.readouterr().out
    assert "pin tool versions before release" in out
```

Replace the `store = cli.build_store()` line with an explicit-kwargs call
matching the pattern used everywhere else in this file, so it doesn't
depend on `cli.build_store`'s bare-call default resolution (which now
lives in a different module than the monkeypatched names — see this
task's "Critical correctness note"):

```python
def test_export_main_prints_digest_from_store(tmp_path, monkeypatch, capsys):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    store.add(Lesson(rule="pin tool versions before release", confidence=0.8, id="L1"))
    cli.save_lessons(store, cli.DEFAULT_LESSONS)

    assert cli.export_main(["--digest"]) == 0
    out = capsys.readouterr().out
    assert "pin tool versions before release" in out
```

(The `monkeypatch.setattr(cli, "DEFAULT_LESSONS"/"DEFAULT_LANCE", ...)`
lines stay — they're still needed so `cli.export_main(["--digest"])`'s
own internal `build_store(lance_url=DEFAULT_LANCE,
lessons_path=DEFAULT_LESSONS)` call, per Step 3's fix, picks up the same
tmp_path values as the store built directly above it.)

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q`

Expected: **185 passed**, 0 failed. This is the first point since Part A
began where the suite is expected to run at all (Part A alone leaves
`cli.py` importing a deleted module) — it confirms the rename and the
split together, as one unit.

If anything fails, check first: (a) every `cli.py` reference to a moved
name (`DEFAULT_LANCE`, `DEFAULT_LESSONS`, `DEFAULT_HOME`, `EMBED_MODEL_ENV`,
`build_store`, `save_lessons`, `DEFAULT_SETTINGS`, `HOOK_COMMAND`,
`HOOK_EVENTS`, `DEFAULT_CLINE_HOOKS_DIR`, `CLINE_HOOK_NAME`,
`CLINE_HOOK_COMMAND`, `add_hook_command`, `hook_block`,
`install_cline_hook`, `install_hook`, `cline_hook_script`) is imported at
the top of the new `cli.py` — a `NameError` means one was missed; (b) the
three `build_store(lance_url=DEFAULT_LANCE, lessons_path=DEFAULT_LESSONS)`
call sites in `consolidate_main`/`serve_main`/`export_main` are present
exactly as written in Step 3, not left as bare `build_store()`.

- [ ] **Step 7: Commit Task 1 (rename + split, both parts together)**

```bash
git add mimir/store_semantic.py tests/test_store_semantic.py mimir/store.py \
       mimir/__init__.py mimir/mcp_server.py mimir/hermes_memory.py README.md \
       mimir/store_io.py mimir/hook_install.py mimir/cli.py tests/test_cli.py
git status --short   # confirm store_cognee.py / test_store_cognee.py show as deleted (renamed)
git commit -m "$(cat <<'EOF'
refactor: rename CogneeLessonStore to SemanticLessonStore, split cli.py

CogneeLessonStore/store_cognee.py were named after a dependency already
removed earlier this session -- misleading to anyone grepping for it.
Renamed to SemanticLessonStore/store_semantic.py throughout.

cli.py had grown to 448 lines covering three unrelated responsibilities
(store persistence, hook-install settings management, CLI dispatch),
already marked with section comments. Split into store_io.py,
hook_install.py, and a thinner cli.py along those existing boundaries.
consolidate_main/serve_main/export_main now pass build_store's lance_url/
lessons_path explicitly instead of relying on its bare-call defaults, so
existing tests' monkeypatch targets (cli.DEFAULT_LANCE/DEFAULT_LESSONS)
keep working correctly now that build_store lives in a different module.
EOF
)"
git status --short
```

## Task 2: `docs/superpowers/README.md`

**Files:**
- Create: `docs/superpowers/README.md`

**Interfaces:** None — pure documentation, no code consumes this.

- [ ] **Step 1: Create the file**

```markdown
# Mimir's own build trail

The specs (`specs/`) and implementation plans (`plans/`) in this
directory are the design documents produced while building Mimir itself,
using the same Superpowers workflow (brainstorm -> spec -> plan ->
implement -> review) that Mimir's own memory system is meant to support
for other coding agents.

They're kept here as a transparency trail — a record of what was decided
and why — not as documentation you need to read to use Mimir. Start with
the top-level [README](../../README.md) instead.
```

- [ ] **Step 2: Run the full test suite (regression check)**

Run: `pytest -q`

Expected: 185 passed, 0 failed (a new markdown file with no code changes
cannot affect test outcomes — this step confirms nothing else in the
working tree was accidentally left in a broken state before adding to it).

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/README.md
git commit -m "docs: frame docs/superpowers/ as Mimir's own build trail"
```

## Task 3: Version and maturity framing

**Files:**
- Modify: `pyproject.toml`
- Modify: `SECURITY.md`
- Modify: `CONTRIBUTING.md`

**Interfaces:** None — plain text edits, no code consumes these values at
runtime except `pyproject.toml`'s `version`/`description` (used only by
packaging tools, not imported by any Mimir code).

- [ ] **Step 1: Edit `pyproject.toml`**

Current:
```toml
[project]
name = "mimir"
version = "0.0.1"
description = "A developer-facing memory layer for AI agents (MCP server). In development."
```
Replace with:
```toml
[project]
name = "mimir"
version = "0.1.0"
description = "A developer-facing memory layer for AI agents (MCP server)."
```

- [ ] **Step 2: Edit `SECURITY.md`**

Line 3, current:
```markdown
Mimir is early-stage (v0.0.1). If you find a security issue — including in
```
Replace with:
```markdown
Mimir is pre-1.0 (v0.1.0). If you find a security issue — including in
```

Line 18, current:
```markdown
You should get an acknowledgment within a few days. There's no bug bounty at
this stage — this is a young solo project — but every report is read and
taken seriously.
```
Replace with:
```markdown
You should get an acknowledgment within a few days. There's no bug bounty
program, but every report is read and taken seriously.
```

(Line 23, "Pre-1.0, only the latest commit on `main` is supported." —
unchanged, still accurate.)

- [ ] **Step 3: Edit `CONTRIBUTING.md`**

Line 3, current:
```markdown
Thanks for considering contributing — Mimir is early (v0.0.1) and every bit
of help matters.
```
Replace with:
```markdown
Thanks for considering contributing — every bit of help matters.
```

- [ ] **Step 4: Run the full test suite (regression check)**

Run: `pytest -q`

Expected: 185 passed, 0 failed. None of these files are imported by any
test or by Mimir's own code at runtime — this step exists purely to
confirm the working tree is still fully green at the end of the plan.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml SECURITY.md CONTRIBUTING.md
git commit -m "docs: update version to 0.1.0, drop early-stage framing"
```
