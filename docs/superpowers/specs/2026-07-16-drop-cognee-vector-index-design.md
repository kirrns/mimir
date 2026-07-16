# Storage backend: delete CogneeVectorIndex

Date: 2026-07-16
Status: implemented (part A of sub-project 3; part B — real embedder — implemented,
see addendum below)

## Context

Sub-project 3 of "make Mimir efficient and fast." Investigated whether Mimir
should build a custom vector engine (prompted by the user citing turbovec
as a fast quantized vector-search library and wanting "better than Cognee").
Finding: Cognee was already not in the live path — `build_store()` in
`mimir/cli.py` has always used `LanceDBVectorIndex` (direct LanceDB, sync
API), never `CogneeVectorIndex` (cognee's own async LanceDB adapter). The
latter existed only as a third, untested-in-production `VectorIndex`
implementation, and was the sole cause of the 25s hang-and-skip in the test
suite (already isolated behind a `slow` marker in sub-project 1).

## Change

Deleted `CogneeVectorIndex` (mimir/store_cognee.py) and everything that only
existed to support it:
- `lesson_uuid()`/`_NS` (only needed because cognee's `DataPoint.id` requires
  a UUID; `LanceDBVectorIndex`/`InProcessVectorIndex` use the lesson id
  string directly).
- Its test (`test_cognee_vector_index_live_or_skip`) and the `lesson_uuid`
  test.
- The `cognee` optional-dependency extra in `pyproject.toml` — replaced with
  `lancedb` added directly to the `mcp` extra (it was previously pulled in
  transitively via `cognee`'s own dependency on lancedb; now declared
  explicitly since it's the real live-path dependency).
- `cognee` import check in `hermes_memory.py`'s `is_available()` (only
  `lancedb` matters now).
- Public surface: README's "Built on Cognee" badge/section, `pip install`
  instructions, CI workflow's install step, `hermes_memory_plugin`'s install
  docstring — all updated to reflect LanceDB directly, not Cognee.

`CogneeLessonStore` (the class name) and `store_cognee.py` (the filename)
are unchanged — renaming those was out of scope (bigger, purely cosmetic
diff across every call site, no functional benefit); only the actually-dead
Cognee-specific code was removed.

## Result

Full suite (`pytest -m ""`, was full-coverage mode): 123 passed, 1 skipped,
~33.9s -> 122 passed, 0 skipped, ~3.1s. Default local `pytest`: 121 passed,
1 deselected, ~2.75s. The 25s cognee-hang wait and the cognee import cost
are both gone entirely, not just deferred behind a marker.

## Part B addendum: opt-in fastembed local embedder (implemented)

User chose "local model" over API-based (no key/network/per-call cost) or
improving the hash embedder in place. Chose [fastembed](https://github.com/qdrant/fastembed)
specifically (verified its actual API before writing code, not from memory):
ONNX runtime, no torch, `TextEmbedding(model_name=...).embed(texts)`, default
model `BAAI/bge-small-en-v1.5` (384-dim).

Design: kept `hash_embed` as the literal, unchanged default (no surprise
model download for existing users) — matches this codebase's existing DI
convention (real judge/probe/solver are opt-in injections everywhere else,
e.g. `bench/claude_cli.py`). New `MIMIR_EMBED_MODEL` env var (mirrors
`MIMIR_CITATION_KEY`'s pattern) opts a real fastembed model into
`build_store()`'s `LanceDBVectorIndex`; unset, behavior is byte-for-byte
identical to before.

- `mimir/store_cognee.py`: `fastembed_embed()` — lazy import (core stays
  dependency-free), output unit-normalized (`_unit()`) since fastembed
  doesn't guarantee it and the `Embed` contract requires it for cosine
  similarity to be meaningful. `_model_cls` injection seam keeps the
  normalization logic unit-testable without installing fastembed or
  downloading a model.
- `mimir/cli.py`: `_embed_fn()` reads `EMBED_MODEL_ENV`; `build_store()`
  passes it through to `LanceDBVectorIndex` only when set.
- New `embed` extra (`pyproject.toml`) — `fastembed` is not pulled in by
  `mcp` or `dev`, purely opt-in.
- Tests: pure normalization test (fake `_model_cls`, no real model) +
  `@pytest.mark.slow` `pytest.importorskip("fastembed")` live smoke test
  (skips cleanly in CI/dev since fastembed isn't installed there) + two
  `test_cli.py` tests proving `build_store()` routes to hash_embed by
  default and to fastembed when the env var is set (monkeypatched, no real
  download).

Result: 125 passed, 1 skipped (`-m ""`, the fastembed live test skips since
fastembed isn't installed in this environment) in ~3.1s. Default local
`pytest`: 124 passed, 2 deselected, ~3.3s.

## Open questions

None.
