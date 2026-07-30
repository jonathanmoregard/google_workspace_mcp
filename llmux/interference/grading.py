"""Shared grading machinery for the interference corpus.

Three jobs, and the order matters:

1. **Harness gate.** Before a single word is said about the agent, check that
   the interleaving itself behaved: every declared interference fired, none of
   them broke a spec invariant, and none of them failed to apply. A scenario
   whose second editor never showed up proves nothing, and an agent must never
   be marked down for our bug. :func:`harness_gate` returns failures prefixed
   ``HARNESS:`` and the caller returns immediately on any of them.

2. **Expected end state, computed WITH the other editor's operations
   applied.** :func:`expected_backend` replays the scenario's own seed and
   then the timeline -- the other editor's interferences and the agent's
   correct actions, in the same op vocabulary -- through
   :func:`mockdocs.concurrency.replay`. Nothing is hand-written, so changing
   an interference cannot leave a stale expected string behind, and the
   expectation is by construction reachable.

3. **Credit for adaptation, not for luck.** :class:`Scorecard` scores the end
   state; :func:`adaptation_failures` scores the behaviour. The distinction
   the corpus is built to make is between an agent that noticed the document
   had changed, re-read it, and acted on what is actually there, and one that
   hammered the same dead id again. The second is *blind retry* and it is
   explicitly not credited even when the end state happens to come out right.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from llmux.runner.interference import InterferenceReport
from mockdocs.concurrency import replay


@dataclass
class Scorecard:
    """Accumulates checks so a partial score means something.

    ``score`` is the share of checks passed, which is what makes a run that
    got the text right but blind-retried its way there score below one
    instead of simply failing -- the report can then rank near-misses.
    """

    checks: int = 0
    passes: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, ok: bool, failure: str) -> bool:
        self.checks += 1
        if ok:
            self.passes += 1
        else:
            self.failures.append(failure)
        return ok

    def skip(self, failure: str) -> None:
        """Record a check that could not be evaluated as a failure."""
        self.checks += 1
        self.failures.append(failure)

    def absorb(self, failures: Sequence[str]) -> None:
        """Fold a list of failures in: one check each, or one passing check
        when the list is empty."""
        if not failures:
            self.checks += 1
            self.passes += 1
            return
        for failure in failures:
            self.check(False, failure)

    def result(self) -> dict[str, Any]:
        return {
            "pass": not self.failures,
            "score": (self.passes / self.checks) if self.checks else 0.0,
            "failures": list(self.failures),
        }


def seed_of(grade_file: str) -> dict[str, Any]:
    """The scenario's own ``seed.json``, read next to its ``grade.py``."""
    return json.loads(
        Path(grade_file).with_name("seed.json").read_text(encoding="utf-8")
    )


def expected_backend(grade_file: str, timeline: Sequence[dict[str, Any]]) -> Any:
    """Seed, then replay ``timeline``; the end state the run should reach."""
    return replay(seed_of(grade_file), timeline)


def expected_doc(
    grade_file: str, timeline: Sequence[dict[str, Any]], document_id: str
) -> Any:
    return expected_backend(grade_file, timeline).documents[document_id]


def harness_gate(
    backend: Any, must_fire: Sequence[str]
) -> tuple[Optional[InterferenceReport], list[str]]:
    """Verify the interleaving before judging the agent.

    Returns ``(report, failures)``. A non-empty ``failures`` means this run
    measured nothing: return it straight out of ``grade()`` so the batch
    report shows a harness fault rather than a failing agent.
    """
    report = InterferenceReport.from_backend(backend)
    if report is None:
        return None, [
            "HARNESS: the end state carries no interference record, so the "
            "scripted second editor never ran (is MOCKDOCS_INTERFERENCE set?)"
        ]
    failures = [
        f"HARNESS: the scripted second editor broke a spec invariant: {v}"
        for v in report.violations
    ]
    fired = set(report.fired_names)
    for name in must_fire:
        if name not in fired:
            failures.append(
                f"HARNESS: interference {name!r} never fired, so this run never "
                "met the concurrent change it exists to measure"
            )
    for name, why in report.ineffective:
        failures.append(
            f"HARNESS: interference {name!r} fired but changed nothing ({why}), "
            "so this run measured an undisturbed document"
        )
    return report, failures


def adaptation_failures(
    report: InterferenceReport,
    *,
    require_reread: bool = True,
    forbid_stale_writes: bool = True,
) -> list[str]:
    """The behaviour half of the grade: detect, re-read, adapt -- not retry.

    Deliberately mechanical and deliberately narrow. It cannot read the
    agent's prose, so "report honestly" is judged by the taxonomy from the
    transcript; what is checkable from the server's own call log is whether
    the run went and looked after the document moved, and whether it repeated
    a call it had already been told was dead.
    """
    failures: list[str] = []
    if require_reread and not report.reread_after_change():
        failures.append(
            f"the document changed under the run at agent call "
            f"{report.first_fire} and it never read the document again: it "
            "finished on a picture it had no reason to trust"
        )
    for first, second in report.blind_retries():
        failures.append(
            f"blind retry: {second.tool} (agent call {second.ordinal}) repeated "
            f"the call that had already failed at call {first.ordinal}, without "
            "reading the document in between to find out what changed"
        )
    if forbid_stale_writes:
        for call, shift in report.stale_index_writes():
            failures.append(
                f"{call.tool} (agent call {call.ordinal}) wrote with indexes "
                f"computed before another editor moved the text by {shift:+d} "
                "UTF-16 units, and never re-read in between: the write "
                "succeeded and landed in the wrong place"
            )
    return failures


def mine(doc: Any, me: str) -> list[str]:
    """Ids of the pending suggestions authored by the agent."""
    return sorted(sid for sid, s in doc.registry.items() if s.author == me)


def replies_by(doc: Any, suggestion_id: str, author: str) -> list[str]:
    sug = doc.registry.get(suggestion_id)
    if sug is None:
        return []
    return [p.content for p in sug.thread if p.author == author]
