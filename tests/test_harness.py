"""C5 — WARM/COLD benchmark harness. Tests first (TDD RED).

Invariants under test (BUILD_SPEC C5):
- a COLD run scores a task set deterministically (success_rate / mean_score)
- runs are reproducible: same seed -> identical records
- WARM minus COLD lift is computed from the two arms (the curve primitive)
- a solver crash is a failed task (a MISTAKE), never a harness crash
- the run is logged one row per task (reproducibility artifact)
"""
import json

from bench.harness import COLD, COLD_NAIVE, WARM, Task, lift, net_value, run


def _task(tid, payload, scorer):
    return Task(id=tid, payload=payload, verify=scorer)


def _binary(expected):
    return lambda answer: 1.0 if answer == expected else 0.0


def test_cold_run_scores_deterministically():
    tasks = [
        _task("t1", "a", _binary("a")),   # solver echoes -> pass
        _task("t2", "b", _binary("b")),   # pass
        _task("t3", "c", _binary("WRONG")),  # fail
    ]
    report = run(tasks, solver=lambda payload, lessons: payload, seed=0)
    assert report.arm == "cold"
    assert report.n == 3
    assert report.success_rate == 2 / 3
    assert report.mean_score == 2 / 3


def test_run_is_reproducible():
    tasks = [_task("t1", "a", _binary("a"))]
    solver = lambda payload, lessons: payload
    # duration_s is real wall time (inherently non-deterministic); pin the clock so
    # the reproducibility check still covers every field, timing included.
    a = run(tasks, solver=solver, seed=42, _clock=lambda: 0.0)
    b = run(tasks, solver=solver, seed=42, _clock=lambda: 0.0)
    assert a.records == b.records


def test_warm_lift_over_cold():
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    # cold solver can't solve it; warm solver solves it iff it was handed a lesson
    cold_solver = lambda payload, lessons: "stuck"
    warm_solver = lambda payload, lessons: "solved" if lessons else "stuck"

    cold = run(tasks, solver=cold_solver, seed=0)
    warm = run(tasks, solver=warm_solver, recall=lambda task: ["use the X trick"], seed=0)

    assert cold.mean_score == 0.0
    assert warm.mean_score == 1.0
    assert warm.arm == "warm"
    assert lift(warm, cold)["mean_score_lift"] == 1.0
    assert lift(warm, cold)["success_rate_lift"] == 1.0


def test_naive_arm_is_distinct_from_warm():
    """The 'never cut' three-arm bar: COLD < COLD+naive < WARM, each its own label.

    COLD+naive and WARM both pass a recall callable; only the explicit arm label
    distinguishes naive context-stuffing from real Mimir recall — so a naive win
    can't masquerade as a WARM win.
    """
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    solver = lambda payload, lessons: "solved" if lessons else "stuck"

    naive = run(tasks, solver=solver, recall=lambda t: ["stuff everything"],
                arm=COLD_NAIVE, seed=0)
    warm = run(tasks, solver=solver, recall=lambda t: ["the X trick"],
               arm=WARM, seed=0)

    assert naive.arm == COLD_NAIVE
    assert warm.arm == WARM
    assert naive.arm != warm.arm  # naive cannot masquerade as warm


def test_arm_defaults_preserve_cold_warm_inference():
    tasks = [_task("t1", "a", _binary("a"))]
    solver = lambda payload, lessons: payload
    assert run(tasks, solver=solver, seed=0).arm == COLD
    assert run(tasks, solver=solver, recall=lambda t: [], seed=0).arm == WARM


def test_solver_crash_is_failed_task_not_harness_crash():
    def boom(payload, lessons):
        raise RuntimeError("solver blew up")

    report = run([_task("t1", "a", _binary("a"))], solver=boom, seed=0)
    assert report.records[0]["score"] == 0.0  # counted as a MISTAKE, run survived


def test_warm_record_carries_lesson_ids_for_adoption_tracking():
    """FR4: each record's lesson_ids is what circuit_breaker_sweep needs to tell
    'this lesson was recalled for this task' from 'it wasn't' (bench.build_adoptions)."""
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    solver = lambda payload, lessons: "solved" if lessons else "stuck"

    class Lo:
        def __init__(self, id):
            self.id = id

    warm = run(tasks, solver=solver, recall=lambda t: [Lo("L1"), Lo("L2")], seed=0)

    assert warm.records[0]["lesson_ids"] == ["L1", "L2"]


def test_cold_record_has_empty_lesson_ids():
    tasks = [_task("t1", "a", _binary("a"))]
    cold = run(tasks, solver=lambda payload, lessons: payload, seed=0)
    assert cold.records[0]["lesson_ids"] == []


# --- C5 net value: lift minus added token/latency cost -----------------------

class _Lo:
    def __init__(self, rule, id="L"):
        self.rule = rule
        self.id = id


def test_record_carries_duration_and_lesson_chars():
    tasks = [_task("t1", "a", _binary("a"))]
    ticks = iter([10.0, 10.25])  # start, end -> 0.25s, deterministic via injected clock
    report = run(tasks, solver=lambda payload, lessons: payload, seed=0,
                recall=lambda t: [_Lo("ab"), _Lo("cde")], _clock=lambda: next(ticks))

    assert report.records[0]["duration_s"] == 0.25
    assert report.records[0]["n_lesson_chars"] == 5  # len("ab") + len("cde")


def test_mean_duration_and_lesson_chars_properties():
    tasks = [_task("t1", "a", _binary("a")), _task("t2", "b", _binary("b"))]
    ticks = iter([0.0, 1.0, 0.0, 3.0])  # durations 1.0, 3.0 -> mean 2.0
    report = run(tasks, solver=lambda payload, lessons: payload, seed=0,
                recall=lambda t: [_Lo("abcd")], _clock=lambda: next(ticks))

    assert report.mean_duration_s == 2.0
    assert report.mean_lesson_chars == 4.0


def test_net_value_with_zero_cost_weights_equals_score_lift():
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    cold = run(tasks, solver=lambda p, l: "stuck", seed=0)
    warm = run(tasks, solver=lambda p, l: "solved" if l else "stuck",
              recall=lambda t: [_Lo("use the X trick")], seed=0)

    nv = net_value(warm, cold)
    assert nv["mean_score_lift"] == 1.0
    assert nv["net_value"] == 1.0  # no cost weights -> net value == raw lift


def test_net_value_penalizes_added_latency_and_tokens():
    tasks = [_task("t1", "x", lambda answer: 1.0 if answer == "solved" else 0.0)]
    cold_ticks = iter([0.0, 0.0])   # 0s, no lessons
    warm_ticks = iter([0.0, 1.0])   # 1s added latency
    cold = run(tasks, solver=lambda p, l: "stuck", seed=0, _clock=lambda: next(cold_ticks))
    warm = run(tasks, solver=lambda p, l: "solved" if l else "stuck",
              recall=lambda t: [_Lo("a" * 10)], seed=0, _clock=lambda: next(warm_ticks))

    nv = net_value(warm, cold, cost_per_second=0.5, cost_per_char=0.01)
    # lift 1.0 - (0.5 * 1s added latency + 0.01 * 10 added chars) = 1.0 - 0.6 = 0.4
    assert nv["net_value"] == 0.4


# --- C5 equal-budget control: identical compute ceiling per arm --------------

def test_budget_truncates_lessons_to_fit_the_ceiling():
    tasks = [_task("t1", "a", _binary("a"))]
    seen = []

    def solver(payload, lessons):
        seen.append(lessons)
        return payload

    # "ab"(2) + "cde"(3) fits in budget=5; "fghij"(5) would push it to 10, dropped.
    lessons = [_Lo("ab"), _Lo("cde"), _Lo("fghij")]
    run(tasks, solver=solver, seed=0, recall=lambda t: lessons, budget=5)

    assert [lo.rule for lo in seen[0]] == ["ab", "cde"]


def test_budget_none_keeps_all_lessons():
    tasks = [_task("t1", "a", _binary("a"))]
    seen = []

    def solver(payload, lessons):
        seen.append(lessons)
        return payload

    lessons = [_Lo("ab"), _Lo("cde"), _Lo("fghij")]
    run(tasks, solver=solver, seed=0, recall=lambda t: lessons, budget=None)

    assert len(seen[0]) == 3


def test_budget_applies_to_naive_and_warm_alike():
    """The honesty bar: naive can't out-stuff WARM under the same ceiling."""
    tasks = [_task("t1", "a", _binary("a"))]
    seen = []

    def solver(payload, lessons):
        seen.append(len(lessons))
        return payload

    naive_lessons = [_Lo("x" * 100)] * 5   # naive dumps everything
    warm_lessons = [_Lo("y")]

    run(tasks, solver=solver, seed=0, recall=lambda t: naive_lessons,
        arm=COLD_NAIVE, budget=100)
    run(tasks, solver=solver, seed=0, recall=lambda t: warm_lessons,
        arm=WARM, budget=100)

    assert seen[0] == 1   # naive's 500 chars of lessons get capped to 1 at budget=100
    assert seen[1] == 1   # warm's single short lesson fits untouched


def test_run_is_logged_one_row_per_task(tmp_path):
    log = tmp_path / "run.jsonl"
    tasks = [_task("t1", "a", _binary("a")), _task("t2", "b", _binary("b"))]
    run(tasks, solver=lambda payload, lessons: payload, seed=7, log_path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["arm"] == "cold"
    assert row["seed"] == 7
    assert row["task_id"] == "t1"
