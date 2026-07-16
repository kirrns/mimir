"""C2 — slow-path consolidation ("dreaming").

Turns raw EPISODEs into admitted, attributed, gated LESSONs. Off the hot path:
batch/cron or manual `consolidate(since)`. Each stage is independently testable.

Pipeline (BUILD_SPEC C2):
  EXTRACT (FR1)  judge specificity/generalizability/non-sycophancy -> C0 confidence
  ADMIT   (FR3)  ε-gate counterfactual sufficiency + HMAC citation  (FR7)
  RESOLVE (FR2)  contradiction -> bi-temporal supersede   (FR4) circuit-breaker sweep
  WRITE          store.add / store.supersede

The LLM judge and the held-out probe are *injected callables* — the heavy/LLM
parts live at the call site, so this module is pure and unit-testable offline.
Per BUILD_SPEC cut-line #1, attribution is the plain single-lesson counterfactual
that the ε-gate already performs (TracLLM/CAR cascade is a later add).
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from functools import lru_cache
from statistics import mean
from typing import Callable, Iterable

from mimir.models import QUARANTINED, RETIRED, SUPERSEDED, Episode, Lesson

EPSILON = 0.05          # FR3: min probe improvement to admit a lesson
JUDGE_THRESHOLD = 0.5   # FR1: min per-criterion judge score to keep a lesson


# --- EXTRACT / FR1 -----------------------------------------------------------

@dataclass
class Verdict:
    """LLM-as-judge output over one EPISODE (FR1). Score = initial confidence C0."""

    rule: str
    specificity: float        # actionable, not "be careful"
    generalizability: float   # beyond the one instance
    non_sycophancy: float     # genuine task insight, not user-affirmation

    @property
    def confidence(self) -> float:
        return mean((self.specificity, self.generalizability, self.non_sycophancy))

    def passes(self, threshold: float = JUDGE_THRESHOLD) -> bool:
        # weakest criterion must clear the bar — a sycophantic lesson fails on non_sycophancy
        return min(self.specificity, self.generalizability, self.non_sycophancy) >= threshold


Judge = Callable[[Episode], Verdict]


def extract(episodes: Iterable[Episode], judge: Judge,
            *, threshold: float = JUDGE_THRESHOLD) -> list[Lesson]:
    """FR1: keep only specific, generalizable, non-sycophantic lessons; C0 from the judge."""
    lessons: list[Lesson] = []
    for ep in episodes:
        v = judge(ep)
        if not v.passes(threshold):
            continue
        lessons.append(Lesson(
            rule=v.rule,
            confidence=v.confidence,
            supporting_episodes=[ep.id] if ep.id else [],
            provenance="C2.extract",
        ))
    return lessons


# --- ADMIT / FR3 ε-gate + FR7 HMAC ------------------------------------------

Probe = Callable[[list[Lesson]], float]


def epsilon_admit(lesson: Lesson, active: list[Lesson], probe: Probe,
                  *, epsilon: float = EPSILON, baseline: float | None = None) -> bool:
    """FR3: admit only if the held-out probe set improves by >= ε (counterfactual sufficiency).

    `baseline` lets a caller reuse a `probe(active)` score across several candidate
    lessons scored against the same unchanged active set — a real probe can be a live
    LLM/solver call (`bench.claude_judge.make_solver_probe` costs 2x|held_out| calls
    per invocation), so re-probing an unchanged baseline for every rejected candidate
    wastes real calls. Left None, this recomputes it (unchanged standalone behaviour).
    """
    if baseline is None:
        baseline = probe(active)
    improved = probe([*active, lesson])
    return improved - baseline >= epsilon


def _canonical(lesson: Lesson) -> bytes:
    # sign the integrity-bearing fields; confidence/citation/status are excluded (they move)
    parts = [lesson.rule, lesson.provenance, *sorted(lesson.supporting_episodes)]
    return "\x00".join(parts).encode("utf-8")


def _key_bytes(key: str | bytes) -> bytes:
    return key if isinstance(key, bytes) else key.encode("utf-8")


def sign_citation(lesson: Lesson, key: str | bytes) -> str:
    """HMAC-SHA-256 over the lesson's integrity fields (FR7 provenance integrity)."""
    return hmac.new(_key_bytes(key), _canonical(lesson), hashlib.sha256).hexdigest()


def verify_citation(lesson: Lesson, key: str | bytes) -> bool:
    """Constant-time check that lesson.citation still matches its content."""
    return hmac.compare_digest(sign_citation(lesson, key), lesson.citation)


# --- RESOLVE / FR2 contradiction --------------------------------------------

_NEG = {"not", "never", "no", "dont", "don't", "avoid", "without", "stop"}
# Function words carry no topic signal; counting them inflates overlap and causes
# unrelated lessons (e.g. "str.strip" vs "str(x)") to register as contradictions.
_STOP = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "with",
         "by", "is", "are", "be", "as", "at", "it", "use", "using", "from",
         "into", "that", "this", "if", "else", "do", "than", "then"}
_WORD = re.compile(r"[a-z0-9']+")


# ponytail: bounded cache -- rule text is immutable once written, so caching by
# string is safe. maxsize must stay comfortably above the active-lesson count
# or LRU eviction thrashes and makes calls slower than uncached (measured);
# 20k is far past any realistic v1 store size -- raise further if that changes.
@lru_cache(maxsize=20000)
def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def detect_contradiction(a: Lesson, b: Lesson, *, topic_overlap: int = 2) -> bool:
    """FR2 write-time gate: same topic (content-word overlap) + opposing negation polarity.

    ponytail: lexical heuristic with a known ceiling — swap for an LLM topic+negation
    judge if false-positive rate bites. Stopwords are excluded so incidental function-word
    overlap doesn't flag unrelated lessons. Keeps the bi-temporal supersede path testable now.
    """
    ta, tb = _tokens(a.rule), _tokens(b.rule)
    shared = (ta & tb) - _NEG - _STOP
    if len(shared) < topic_overlap:
        return False
    return bool(ta & _NEG) != bool(tb & _NEG)


# --- RESOLVE / FR4 circuit breaker ------------------------------------------

@dataclass
class Adoption:
    """One observation: was the lesson adopted on a task, and what was the outcome."""

    adopted: bool
    outcome_score: float


def circuit_breaker_sweep(store, observations: dict[str, list[Adoption]],
                          *, margin: float = 0.0) -> list[str]:
    """FR4: quarantine any active lesson whose adoption correlates with outcome regressions."""
    quarantined: list[str] = []
    for lesson in store.active():
        if lesson.protected:
            continue
        obs = observations.get(lesson.id, [])
        with_l = [o.outcome_score for o in obs if o.adopted]
        without = [o.outcome_score for o in obs if not o.adopted]
        if with_l and without and mean(with_l) < mean(without) - margin:
            lesson.status = QUARANTINED  # store.active() filters it out from here on
            quarantined.append(lesson.id)
    return quarantined


def build_adoptions(records: Iterable[dict], active_ids: Iterable[str]) -> dict[str, list[Adoption]]:
    """Turn a WARM Report's records (bench.harness.run, each carrying 'lesson_ids' + 'score')
    into the per-lesson Adoption observations circuit_breaker_sweep needs.

    Every currently-active lesson is a candidate for every task: adopted=True on the
    tasks that actually recalled it, adopted=False (with that task's own score) on the
    ones that didn't. A lesson recalled for none of the tasks gets no with_l samples and
    is left alone by circuit_breaker_sweep -- silence isn't evidence of regression.
    """
    records = list(records)
    observations: dict[str, list[Adoption]] = {lid: [] for lid in active_ids}
    for r in records:
        recalled = set(r.get("lesson_ids", ()))
        score = r["score"]
        for lid in observations:
            observations[lid].append(Adoption(adopted=lid in recalled, outcome_score=score))
    return observations


def sweep_episodes(store, episodes: Iterable) -> list[str]:
    """FR4 over the real capture log: live-path counterpart to bench.loop.sweep_regressions,
    built from EPISODE.outcome_score + EPISODE.recalled_lesson_ids instead of a bench
    Report's records. Episodes with no recorded outcome (still unscored) carry no signal.
    """
    active_ids = [lo.id for lo in store.active()]
    records = [{"score": ep.outcome_score, "lesson_ids": ep.recalled_lesson_ids}
              for ep in episodes if ep.outcome_score is not None]
    observations = build_adoptions(records, active_ids)
    return circuit_breaker_sweep(store, observations)


# --- C5 quality-gate reporting -----------------------------------------------

def quality_gate_report(store) -> dict:
    """C5 headline quality gate: contradiction rate (FR2 supersede), stale-lesson
    rate (superseded + retired), and quarantine events (FR4), over every lesson
    the store has ever held (`store.all()`, not just active ones).
    """
    lessons = store.all()
    total = len(lessons)
    superseded = sum(1 for lo in lessons if lo.status == SUPERSEDED)
    retired = sum(1 for lo in lessons if lo.status == RETIRED)
    quarantined = sum(1 for lo in lessons if lo.status == QUARANTINED)
    return {
        "total_lessons": total,
        "contradiction_rate": superseded / total if total else 0.0,
        "stale_lesson_rate": (superseded + retired) / total if total else 0.0,
        "quarantine_events": quarantined,
    }


# --- Orchestration -----------------------------------------------------------

def consolidate(episodes: Iterable[Episode], store, judge: Judge, probe: Probe,
                key: str | bytes, *, epsilon: float = EPSILON,
                threshold: float = JUDGE_THRESHOLD) -> list[Lesson]:
    """Full slow path: EXTRACT -> ADMIT(ε-gate + HMAC) -> RESOLVE(contradiction) -> WRITE."""
    admitted: list[Lesson] = []
    candidates = extract(episodes, judge, threshold=threshold)
    if not candidates:
        return admitted

    # probe(active) is invariant while the store is unmutated, so it's cached across
    # candidates and only re-probed after an admit actually changes `active` (see
    # epsilon_admit's docstring — matters when probe is a real, paid LLM/solver call).
    active = store.active()
    baseline = probe(active)
    for lesson in candidates:
        if not epsilon_admit(lesson, active, probe, epsilon=epsilon, baseline=baseline):
            continue
        loser = next((a for a in active if not a.protected and detect_contradiction(a, lesson)),
                     None)
        lesson.citation = sign_citation(lesson, key)
        if loser is not None:
            store.supersede(loser.id, lesson)  # bi-temporal: loser kept, marked invalid
        else:
            store.add(lesson)
        admitted.append(lesson)
        active = store.active()
        baseline = probe(active)  # store mutated; refresh for the next candidate
    return admitted
