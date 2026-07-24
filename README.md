# Mimir

<p align="center">
  <img alt="Mimir: the agent memory that has to earn its keep" src="assets/mimir.png" width="640">
</p>

<p align="center">
  <a href="https://github.com/kirrns/mimir/actions/workflows/tests.yml"><img src="https://github.com/kirrns/mimir/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
</p>

<p align="center">
  <a href="https://github.com/kirrns/mimir/issues">Issues</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="CODE_OF_CONDUCT.md">Code of Conduct</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="LICENSE">License</a>
</p>

**The agent memory that has to earn its keep.**

Your coding agent nails a fix, you move on, and three sessions later it makes
the exact same mistake again. Most memory tools "fix" this by hoarding
everything they see and hoping some of it helps. Usually it doesn't — it just
makes retrieval slower.

Mimir keeps a lesson only if a before/after benchmark proves the agent does
measurably better with it. No proof, no lesson. Every lesson that survives is
HMAC-signed and traceable back to the failure that created it.

Install it once, and capture runs quietly in the background of every Claude
Code session — it never blocks, never raises.

<p align="center">
  <a href="https://youtu.be/VvS3vpu5USw">
    <img src="https://img.youtube.com/vi/VvS3vpu5USw/maxresdefault.jpg" alt="Watch the demo" width="640">
  </a>
  <br>
  <sub>&#9654; <a href="https://youtu.be/VvS3vpu5USw">watch the demo</a></sub>
</p>

> **v0.1.1.** Capture, consolidate, recall, and forget all work end-to-end
> and are covered by CI. Pre-1.0: interfaces can still move.

---

## How it works

```
capture (fast path)          consolidate (slow path)          recall (MCP)
─────────────────────        ─────────────────────────        ─────────────────
Claude Code hook logs   ──►  LLM judge extracts a rule   ──►  confidence-gated
EPISODEs (action /           from failure episodes;           retrieval over the
context / consequence)       contradiction check, then        LanceDB
to append-only JSONL.        HMAC-signed LESSON written       vector store, served
Never blocks, never          to the vector store.             as MCP tools.
raises.
```

- **EPISODE** — what the agent did, in what context, with what consequence,
  scored by a deterministic verifier.
- **LESSON** — a distilled directive (never "be careful"), with a confidence
  score that only moves on scored evidence, a bi-temporal lifecycle
  (`active → quarantined / superseded / retired`, never hard-deleted), and an
  HMAC-SHA-256 citation back to the episodes that support it.

Once enough failures pile up (5 by default) and enough time has passed since
the last run (4 hours by default), the hook quietly spawns a background
`mimir consolidate` on its own — no command to remember. Run it by hand any
time, or set `MIMIR_AUTO_CONSOLIDATE=0` to go fully manual. Every run is
logged to `~/.mimir/auto_consolidate.log`, and `mimir.forget` retires a
lesson instantly if it stops earning its keep.

---

## The benchmark

`bench/` runs the same tasks two ways — an agent with Mimir's lessons (WARM)
and the same agent with none (COLD) — with seeded runs, a held-out probe set,
and a noise-band gate: a lesson only counts if the lift clears the noise
floor. Memory you can't attribute to an outcome improvement is just storage.

|  | Typical memory layer | Mimir |
|---|---|---|
| What gets kept | Everything it sees | Only what clears the gate |
| Evidence | "Should help" | WARM vs. COLD, seeded, noise-banded |
| Reproducible? | Usually not | Yes — `demo_band()` is one command, on your machine |

```bash
pytest tests/test_live.py                                    # token-free: injected fake model
python -c "from bench.live import demo_band; demo_band(3)"  # live: real Claude via your CLI, ~27 calls
```

The live run prints each arm's mean success rate with a (min, max) noise
band. A WARM−COLD lift smaller than the band gets reported as noise, not a
result.

For a rendered scoreboard instead of raw numbers:

```bash
MIMIR_CLAUDE_MODEL=sonnet python -m bench.scoreboard --repeats 3
open bench/scoreboard/index.html
```

(`--repeats 1` for a quick single pass instead of a noise-banded one.)

---

## Quickstart

```bash
pip install 'mimir-agent-memory[mcp]'
mimir setup
```

That's it. `mimir setup` registers the capture hook in
`~/.claude/settings.json` and adds `mimir-serve` as an MCP server via
`claude mcp add` (if the `claude` CLI is on `PATH` — capture still works
without it). Work normally; failures land in `~/.mimir/episodes.jsonl` and
lessons come back automatically through `mimir.recall`.

Not using the `claude` CLI, or want to wire it up by hand? `mimir
install-hook` registers just the capture hook, and `mimir-serve` is a stdio
MCP server any MCP client can point at directly.

**Windows:** pip installs `mimir`/`mimir-hook`/`mimir-serve` into
`%APPDATA%\Python\Python3XX\Scripts`, which often isn't on `PATH`. If `mimir`
isn't found after install, add that folder to `PATH`, or run everything as
`python -m mimir.cli ...`.

**Cline & Hermes:** `mimir install-hook --cline` writes the `PostToolUse`
hook Cline picks up automatically. Hermes gets two drop-in plugins —
`hermes_plugin/` for capture, and `hermes_memory_plugin/`, which registers
Mimir as a real Hermes `MemoryProvider` (`memory.provider: mimir` in
Hermes' `config.yaml`). Schemas here aren't verified against a live session
yet; [open an issue](https://github.com/kirrns/mimir/issues) if something
doesn't map.

**Anything else:** `mimir hook --config <path>` plugs in any tool that can
run a shell command with JSON on stdin — see
[docs/integrations/generic.md](docs/integrations/generic.md).

---

## Storage

Storage and retrieval run directly on [LanceDB](https://github.com/lancedb/lancedb)
(`mimir/store_semantic.py`) — no framework in between, just a thin,
swappable `VectorIndex` seam (LanceDB on disk, or a zero-dependency
in-process cosine index for tests). The LESSON objects are the source of
truth; the vector index is a derived cache rebuilt from them on load.

The default embedder is a dependency-free token-hashing bag-of-words — good
enough for shared-vocabulary matches, zero install cost. For real semantic
recall, opt into a local [fastembed](https://github.com/qdrant/fastembed)
model (ONNX, no torch, no network after the first download):

```bash
pip install 'mimir-agent-memory[embed]'
export MIMIR_EMBED_MODEL=BAAI/bge-small-en-v1.5   # any fastembed model name
```

## MCP tools

`mimir-serve` exposes capture, consolidate, recall, and forget over stdio to
any MCP client, Claude Code included:

- `mimir.capture` — log an episode directly, when you're not using the hook
- `mimir.consolidate` — run the judged, gated distillation pass on demand
- `mimir.recall` — ranked lesson retrieval for the current context; not
  exposed at all until there's something to recall, so you never get a
  round-trip that could only come back empty
- `mimir.forget` — retire a lesson; the prior version stays on record for
  audit but is excluded from recall

(`mimir.attribute`, single-lesson counterfactual credit, stays CLI/bench-only
— it needs an injected solver callable and only makes sense inside the
benchmark harness.)

---

## Development

```bash
git clone https://github.com/kirrns/mimir && cd mimir
pip install -e '.[dev,mcp]'
pytest
```

Python ≥ 3.10. The core package is dependency-free; `mcp` and `lancedb` are
the `mcp` extra, imported lazily so tests run without either installed.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project
follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Found a security
issue? See [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## License

[Apache License 2.0](LICENSE)
