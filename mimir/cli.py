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
