"""C5 integration — the end-to-end "Mimir runs on Mimir" loop. Tests first (TDD RED).

Proves the centerpiece deterministically (no live LLM, no tokens): the only
variable across arms is Mimir.
- COLD (memory-off) and COLD+naive (ungated context-stuffing) both fail; WARM
  (confidence-gated recall) succeeds — so the *gate* is what wins, not raw recall.
- the loop actually runs C1 capture -> C2 consolidate -> C4 recall (not pre-seeded).
- attribution headline: ablating the one fix-lesson drops the WARM score (its credit).
- safety: the poisoned/quarantined lesson is excluded from WARM recall.
"""
from bench import loop
from bench.harness import COLD, COLD_NAIVE, WARM, ablation_credit
from mimir.store import InMemoryLessonStore
from mimir.mcp_server import recall


def test_warm_beats_cold_and_naive_on_handbuilt_tasks():
    store = InMemoryLessonStore()
    loop.seed_poison(store)  # a known-bad lesson lives in the store (poisoning demo)

    cold, naive, warm = loop.run_loop(store, loop.TASKS, key="k", seed=0)

    assert cold.arm == COLD and naive.arm == COLD_NAIVE and warm.arm == WARM
    assert cold.success_rate == 0.0          # no guidance
    assert naive.success_rate == 0.0         # stuffed with the trap -> misled
    assert warm.success_rate == 1.0          # gated recall -> only the good lesson
    assert warm.success_rate > naive.success_rate > cold.success_rate - 0.0001


def test_loop_admits_the_fix_lesson_via_consolidation():
    store = InMemoryLessonStore()
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    # the consolidation step (C2) wrote the fix lessons into the store
    learned = {lo.rule for lo in store.active()}
    for task in loop.TASKS:
        assert task.fix_rule in learned


def test_ablation_credits_the_fix_lesson():
    store = InMemoryLessonStore()
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    task = loop.TASKS[0]
    lessons = recall(store, task.prompt).lessons
    fix = next(lo for lo in lessons if lo.rule == task.fix_rule)

    credit = ablation_credit(task, lessons, fix.id, loop.scripted_solver, seed=0)
    assert credit == 1.0  # removing it flips the task from solved to failed


def test_poison_admission_demo_rejects_injected_poisoned_episode():
    """C5 safety demo (b): distinct from seed_poison (which force-inserts an
    already-quarantined lesson, proving only that recall filters it). Here a
    poisoned EPISODE is fed through the real consolidate() pipeline -- the
    ε-gate (FR3) itself must reject it, because the probe set never rewards a
    trap rule, so the poison never becomes an admitted, active LESSON at all."""
    result = loop.poison_admission_demo()
    assert result == {"admitted": 0, "active": 0}


def test_poisoned_lesson_excluded_from_warm_recall():
    store = InMemoryLessonStore()
    loop.seed_poison(store)
    loop.run_loop(store, loop.TASKS, key="k", seed=0)
    task = loop.TASKS[0]
    recalled = {lo.rule for lo in recall(store, task.prompt).lessons}
    assert task.trap_rule not in recalled


# --- FR4 circuit breaker over a real WARM run (the SEAL-gap: forgetting/regression) ---

# A lesson that legitimately fixes task A also gets lexically recalled for task B
# (shared vocabulary), where it actively misleads the solver (it IS task B's trap).
# No pre-seeded poison: this lesson passes ADMIT (C2's ε-gate) same as any other
# lesson -- the regression only shows up once it's adopted across real WARM tasks.
SWEEP_TASKS = [
    loop.BugTask(
        id="s-a", prompt="alpha uses zzzshared token pattern",
        fix_rule="R1 zzzshared token pattern fixes alpha",
        trap_rule="unused-trap-a",
    ),
    loop.BugTask(
        id="s-b", prompt="beta uses zzzshared token pattern too",
        fix_rule="R2 totally unrelated filler text nobody recalls",
        trap_rule="R1 zzzshared token pattern fixes alpha",  # == s-a's fix: misapplied here
    ),
    loop.BugTask(
        id="s-c", prompt="gamma has an entirely different db index issue",
        fix_rule="R3 rebuild the db index for gamma",
        trap_rule="unused-trap-c",
    ),
]


def test_sweep_regressions_quarantines_a_lesson_that_regresses_other_tasks():
    store = InMemoryLessonStore()
    cold, naive, warm = loop.run_loop(store, SWEEP_TASKS, key="k", seed=0)
    assert warm.success_rate < 1.0  # sanity: s-b really did fail, misled by s-a's fix

    quarantined_rules = {
        lo.rule for lo in store.all()
        if lo.id in loop.sweep_regressions(store, warm) and lo.status == "quarantined"
    }

    assert quarantined_rules == {"R1 zzzshared token pattern fixes alpha"}
    active_rules = {lo.rule for lo in store.active()}
    assert "R2 totally unrelated filler text nobody recalls" in active_rules
    assert "R3 rebuild the db index for gamma" in active_rules
