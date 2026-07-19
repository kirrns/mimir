# Production-readiness pass — design

## Origin

User asked for the Mimir repo to feel "fully properly architected... made
by a senior AI engineer" and production-ready enough to be "everyone's
go-to." That request is too vague to scope directly, so the first step was
a read-only audit of the whole repo (hygiene, dead code, module boundaries,
CI/packaging, docs structure, README accuracy).

The audit's finding: the codebase is already in better shape than the
vague worry implied. Error handling, CI, and repo-root hygiene are all
already deliberate. Five concrete items surfaced. One (a broken install
command in `CONTRIBUTING.md`) was trivial and already fixed directly
(commit `5c4cd35`) since it had no design attached — just a wrong word.
The remaining four are genuine decisions, confirmed with the user, and are
what this spec scopes:

1. Rename `CogneeLessonStore`/`store_cognee.py` (live, central, named
   after an already-removed dependency).
2. Split `cli.py` (448 lines, three unrelated responsibilities already
   section-commented) into three files.
3. Add a short README to `docs/superpowers/` framing its 13 internal
   process docs as a deliberate build-trail, not clutter.
4. Bump `pyproject.toml` version and drop "early-stage" framing from
   `SECURITY.md`/`CONTRIBUTING.md`.

**Explicit non-goal:** `README.md` itself is not touched by this spec.
It's already well-written (confirmed by the audit) and should be rewritten
*after* these four land, not before — otherwise it would describe a state
that's about to change out from under it. README rewrite is a separate,
later spec.

## 1. Rename the Cognee-named class

### What's there now

`mimir/store_cognee.py` defines `CogneeLessonStore(InMemoryLessonStore)` —
the live, exported, central semantic-recall backend used by
`cli.py:build_store()` (the real `mimir consolidate`/`mimir-serve` path).
Its own docstring already explains that the Cognee async adapter was
removed and it now talks to `LanceDBVectorIndex` or `InProcessVectorIndex`
directly — no Cognee dependency exists anywhere in the live path. The
class and file name are the only things still saying "Cognee."

Two other stale references, found while reading the file for this spec:

- `mimir/store.py`'s module docstring: "lets the rest of the system... be
  built and tested without standing up Cognee. The Cognee-backed adapter
  implements the same surface later" — no such adapter exists; fix this
  sentence while touching the file for the rename's downstream import.
- `mimir/store_cognee.py`'s `LanceDBVectorIndex` docstring: "LanceDB
  (Cognee's own vector engine)" — LanceDB is a vector DB Cognee happens to
  use elsewhere; Mimir talks to it directly, with no Cognee involved.
  Reword to drop the Cognee framing entirely.

### The rename

| Old | New |
|---|---|
| `mimir/store_cognee.py` | `mimir/store_semantic.py` |
| `CogneeLessonStore` | `SemanticLessonStore` |
| `tests/test_store_cognee.py` | `tests/test_store_semantic.py` |

`LanceDBVectorIndex`, `InProcessVectorIndex`, `VectorIndex`, `hash_embed`,
`fastembed_embed` — unchanged, already correctly named.

### Every call site to update

Found via `grep -rl "CogneeLessonStore\|store_cognee"` across the tracked
tree, filtered to files that matter (excludes `docs/superpowers/specs/`
and `.superpowers/sdd/` — those are historical records of what was true
when written, and stay as-is; excludes `INTERNAL_*.md`, which is
gitignored and never enters the tracked tree):

- `mimir/__init__.py` — public API export
- `mimir/cli.py` — `build_store()`, `_embed_fn()` imports
  (this becomes `mimir/store_io.py` per section 2 below — do section 2's
  file split using the *new* name directly, don't rename-then-split)
- `mimir/hermes_memory.py` — check usage, update if present
- `mimir/mcp_server.py` — check usage, update if present
- `README.md` — one reference (line ~164 per the audit)
- `tests/test_cli.py` — check usage, update if present

### Testing

No new test cases — this is a pure rename. The existing test suite (moved
to `tests/test_store_semantic.py`, with `CogneeLessonStore` →
`SemanticLessonStore` throughout) must pass unchanged, proving behavior
didn't shift.

## 2. Split `cli.py`

### Current structure (448 lines, already section-commented)

Three responsibilities living in one file, each already marked with a
section comment:

1. **Store persistence** (lines 68-117): `_embed_fn`, `_lesson_from_row`,
   `load_lessons`, `save_lessons`, `build_store`.
2. **Hook-install settings management** (lines 149-233):
   `add_hook_command`, `hook_block`, `_load_settings`, `install_hook`,
   `cline_hook_script`, `install_cline_hook`.
3. **CLI entry points** (lines 237-449): `_ensure_utf8_stdio`,
   `_extract_flag_value`, `hook_main`, `hook_main_cline`,
   `consolidate_main`, `_auto_consolidate_worker_main`, `serve_main`,
   `render_digest`, `export_main`, `main`, plus `_log_path`,
   `_citation_key`, `_episodes_from_log`, `_split_for_probe` (helpers used
   only by `consolidate_main`).

### New structure

- **`mimir/store_io.py`** — responsibility 1. Exports: `load_lessons`,
  `save_lessons`, `build_store`. Constants: `DEFAULT_HOME`, `DEFAULT_LANCE`,
  `DEFAULT_LESSONS`, `EMBED_MODEL_ENV`. Imports
  `SemanticLessonStore`/`LanceDBVectorIndex` from the new
  `mimir/store_semantic.py` (section 1) directly — this file is written
  fresh with the new name, never imports the old one.
- **`mimir/hook_install.py`** — responsibility 2. Exports:
  `add_hook_command`, `hook_block`, `install_hook`, `cline_hook_script`,
  `install_cline_hook`. Constants: `DEFAULT_SETTINGS`, `HOOK_COMMAND`,
  `HOOK_EVENTS`, `DEFAULT_CLINE_HOOKS_DIR`, `CLINE_HOOK_NAME`,
  `CLINE_HOOK_COMMAND`.
- **`mimir/cli.py`** (thinner, responsibility 3 only) — imports
  `load_lessons`/`save_lessons`/`build_store` from `store_io` and
  `install_hook`/`hook_block`/`cline_hook_script`/`install_cline_hook`
  from `hook_install`, re-exporting nothing extra. Module docstring
  (usage text) stays put — it documents the CLI surface, which hasn't
  moved. `DEFAULT_LOG`, `CITATION_KEY_ENV`, `HOOK_CONFIG_ENV` stay in
  `cli.py` (only used by entry points).

### Backward compatibility

`tests/test_cli.py` currently imports helpers like `load_lessons`,
`install_hook`, etc. directly from `mimir.cli` (need to confirm exact
imports when writing the plan). Two options:

- **(a)** Update the test imports to point at the new modules.
- **(b)** Re-export the moved names from `cli.py` (`from mimir.store_io
  import load_lessons, save_lessons, build_store` etc.) so `mimir.cli.X`
  keeps working for anyone already importing it that way.

**Decision: (a).** `mimir.cli` re-exporting everything it just split out
defeats the purpose of splitting — it would still be a 448-line surface
from an import-path perspective, just with the bodies moved elsewhere.
Since Mimir is pre-1.0 and this file has never been documented as a
stable import surface for these internals (only the `mimir`/`mimir-hook`/
`mimir-serve` *commands* are the documented public surface, per
`pyproject.toml`'s `[project.scripts]`), breaking internal import paths is
acceptable. Update every import site instead.

### Testing

No new test cases — this is a structural move. `tests/test_cli.py`'s
existing tests must pass with updated imports, proving behavior is
unchanged. If `test_cli.py` itself has grown large enough to mirror the
three-way split (a call the plan should make after reading the current
file), splitting it in parallel is in scope; if not, leave it as one file
importing from three modules.

## 3. `docs/superpowers/README.md`

New file, `docs/superpowers/README.md`:

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

(Exact wording may be refined slightly when written — this is the
substance, not a placeholder; any refinement stays within this scope,
doesn't introduce new claims.)

### Testing

None — this is a docs-only addition, nothing to assert against.

## 4. Version/maturity framing

### `pyproject.toml`

- `version = "0.0.1"` → `version = "0.1.0"`
- `description = "A developer-facing memory layer for AI agents (MCP
  server). In development."` → `description = "A developer-facing memory
  layer for AI agents (MCP server)."` (drop the trailing "In
  development.")

### `SECURITY.md`

- Line 3: "Mimir is early-stage (v0.0.1)." → "Mimir is pre-1.0 (v0.1.0)."
  (keeps the factually-relevant part — pre-1.0 semver stability
  expectations — drops "early-stage" framing)
- Line 18: "There's no bug bounty at this stage — this is a young solo
  project — but every report is read and taken seriously." → "There's no
  bug bounty program, but every report is read and taken seriously."
  (drops "young solo project," keeps the substance: no bounty, reports are
  taken seriously)
- Line 23 ("Pre-1.0, only the latest commit on `main` is supported.")
  unchanged — this is a real, still-accurate policy statement, not
  maturity framing.

### `CONTRIBUTING.md`

- Line 3: "Thanks for considering contributing — Mimir is early (v0.0.1)
  and every bit of help matters." → "Thanks for considering contributing
  — every bit of help matters." (drops the version/maturity clause
  entirely; the version number itself isn't useful information in this
  sentence, unlike in `SECURITY.md` where pre-1.0 status has a real
  policy consequence)

### Testing

None — plain text edits, nothing to assert against. A grep for the old
strings after editing is a reasonable manual check, not a test case.

## Explicit non-goals (all four components)

- No `README.md` changes — separate, later spec, after these land.
- No CI/lint changes (the audit noted the absence of a ruff/mypy job as a
  decision point, not a defect — out of scope here).
- No `CHANGELOG.md` — not requested, no existing convention to extend.
- No further module splitting beyond `cli.py` — `consolidate.py` (254
  lines) and other files were judged coherent by the audit; not touched.
- `hermes_plugin`/`hermes_memory_plugin` naming — audit judged this
  legitimately-separate, not a duplicate; the fix there (a one-line
  clarifying doc note) wasn't part of what the user approved in this
  round. Not in scope.
