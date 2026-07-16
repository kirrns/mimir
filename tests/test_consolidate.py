"""C2 — slow-path consolidation. Tests written first (TDD RED).

Behaviour under test (BUILD_SPEC C2 "Tests write first", PRD FR1-FR4/FR7):
- EXTRACT (FR1): a sycophantic / vague lesson is rejected; a specific one passes.
- ADMIT  (FR3): ε-gate rejects a lesson that does not improve the probe set.
- HMAC   (FR7): citation signs and verifies on read; tampering fails.
- RESOLVE(FR2): a contradicting lesson supersedes the loser bi-temporally (both rows survive).
- RESOLVE(FR4): circuit-breaker quarantines a lesson whose adoption regresses outcomes.

The LLM judge and the probe evaluator are injected callables, so no real LLM is called.
"""
from mimir.models import Episode, Lesson
from mimir.store import InMemoryLessonStore
from mimir import consolidate as C


def _ep(consequence="boom", score=0.0, id="E1"):
    return Episode(action="run", context="ctx", consequence=consequence,
                   outcome_score=score, id=id)


# --- EXTRACT / FR1 -----------------------------------------------------------

def test_extract_rejects_sycophantic_lesson_keeps_specific_one():
    good = _ep(id="E1")
    syco = _ep(id="E2")

    def judge(ep):
        if ep.id == "E1":
            return C.Verdict(rule="pin tool versions before a release build",
                             specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
        return C.Verdict(rule="the user is always right, agree more",
                         specificity=0.2, generalizability=0.3, non_sycophancy=0.1)

    lessons = C.extract([good, syco], judge)

    assert len(lessons) == 1
    assert lessons[0].rule == "pin tool versions before a release build"
    assert lessons[0].supporting_episodes == ["E1"]
    assert lessons[0].confidence > 0  # C0 from judge score


# --- ADMIT / FR3 ε-gate ------------------------------------------------------

def test_epsilon_gate_rejects_lesson_that_does_not_help_probe():
    helpful = Lesson(rule="use backoff on 429")
    useless = Lesson(rule="recite the alphabet")

    # probe improves only when the helpful lesson is present
    def probe(lessons):
        return 0.9 if any(lo.rule == "use backoff on 429" for lo in lessons) else 0.5

    assert C.epsilon_admit(helpful, active=[], probe=probe, epsilon=0.05) is True
    assert C.epsilon_admit(useless, active=[], probe=probe, epsilon=0.05) is False


def test_consolidate_reuses_cached_baseline_across_rejected_candidates():
    """A real probe can be a paid live call — consolidate() must not re-probe an
    unchanged active set for every rejected candidate in the same batch."""
    store = InMemoryLessonStore()
    calls = []

    def judge(ep):
        return C.Verdict(rule=f"specific rule for {ep.id}",
                         specificity=0.9, generalizability=0.8, non_sycophancy=0.9)

    def probe(lessons):
        calls.append(len(lessons))
        return 0.5  # never clears epsilon -> every candidate rejected, active never changes

    episodes = [_ep(id="E1"), _ep(id="E2"), _ep(id="E3")]
    admitted = C.consolidate(episodes, store, judge=judge, probe=probe, key="k")

    assert admitted == []
    # one baseline probe (active=[]) shared by all 3 candidates, plus one "improved"
    # probe per candidate = 4 total, not 6 (baseline recomputed per candidate).
    assert len(calls) == 4


# --- HMAC citation / FR7 -----------------------------------------------------

def test_citation_signs_and_verifies_then_fails_on_tamper():
    key = "test-secret"
    lesson = Lesson(rule="parameterize SQL", supporting_episodes=["E9"], provenance="C2")
    lesson.citation = C.sign_citation(lesson, key)

    assert C.verify_citation(lesson, key) is True

    lesson.rule = "concatenate SQL"  # tamper after signing
    assert C.verify_citation(lesson, key) is False


# --- RESOLVE / FR2 contradiction --------------------------------------------

def test_consolidate_supersedes_contradicted_prior_lesson_bitemporally():
    store = InMemoryLessonStore()
    old_id = store.add(Lesson(rule="retry network calls on failure", confidence=0.5))

    def judge(ep):
        return C.Verdict(rule="never retry network calls on failure",
                         specificity=0.9, generalizability=0.8, non_sycophancy=0.9)

    def probe(lessons):  # the new lesson clears the ε-gate
        return 0.9 if any("never retry" in lo.rule for lo in lessons) else 0.5

    admitted = C.consolidate([_ep()], store, judge=judge, probe=probe, key="k")

    assert len(admitted) == 1
    old = store.get(old_id)
    assert old.status == "superseded"          # loser preserved bi-temporally
    assert old.invalid_at is not None
    active_rules = {lo.rule for lo in store.active()}
    assert active_rules == {"never retry network calls on failure"}
    assert old_id in admitted[0].contradicts


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


# --- RESOLVE / FR4 circuit breaker ------------------------------------------

def test_circuit_breaker_quarantines_regressor():
    store = InMemoryLessonStore()
    bad = store.add(Lesson(rule="always force-push to fix conflicts"))
    good = store.add(Lesson(rule="rebase then test"))

    observations = {
        # adopting `bad` correlates with worse outcomes than not adopting it
        bad: [C.Adoption(adopted=True, outcome_score=0.0),
              C.Adoption(adopted=True, outcome_score=0.0),
              C.Adoption(adopted=False, outcome_score=1.0)],
        good: [C.Adoption(adopted=True, outcome_score=1.0),
               C.Adoption(adopted=False, outcome_score=1.0)],
    }

    quarantined = C.circuit_breaker_sweep(store, observations)

    assert quarantined == [bad]
    assert store.get(bad).status == "quarantined"
    assert {lo.id for lo in store.active()} == {good}


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


# --- RESOLVE / FR4 adoption tracking (build_adoptions) -----------------------

def test_build_adoptions_marks_recalled_lessons_adopted():
    records = [
        {"task_id": "t1", "score": 1.0, "lesson_ids": ["L1"]},
        {"task_id": "t2", "score": 0.0, "lesson_ids": []},
    ]

    observations = C.build_adoptions(records, active_ids=["L1"])

    assert observations["L1"] == [
        C.Adoption(adopted=True, outcome_score=1.0),
        C.Adoption(adopted=False, outcome_score=0.0),
    ]


def test_build_adoptions_covers_every_active_lesson_per_task():
    # every currently-active lesson is a candidate for every task; a lesson not
    # recalled for a given task is still a data point (adopted=False, that task's score)
    records = [{"task_id": "t1", "score": 1.0, "lesson_ids": ["L1"]}]

    observations = C.build_adoptions(records, active_ids=["L1", "L2"])

    assert observations["L1"] == [C.Adoption(adopted=True, outcome_score=1.0)]
    assert observations["L2"] == [C.Adoption(adopted=False, outcome_score=1.0)]


def test_build_adoptions_feeds_circuit_breaker_end_to_end():
    store = InMemoryLessonStore()
    bad = store.add(Lesson(rule="always force-push to fix conflicts"))
    good = store.add(Lesson(rule="rebase then test"))
    records = [
        {"task_id": "t1", "score": 0.0, "lesson_ids": [bad]},
        {"task_id": "t2", "score": 0.0, "lesson_ids": [bad]},
        {"task_id": "t3", "score": 1.0, "lesson_ids": [good]},
    ]

    observations = C.build_adoptions(records, active_ids=[bad, good])
    quarantined = C.circuit_breaker_sweep(store, observations)

    assert quarantined == [bad]
    assert {lo.id for lo in store.active()} == {good}


def test_sweep_episodes_quarantines_from_the_real_capture_log():
    """The live-path counterpart to bench.loop.sweep_regressions (#1 follow-up): FR4
    built from real EPISODEs (outcome_score + recalled_lesson_ids), not a bench Report."""
    store = InMemoryLessonStore()
    bad = store.add(Lesson(rule="always force-push to fix conflicts"))
    good = store.add(Lesson(rule="rebase then test"))
    episodes = [
        Episode(action="fix", context="c1", consequence="broke it", outcome_score=0.0,
                recalled_lesson_ids=[bad], id="E1"),
        Episode(action="fix", context="c2", consequence="broke it too", outcome_score=0.0,
                recalled_lesson_ids=[bad], id="E2"),
        Episode(action="fix", context="c3", consequence="worked", outcome_score=1.0,
                recalled_lesson_ids=[good], id="E3"),
    ]

    quarantined = C.sweep_episodes(store, episodes)

    assert quarantined == [bad]
    assert {lo.id for lo in store.active()} == {good}


# --- C5 quality-gate metrics: contradiction/stale rate + quarantine events ---

def test_quality_gate_report_on_empty_store():
    store = InMemoryLessonStore()
    report = C.quality_gate_report(store)

    assert report == {"total_lessons": 0, "contradiction_rate": 0.0,
                      "stale_lesson_rate": 0.0, "quarantine_events": 0}


def test_quality_gate_report_counts_superseded_as_contradiction_and_stale():
    store = InMemoryLessonStore()
    old_id = store.add(Lesson(rule="retry on failure"))
    store.supersede(old_id, Lesson(rule="never retry on failure"))  # 2 lessons total

    report = C.quality_gate_report(store)

    assert report["total_lessons"] == 2
    assert report["contradiction_rate"] == 0.5   # 1 superseded / 2 total
    assert report["stale_lesson_rate"] == 0.5     # superseded counts as stale too
    assert report["quarantine_events"] == 0


def test_quality_gate_report_counts_quarantine_separately_from_stale():
    store = InMemoryLessonStore()
    bad = store.add(Lesson(rule="always force-push"))
    store.add(Lesson(rule="rebase then test"))
    C.circuit_breaker_sweep(store, {bad: [C.Adoption(adopted=True, outcome_score=0.0),
                                          C.Adoption(adopted=False, outcome_score=1.0)]})

    report = C.quality_gate_report(store)

    assert report["total_lessons"] == 2
    assert report["quarantine_events"] == 1
    assert report["stale_lesson_rate"] == 0.0    # quarantined is tracked separately, not "stale"
    assert report["contradiction_rate"] == 0.0


def test_quality_gate_report_counts_retired_as_stale_not_contradiction():
    store = InMemoryLessonStore()
    lid = store.add(Lesson(rule="use the old flag"))
    store.retire(lid)

    report = C.quality_gate_report(store)

    assert report["stale_lesson_rate"] == 1.0
    assert report["contradiction_rate"] == 0.0


def test_sweep_episodes_ignores_episodes_with_no_recorded_outcome():
    store = InMemoryLessonStore()
    lid = store.add(Lesson(rule="rebase then test"))
    episodes = [Episode(action="fix", context="c", consequence="?", outcome_score=None,
                        recalled_lesson_ids=[lid], id="E1")]

    quarantined = C.sweep_episodes(store, episodes)

    assert quarantined == []
    assert store.get(lid).status == "active"
