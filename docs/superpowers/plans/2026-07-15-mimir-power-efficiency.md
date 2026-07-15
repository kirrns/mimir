# Mimir power/efficiency increment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ε-gate actually gate in the live `mimir consolidate` CLI path (it's currently a no-op), add a human-pinned protected-lesson tier immune to auto-supersede/quarantine, and add a `mimir export --digest` command that prints active lessons as readable markdown.

**Architecture:** Three independent, additive changes to the existing `mimir/` package. No new modules besides tests; every change extends an existing file along its established seams (the `_runner`-injectable live-call pattern in `bench/`, the store's `retire`/`rollback` pair, the CLI's existing subcommand dispatch in `main()`).

**Tech Stack:** Python 3.14, plain `pytest` (no new test frameworks/fixtures beyond stdlib `pytest.monkeypatch`/`capsys`, both already used in this test suite), no new dependencies.

## Global Constraints

- No new third-party dependencies. (Spec: "no new frameworks/fixtures.")
- Every live-call code path must stay testable with zero tokens via an injectable `_runner`/factory-function seam, matching `bench/claude_cli.py`'s existing pattern. (Spec §1, Design.)
- Fail-closed: an empty held-out set, or a probe given no lessons, returns `0.0` rather than raising or guessing. (Spec §1: "no held-out evidence means no counterfactual proof means no admission.")
- Do not `git push`. Commit locally only; a push needs the user's separate, explicit go-ahead (standing project rule, not in the spec file but established this session).
- Do not touch the currently-uncommitted `mimir/consolidate.py` / `tests/test_consolidate.py` probe-baseline-caching change from the prior session except by adding new code near it — leave its existing diff as-is, it is a separate uncommitted change the user has not asked to be folded in here.

---

### Task 1: Live counterfactual probe function

**Files:**
- Modify: `mimir/bench/claude_judge.py` (add function after `make_live_judge`, before `make_solver_probe`)
- Test: `mimir/tests/test_claude_judge.py` (add tests after the existing `test_scores_clamp_into_range`, before `test_solver_probe_measures_real_pass_rate`)

**Interfaces:**
- Consumes: `run_claude` (from `bench.claude_cli`, already imported in `claude_judge.py`), `Episode` (from `mimir.models`, already imported).
- Produces: `make_live_counterfactual_probe(held_out: list[Episode], *, runner: Optional[Callable] = None) -> Callable[[list], float]` — a `Probe` (matches `mimir.consolidate.Probe = Callable[[list[Lesson]], float]`). Task 2 imports this by name.

- [ ] **Step 1: Write the failing tests**

Add to `mimir/tests/test_claude_judge.py`. First add `Lesson` to the existing import line and `make_live_counterfactual_probe` to the `bench.claude_judge` import:

```python
from bench.claude_judge import (
    make_live_counterfactual_probe, make_live_judge, make_solver_probe, _parse_verdict,
)
from bench.live import CodeTask
from mimir.models import Episode, Lesson
```

Then add these two tests (after `test_scores_clamp_into_range`, before `test_solver_probe_measures_real_pass_rate`):

```python
def test_counterfactual_probe_scores_fraction_of_held_out_prevented():
    ep1 = Episode(action="a1", context="c1", consequence="boom1")
    ep2 = Episode(action="a2", context="c2", consequence="boom2")
    replies = iter(["yes", "no"])
    runner = lambda prompt, timeout: json.dumps({"result": next(replies)})

    probe = make_live_counterfactual_probe([ep1, ep2], runner=runner)

    assert probe([Lesson(rule="guard empty input")]) == 0.5


def test_counterfactual_probe_fails_closed_on_no_held_out_or_no_lessons():
    ep = Episode(action="a", context="c", consequence="boom")
    calls = []

    def runner(prompt, timeout):
        calls.append(prompt)
        return json.dumps({"result": "yes"})

    assert make_live_counterfactual_probe([], runner=runner)([Lesson(rule="x")]) == 0.0
    assert make_live_counterfactual_probe([ep], runner=runner)([]) == 0.0
    assert calls == []  # neither short-circuit called the runner
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mimir && python -m pytest tests/test_claude_judge.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_live_counterfactual_probe'`

- [ ] **Step 3: Write minimal implementation**

In `mimir/bench/claude_judge.py`, add after the `make_live_judge` function (before `make_solver_probe`):

```python
_PROBE_PROMPT = (
    "An AI coding agent has this lesson available: {rule}\n\n"
    "It later failed a task with this episode:\n"
    "  action: {action}\n  context: {context}\n  consequence: {consequence}\n\n"
    "Would having this lesson available likely have prevented this failure? "
    "Reply with ONLY one word: yes or no."
)


def make_live_counterfactual_probe(held_out: list[Episode], *, runner: Optional[Callable] = None):
    """FR3 live probe for the CLI's single-user consolidate path: fraction of held-out
    episodes the judge says a candidate lesson set would likely have prevented. One
    structured call per held-out episode per probe() invocation -- cheaper than a full
    solver replay (make_solver_probe below), same _runner injection seam as
    make_live_judge, zero tokens in tests. Fail-closed: ambiguous replies count as 'no'.
    """

    def probe(lessons: list) -> float:
        if not held_out or not lessons:
            return 0.0
        rules = "; ".join(lo.rule for lo in lessons)
        hits = 0
        for ep in held_out:
            prompt = _PROBE_PROMPT.format(
                rule=rules, action=ep.action, context=ep.context, consequence=ep.consequence)
            reply = run_claude(prompt, _runner=runner).strip().lower()
            if reply.startswith("yes"):
                hits += 1
        return hits / len(held_out)

    return probe
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mimir && python -m pytest tests/test_claude_judge.py -v`
Expected: PASS (6 passed: the 4 existing + 2 new)

- [ ] **Step 5: Commit**

```bash
cd mimir
git add bench/claude_judge.py tests/test_claude_judge.py
git commit -m "feat: add live counterfactual probe for the epsilon-gate (FR3)"
```

---

### Task 2: Wire the live probe into `mimir consolidate`, fixing the no-op default

**Files:**
- Modify: `mimir/mimir/cli.py:97-113` (add `_split_for_probe` near `_episodes_from_log`), `mimir/mimir/cli.py:211-246` (`consolidate_main`)
- Test: `mimir/tests/test_cli.py` (modify existing e2e test at line ~72, add new tests)

**Interfaces:**
- Consumes: `make_live_counterfactual_probe` (Task 1, imported lazily from `bench.claude_judge` inside `consolidate_main`, same lazy-import pattern already used for `make_live_judge`).
- Produces: `_split_for_probe(episodes: list[Episode]) -> tuple[list[Episode], list[Episode]]` (held_out, extraction) — pure, no I/O. `consolidate_main(argv=None, *, judge=None, probe=None) -> int` gains a `probe` keyword (previously `judge`-only); existing callers that only pass `judge` now get a *real* live probe instead of the no-op, so any existing test relying on unconditional admission must inject `probe` explicitly.

- [ ] **Step 1: Write the failing tests**

In `mimir/tests/test_cli.py`, first fix the existing end-to-end test so it keeps testing what it always tested (deterministic admission) rather than accidentally depending on the new live probe. Change:

```python
    fake_judge = lambda ep: Verdict(rule="guard json decode against empty input",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0
```

to:

```python
    fake_judge = lambda ep: Verdict(rule="guard json decode against empty input",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    fake_probe = lambda lessons: 1.0  # always clears epsilon; live probe wiring covered separately
    assert cli.consolidate_main(judge=fake_judge, probe=fake_probe) == 0
```

Then add these new tests at the end of `tests/test_cli.py`:

```python
def test_split_for_probe_holds_out_last_third_minimum_one():
    import mimir.cli as cli
    from mimir.models import Episode

    eps = [Episode(id=str(i)) for i in range(6)]
    held_out, extraction = cli._split_for_probe(eps)

    assert held_out == eps[-2:]      # 6 // 3 = 2
    assert extraction == eps[:-2]


def test_split_for_probe_extracts_all_when_fewer_than_two_episodes():
    import mimir.cli as cli
    from mimir.models import Episode

    assert cli._split_for_probe([]) == ([], [])
    one = [Episode(id="1")]
    assert cli._split_for_probe(one) == ([], one)


def test_consolidate_main_wires_live_probe_with_held_out_split(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import bench.claude_judge as cj
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)
    capture(Episode(action="a2", context="c2", consequence="boom2", outcome_score=0.0), log_path=log)
    capture(Episode(action="a3", context="c3", consequence="boom3", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    seen_held_out = []

    def fake_make_probe(held_out, **kw):
        seen_held_out.append(held_out)
        return lambda lessons: 1.0  # always clears epsilon

    monkeypatch.setattr(cj, "make_live_counterfactual_probe", fake_make_probe)

    fake_judge = lambda ep: Verdict(rule=f"rule for {ep.action}",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0

    assert len(seen_held_out) == 1
    held_out = seen_held_out[0]
    assert len(held_out) == 1           # 3 episodes -> 1 held out (last third, min 1)
    assert held_out[0].action == "a3"   # last episode held out, not extracted from


def test_consolidate_main_admits_nothing_with_one_episode_no_probe_injected(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    fake_judge = lambda ep: Verdict(rule="a rule", specificity=0.9,
                                    generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0  # still exits 0

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    assert store.active() == []  # fail-closed: no held-out evidence, nothing admitted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mimir && python -m pytest tests/test_cli.py -v`
Expected: FAIL — `test_split_for_probe_*` fail with `AttributeError: module 'mimir.cli' has no attribute '_split_for_probe'`; `test_consolidate_main_wires_live_probe_with_held_out_split` fails because `consolidate_main` doesn't accept `probe=` yet (`TypeError`) is not raised since it's not passed, but the monkeypatched `make_live_counterfactual_probe` is never called (old code path admits via the len()-counting placeholder) so `seen_held_out` stays empty and the assertion `len(seen_held_out) == 1` fails; `test_consolidate_main_admits_nothing_with_one_episode_no_probe_injected` fails because the old placeholder probe admits unconditionally, so `store.active()` is non-empty.

- [ ] **Step 3: Write minimal implementation**

In `mimir/mimir/cli.py`, add this function right after `_episodes_from_log` (before the `# ---- pure, testable settings merge` comment):

```python
def _split_for_probe(episodes: list[Episode]) -> tuple[list[Episode], list[Episode]]:
    """Held-out vs extraction split for the live epsilon-gate probe (FR3): last third
    (minimum 1) held out. Fewer than 2 episodes -> no held-out evidence, extract from
    all (the probe will then fail-closed to 0.0 -- see make_live_counterfactual_probe).
    """
    if len(episodes) < 2:
        return [], episodes
    n_held_out = max(1, len(episodes) // 3)
    return episodes[-n_held_out:], episodes[:-n_held_out]
```

Then replace `consolidate_main` entirely with:

```python
def consolidate_main(argv: Optional[list] = None, *, judge: Optional[Callable] = None,
                     probe: Optional[Callable] = None) -> int:
    """`mimir consolidate` — C2 slow path: logged failures -> gated LESSONs -> persist.

    Builds the Cognee/LanceDB-backed store, runs EXTRACT (FR1 judge) -> ADMIT (FR3
    live counterfactual epsilon-gate) -> RESOLVE (FR2 contradiction) -> WRITE with an
    HMAC citation (FR7), then saves the store. `judge`/`probe` are real Claude calls
    by default (subscription auth, no API key); inject fakes to exercise the wiring
    token-free.
    """
    from mimir.consolidate import consolidate

    episodes = _episodes_from_log(_log_path())
    if not episodes:
        print(f"no failure EPISODEs in {_log_path()} yet; nothing to consolidate")
        return 0

    store = build_store()
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
    save_lessons(store, DEFAULT_LESSONS)
    print(f"consolidated {total} failure episodes -> {len(admitted)} new lesson(s); "
          f"store now {len(store.active())} active (was {before}); saved {DEFAULT_LESSONS}")
    return 0
```

This removes the old `probe = lambda lessons: float(len(lessons))` placeholder entirely.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mimir && python -m pytest tests/test_cli.py -v`
Expected: PASS (all tests, including the modified e2e test and the 4 new ones)

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `cd mimir && python -m pytest -q`
Expected: PASS, no new failures. (Note: the repo currently has an unrelated uncommitted change in `mimir/consolidate.py` + `tests/test_consolidate.py` from a prior session — leave it as-is; it should already be passing and is untouched by this task.)

- [ ] **Step 6: Commit**

```bash
cd mimir
git add mimir/cli.py tests/test_cli.py
git commit -m "fix: replace no-op epsilon-gate placeholder with a real live probe

mimir consolidate's default probe was lambda lessons: float(len(lessons)),
which increases by exactly 1 whenever a lesson is added, so the epsilon-gate
(improvement 1.0 >= 0.05) always passed regardless of the lesson's merit.
Every judge-passed lesson was admitted unconditionally in the live CLI path;
only the benchmark (bench/live.py) had a real held-out probe.

Now consolidate_main holds out the last third (min 1, when >= 2 episodes
logged) of failure episodes and uses make_live_counterfactual_probe against
them. Fewer than 2 episodes means no held-out evidence, so nothing admits --
fail-closed, not a bug."
```

---

### Task 3: Protected (human-pinned) lessons

**Files:**
- Modify: `mimir/mimir/models.py:33-49` (`Lesson` dataclass)
- Modify: `mimir/mimir/store.py:55-59` (add `protect`/`unprotect` after `retire`)
- Modify: `mimir/mimir/consolidate.py:182-193` (`consolidate`'s supersede-target search), `mimir/mimir/consolidate.py:152-163` (`circuit_breaker_sweep`)
- Test: `mimir/tests/test_store.py` (add after `test_retire_excludes_lesson_but_keeps_it_on_record`), `mimir/tests/test_consolidate.py` (add after `test_consolidate_supersedes_contradicted_prior_lesson_bitemporally` and after `test_circuit_breaker_quarantines_regressor`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Lesson.protected: bool` (defaults `False`, round-trips through the existing `store.snapshot()`/`cli._lesson_from_row` JSON persistence automatically — both use `dataclasses.asdict`/`Lesson(**data)`, so no serialization code changes needed). `InMemoryLessonStore.protect(lesson_id) -> None` / `.unprotect(lesson_id) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `mimir/tests/test_store.py` (after `test_retire_excludes_lesson_but_keeps_it_on_record`):

```python
def test_protect_and_unprotect_roundtrip():
    store = InMemoryLessonStore()
    lid = store.add(_lesson("never force-push shared branches"))

    store.protect(lid)
    assert store.get(lid).protected is True

    store.unprotect(lid)
    assert store.get(lid).protected is False
```

Add to `mimir/tests/test_consolidate.py` (after `test_consolidate_supersedes_contradicted_prior_lesson_bitemporally`):

```python
def test_consolidate_does_not_supersede_a_protected_lesson():
    store = InMemoryLessonStore()
    protected_id = store.add(Lesson(rule="never retry network calls on failure",
                                    confidence=0.5, protected=True))

    def judge(ep):
        return C.Verdict(rule="always retry network calls on failure",
                         specificity=0.9, generalizability=0.8, non_sycophancy=0.9)

    def probe(lessons):
        return 0.9 if any("always retry" in lo.rule for lo in lessons) else 0.5

    admitted = C.consolidate([_ep()], store, judge=judge, probe=probe, key="k")

    assert len(admitted) == 1
    protected = store.get(protected_id)
    assert protected.status == "active"       # not superseded, even though it contradicts
    assert protected_id not in admitted[0].contradicts
    active_rules = {lo.rule for lo in store.active()}
    assert active_rules == {"never retry network calls on failure",
                            "always retry network calls on failure"}  # both coexist
```

And after `test_circuit_breaker_quarantines_regressor`:

```python
def test_circuit_breaker_skips_protected_lessons():
    store = InMemoryLessonStore()
    bad_protected = store.add(Lesson(rule="always force-push to fix conflicts", protected=True))

    observations = {
        bad_protected: [C.Adoption(adopted=True, outcome_score=0.0),
                        C.Adoption(adopted=True, outcome_score=0.0),
                        C.Adoption(adopted=False, outcome_score=1.0)],
    }

    quarantined = C.circuit_breaker_sweep(store, observations)

    assert quarantined == []
    assert store.get(bad_protected).status == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mimir && python -m pytest tests/test_store.py tests/test_consolidate.py -v`
Expected: FAIL — `test_protect_and_unprotect_roundtrip` with `AttributeError: 'InMemoryLessonStore' object has no attribute 'protect'`; `test_consolidate_does_not_supersede_a_protected_lesson` with `TypeError: Lesson.__init__() got an unexpected keyword argument 'protected'`; `test_circuit_breaker_skips_protected_lessons` with the same `TypeError`.

- [ ] **Step 3: Write minimal implementation**

In `mimir/mimir/models.py`, in the `Lesson` dataclass, add `protected` right after `status`:

```python
    rule: str                                  # specific directive, not "be careful"
    confidence: float = 0.0                    # 0..1, moves only on scored evidence (FR3)
    status: str = ACTIVE                        # active | quarantined | retired | superseded
    protected: bool = False                      # human-pinned: exempt from auto-supersede/quarantine
    supporting_episodes: list[str] = field(default_factory=list)
```

In `mimir/mimir/store.py`, add after `retire` (before `active`):

```python
    def protect(self, lesson_id: str) -> None:
        """Pin a lesson: exempt from auto-supersede and circuit-breaker quarantine.
        Human-set only (no CLI/MCP surface yet -- store API is the workflow for now)."""
        self._require(lesson_id).protected = True

    def unprotect(self, lesson_id: str) -> None:
        self._require(lesson_id).protected = False
```

In `mimir/mimir/consolidate.py`, in `consolidate()`, change:

```python
        loser = next((a for a in active if detect_contradiction(a, lesson)), None)
```

to:

```python
        loser = next((a for a in active if not a.protected and detect_contradiction(a, lesson)),
                     None)
```

And in `circuit_breaker_sweep`, change:

```python
    quarantined: list[str] = []
    for lesson in store.active():
        obs = observations.get(lesson.id, [])
```

to:

```python
    quarantined: list[str] = []
    for lesson in store.active():
        if lesson.protected:
            continue
        obs = observations.get(lesson.id, [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mimir && python -m pytest tests/test_store.py tests/test_consolidate.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd mimir
git add mimir/models.py mimir/store.py mimir/consolidate.py tests/test_store.py tests/test_consolidate.py
git commit -m "feat: add protected lesson flag, exempt from auto-supersede and quarantine"
```

---

### Task 4: `mimir export --digest`

**Files:**
- Modify: `mimir/mimir/cli.py` (add `render_digest` + `export_main` after `serve_main`, dispatch in `main()`, update module docstring)
- Test: `mimir/tests/test_cli.py` (add at the end)

**Interfaces:**
- Consumes: `Lesson` (already imported in `cli.py`), `build_store` (already in `cli.py`).
- Produces: `render_digest(lessons: list[Lesson]) -> str`, `export_main(argv: Optional[list] = None) -> int`.

- [ ] **Step 1: Write the failing tests**

Add to the end of `mimir/tests/test_cli.py`:

```python
def test_render_digest_sorts_by_confidence_descending():
    import mimir.cli as cli
    from mimir.models import Lesson

    lessons = [Lesson(rule="low confidence rule", confidence=0.3, id="L1"),
              Lesson(rule="high confidence rule", confidence=0.9, id="L2")]
    digest = cli.render_digest(lessons)
    assert digest.index("high confidence rule") < digest.index("low confidence rule")
    assert "confidence: 0.90" in digest
    assert "id: L2" in digest


def test_render_digest_handles_no_active_lessons():
    import mimir.cli as cli

    assert "no active lessons" in cli.render_digest([])


def test_export_main_requires_digest_flag():
    import mimir.cli as cli

    assert cli.export_main([]) == 2


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


def test_main_dispatches_export_command(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "export_main", lambda rest: calls.append(rest) or 0)
    assert cli.main(["export", "--digest"]) == 0
    assert calls == [["--digest"]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mimir && python -m pytest tests/test_cli.py -v`
Expected: FAIL — `AttributeError: module 'mimir.cli' has no attribute 'render_digest'` (and `export_main`); `test_main_dispatches_export_command` fails because `main()` doesn't recognize `export` yet (falls through to `unknown command`, returns 2 not 0, and `calls` stays empty).

- [ ] **Step 3: Write minimal implementation**

In `mimir/mimir/cli.py`, add after `serve_main` (before `def main`):

```python
def render_digest(lessons: list[Lesson]) -> str:
    """Human-readable markdown snapshot of active lessons, sorted by confidence desc."""
    if not lessons:
        return "# Mimir digest\n\nno active lessons yet.\n"
    lines = ["# Mimir digest", ""]
    for lo in sorted(lessons, key=lambda lo: lo.confidence, reverse=True):
        lines.append(f"- **{lo.rule}** (confidence: {lo.confidence:.2f}, id: {lo.id})")
    return "\n".join(lines) + "\n"


def export_main(argv: Optional[list] = None) -> int:
    """`mimir export --digest` — markdown snapshot of active lessons, printed to stdout.
    Redirect with `>` for a file; no new file-writing path (same stdout convention as
    `install-hook --print`)."""
    argv = argv or []
    if "--digest" not in argv:
        print("usage: mimir export --digest", file=sys.stderr)
        return 2
    store = build_store()
    print(render_digest(store.active()))
    return 0
```

Then in `main()`, add the dispatch branch (after the `serve` branch, before the `hook` branch):

```python
    if cmd == "serve":
        return serve_main(rest)
    if cmd == "export":
        return export_main(rest)
    if cmd == "hook":
        return hook_main(rest)
```

Finally, update the module docstring at the top of `mimir/mimir/cli.py` to document the new command, adding this line after the `mimir-serve` line:

```
    mimir export --digest         # print active lessons as a markdown digest to stdout
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mimir && python -m pytest tests/test_cli.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full suite**

Run: `cd mimir && python -m pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
cd mimir
git add mimir/cli.py tests/test_cli.py
git commit -m "feat: add mimir export --digest command"
```

---

## Post-plan note

Do not push any of these commits. The repo already has one unrelated uncommitted
change (`mimir/consolidate.py` probe-baseline caching + its test) from a prior
session — leave it uncommitted unless the user asks for it to be folded in or
committed separately.
