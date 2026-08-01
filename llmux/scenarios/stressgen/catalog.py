"""The stress corpus: four review sets at 30, 60, 90 and 120 suggestions.

The existing 16-scenario ladder saturated -- 32 of 32 agent runs passed, so
it discriminates nothing. These four exist to find the ceiling, and they
vary the one thing the old corpus held constant: *volume*. Same tool
surface, same algebra, same kind of task; 4x, 8x, 12x, 16x the cards.

Each scenario is a real editorial pass over a real article, with a rule list
of the kind a managing editor actually writes. The rules are ordered and
first-match-wins, and every one of them is decidable from what
``list_document_suggestions`` returns: who wrote it, what kind of card it
is, where in the document it sits, whether anyone else touched the same
characters. Nothing requires the agent to guess.

Ground truth is never authored. The rules are evaluated against the seeded
registry to produce an assignment; SPEC L5 turns the assignment into a
document; the oracle applies it through the real MCP tools; the two must
agree. Partial credit is per card, from
:mod:`llmux.scenarios.stressgen.invariants`, so a run that gets 90 of 120
right scores like a run that got 90 of 120 right.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mockdocs.model import MockDoc

from llmux.scenarios.catalog import Scenario
from llmux.scenarios.primitives import (
    Predicate,
    Rule,
    ScenarioError,
    by_author,
    decide,
    grapheme_spans,
    kind_is,
    ordered_suggestion_ids,
    p_and,
    p_or,
    seeded_backend,
)
from llmux.scenarios.steps import Accept, Reject, Step
from llmux.scenarios.stressgen import prose
from llmux.scenarios.stressgen.invariants import (
    pending_checks,
    project,
    witness_checks,
)
from llmux.scenarios.stressgen.prose import Document
from llmux.scenarios.stressgen.walk import PANEL, Walk, run_walk

#: Tier name used in ``meta.json``; the runner buckets on it.
TIER = "stress"


class StressBuildError(RuntimeError):
    """A stress scenario could not be built into a fair, solvable task."""


# ---------------------------------------------------------------------------
# extra predicates -- all decidable from list_document_suggestions output
# ---------------------------------------------------------------------------


def _marked(doc: MockDoc, sid: str) -> set[int]:
    return {i for i, c in enumerate(doc.chars) if sid in c.marks}


def in_section(document: Document, heading: str) -> Predicate:
    """Marks at least one character inside the named section.

    The section runs from its heading to the next one. Headings are never
    edited (the sampler excludes them), so they stay contiguous in the
    display text and the boundary is unambiguous for the agent too: it can
    read the headings out of ``get_doc_review_view`` and compare indexes.
    """
    order = list(document.headings)
    if heading not in order:
        raise ScenarioError(f"{document.key} has no heading {heading!r}")
    position = order.index(heading)
    following = order[position + 1] if position + 1 < len(order) else None

    def pred(doc: MockDoc, sid: str) -> bool:
        start = grapheme_spans(doc, heading)[0][0]
        end = (
            grapheme_spans(doc, following)[0][0]
            if following is not None
            else len(doc.chars)
        )
        return any(start <= i < end for i in _marked(doc, sid))

    return pred


def overlaps_other_author() -> Predicate:
    """Shares at least one character with another reviewer's suggestion.

    This is the card a reviewer has to look twice at: two people edited the
    same words, so accepting both is not the same as accepting either.
    """

    def pred(doc: MockDoc, sid: str) -> bool:
        author = doc.registry[sid].author
        for i in _marked(doc, sid):
            for other in doc.chars[i].marks:
                if other == sid:
                    continue
                entry = doc.registry.get(other)
                if entry is not None and entry.author != author:
                    return True
        return False

    return pred


def strikes_at_least(chars: int) -> Predicate:
    """Removes ``chars`` or more characters of the existing text.

    Visible to the agent as the length of the struck side of the card
    (``pre_text``, or the index span of a deletion).
    """

    def pred(doc: MockDoc, sid: str) -> bool:
        return len(doc.struck(sid)) >= chars

    return pred


# ---------------------------------------------------------------------------
# task definitions
# ---------------------------------------------------------------------------


RuleFactory = Callable[[Document], list[Rule]]


class Task:
    """A rule list plus the English the brief states it in.

    The two have to say the same thing; that correspondence is the only part
    of a scenario a human still has to get right, so it is written once,
    here, next to the rules it describes.
    """

    def __init__(
        self,
        title: str,
        preamble: str,
        clauses: list[tuple[str, Rule]],
        closing: str = "Leave every other suggestion pending. Do not resolve it either way.",
    ) -> None:
        self.title = title
        self.preamble = preamble
        self.clauses = clauses
        self.closing = closing

    @property
    def rules(self) -> list[Rule]:
        return [rule for _text, rule in self.clauses]

    def rules_markdown(self) -> str:
        lines = [
            f"{i}. {text}" for i, (text, _rule) in enumerate(self.clauses, start=1)
        ]
        return "\n".join(lines)


def _task_faq(document: Document) -> Task:
    return Task(
        title="Copyedit pass on the FAQ",
        preamble="""
Five people reviewed this FAQ: **priya** (copyeditor), **dana** (house
style), **marcus** (subject expert), **sam** (research assistant) and
**nadia** (managing editor). We are shipping priya's copyedits now and
holding the substantive changes for a second pass.

Apply these rules **in order** -- the first rule that matches a suggestion
decides it, and later rules do not apply to it:
""",
        clauses=[
            (
                'Reject every suggestion that touches the section headed "How '
                'confident are you in any of this?". That section is being '
                "rewritten separately and must not change.",
                Rule(
                    in_section(document, "How confident are you in any of this?"),
                    "reject",
                ),
            ),
            (
                "Accept every remaining suggestion by **priya**.",
                Rule(by_author("priya"), "accept"),
            ),
            (
                "Accept every remaining suggestion by **dana** whose card is a "
                "pure deletion (it only removes text and adds none).",
                Rule(p_and(by_author("dana"), kind_is("Delete")), "accept"),
            ),
        ],
    )


def _task_career(document: Document) -> Task:
    return Task(
        title="Style pass on the career review",
        preamble="""
Five people reviewed this career review: **priya** (copyeditor), **dana**
(house style), **marcus** (subject expert), **sam** (research assistant) and
**nadia** (managing editor). This pass lands the style fixes. Anything that
cuts real content, and anything in the closing recommendation, waits for the
author.

Apply these rules **in order** -- the first rule that matches a suggestion
decides it, and later rules do not apply to it:
""",
        clauses=[
            (
                'Reject every suggestion that touches the section headed "Our '
                'take". The author has not signed off on that section.',
                Rule(in_section(document, "Our take"), "reject"),
            ),
            (
                "Reject every remaining suggestion that removes 30 or more "
                "characters of the existing text. Those are content cuts, not "
                "style fixes.",
                Rule(strikes_at_least(30), "reject"),
            ),
            (
                "Accept every remaining suggestion by **priya** or **dana**.",
                Rule(p_or(by_author("priya"), by_author("dana")), "accept"),
            ),
        ],
    )


def _task_policy(document: Document) -> Task:
    return Task(
        title="Triage pass on the policy brief",
        preamble="""
Five people reviewed this brief: **priya** (copyeditor), **dana** (house
style), **marcus** (subject expert), **sam** (research assistant) and
**nadia** (managing editor). Several of them worked at the same time, so
some of their edits sit on top of each other. Anywhere that happened we want
a clean re-edit rather than a guess, so those go back.

Apply these rules **in order** -- the first rule that matches a suggestion
decides it, and later rules do not apply to it:
""",
        clauses=[
            (
                "Reject every suggestion that shares any characters with a "
                "suggestion by a **different** reviewer. Two people edited the "
                "same words, so neither edit is safe to take on its own.",
                Rule(overlaps_other_author(), "reject"),
            ),
            (
                "Reject every remaining suggestion that touches the section "
                'headed "Recommendations". Those are numbered and cited '
                "elsewhere, so they are frozen.",
                Rule(in_section(document, "Recommendations"), "reject"),
            ),
            (
                "Accept every remaining suggestion by **sam** or **priya** "
                "whose card only adds text and removes none.",
                Rule(
                    p_and(p_or(by_author("sam"), by_author("priya")), kind_is("Add")),
                    "accept",
                ),
            ),
            (
                "Accept every remaining suggestion by **priya** whose card both "
                "removes and adds text.",
                Rule(p_and(by_author("priya"), kind_is("Replace")), "accept"),
            ),
        ],
    )


def _task_research(document: Document) -> Task:
    return Task(
        title="Full review pass on the research summary",
        preamble="""
Five people reviewed this summary: **priya** (copyeditor), **dana** (house
style), **marcus** (subject expert), **sam** (research assistant) and
**nadia** (managing editor). This is the final pass before publication, so
every card needs a decision or a deliberate deferral.

Apply these rules **in order** -- the first rule that matches a suggestion
decides it, and later rules do not apply to it:
""",
        clauses=[
            (
                "Reject every suggestion that touches the section headed "
                '"Limitations of this evidence". That section was agreed with '
                "the authors and is frozen.",
                Rule(in_section(document, "Limitations of this evidence"), "reject"),
            ),
            (
                "Reject every remaining suggestion that shares any characters "
                "with a suggestion by a **different** reviewer.",
                Rule(overlaps_other_author(), "reject"),
            ),
            (
                "Reject every remaining suggestion that removes 30 or more "
                "characters of the existing text.",
                Rule(strikes_at_least(30), "reject"),
            ),
            (
                "Accept every remaining suggestion by **priya** or **sam**.",
                Rule(p_or(by_author("priya"), by_author("sam")), "accept"),
            ),
            (
                "Accept every remaining suggestion by **dana** whose card is a "
                "pure deletion (it only removes text and adds none).",
                Rule(p_and(by_author("dana"), kind_is("Delete")), "accept"),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


BRIEF_TEMPLATE = """# {title}

**Document ID:** `{document_id}`
**You are signed in as** `reviewer@example.com`. Anything you suggest, reply
or comment is authored by `reviewer`.

This document is a {words:,}-word article with **{count} pending
suggestions** on it from five reviewers, plus {comments} comment threads.
{preamble}

{rules}

{closing}

## Ground rules

- Work only through the document review tools. Read the document before you
  change it; suggestion ids are never given to you, you discover them.
- Every pending suggestion must be considered. A suggestion you never looked
  at is not "left pending" -- it is missed.
- Do exactly what is asked and nothing else: no new suggestions, comments or
  replies, and no accepts or rejects beyond the rules above.
- The rules are ordered. Rule 1 beats rule 2 beats rule 3; a suggestion
  decided by an earlier rule is not reconsidered by a later one.
"""


def _ordered_steps(
    doc: MockDoc, decisions: dict[str, str]
) -> tuple[list[Step], MockDoc, dict[str, str]]:
    """Card-order accept/reject steps, skipping anything already collected.

    Resolving one suggestion can garbage-collect another (§11.1 I2): accept
    a deletion that covers the whole of someone else's insertion and that
    insertion's last marked character is gone. The oracle must not then call
    ``manage_document_suggestion`` on it -- that is a hard 400 -- so the
    order is simulated first and the dead steps dropped.
    """
    simulated = doc.clone()
    steps: list[Step] = []
    applied: dict[str, str] = {}
    for sid in ordered_suggestion_ids(doc):
        action = decisions.get(sid)
        if action is None or sid not in simulated.registry:
            continue
        if action == "accept":
            simulated.accept(sid)
            steps.append(Accept(sid))
        else:
            simulated.reject(sid)
            steps.append(Reject(sid))
        applied[sid] = action
    return steps, simulated, applied


def build_stress_scenario(
    *,
    scenario_id: str,
    document_key: str,
    target: int,
    task_factory: Callable[[Document], Task],
    seed: int,
    comment_count: int,
    nest_count: int,
    sentence_cap: int = 2,
    overlap_fraction: float = 0.18,
    document_id: Optional[str] = None,
) -> Scenario:
    """Walk, decide, prove, and package one stress scenario.

    Raises :class:`StressBuildError` rather than emitting a scenario that is
    unfair or ungradeable -- a card the task says to leave alone that some
    other decision destroys, or an assignment the algebra and the model
    disagree about.
    """
    document = prose.BY_KEY[document_key]
    doc_id = document_id or f"mockdoc-stress-{target:03d}-{document_key}"
    walk: Walk = run_walk(
        document,
        seed=seed,
        target=target,
        panel=PANEL,
        document_id=doc_id,
        comment_count=comment_count,
        nest_count=nest_count,
        sentence_cap=sentence_cap,
        overlap_fraction=overlap_fraction,
    )
    seed_spec = walk.seed_spec()

    # Re-seed from the spec: the scenario ships the spec, so ground truth has
    # to be read off a document built the way the server will build it.
    _backend, doc = seeded_backend(seed_spec)
    if len(doc.registry) != target:
        raise StressBuildError(
            f"{scenario_id}: replaying the seed gave {len(doc.registry)} "
            f"suggestions, the walk had {target}"
        )

    task = task_factory(document)
    decisions = decide(doc, task.rules)
    steps, simulated, applied = _ordered_steps(doc, decisions)

    left_pending = [sid for sid in sorted(doc.registry) if sid not in decisions]
    if not left_pending:
        raise StressBuildError(
            f"{scenario_id}: the rules decide every suggestion, so the task "
            f"never tests restraint"
        )
    missing = [sid for sid in left_pending if sid not in simulated.registry]
    if missing:
        raise StressBuildError(
            f"{scenario_id}: {len(missing)} suggestion(s) the task leaves "
            f"pending are destroyed by other decisions ({missing[:3]}); the "
            f"task is unachievable as stated"
        )
    # A decided suggestion may vanish before the oracle reaches it, and that
    # is not a defect: rejecting an insertion also removes anything nested
    # inside it (§2's conjunctive ``ins``, spec §13.5), so the nested card is
    # garbage-collected and never needs resolving. The end state is identical
    # either way (L3/L5), and the agent that tries anyway meets the same 400 a
    # real reviewer would. Only an implausible number of them means the walk
    # went wrong.
    collected = len(decisions) - len(applied)
    if collected > max(4, target // 20):
        raise StressBuildError(
            f"{scenario_id}: {collected} decided suggestion(s) were "
            f"garbage-collected before the oracle reached them; that is more "
            f"cascade than the walk should produce -- pick another seed"
        )

    # L1/L3/L7: the algebra's prediction for this assignment must equal what
    # the model actually produces. Two implementations, one answer.
    predicted = project(doc, decisions)
    if predicted != simulated.display_text():
        raise StressBuildError(
            f"{scenario_id}: L5 projection and the model disagree about the "
            f"end state; that is a model bug, not a scenario bug"
        )

    extra_checks: list[dict[str, Any]] = []
    extra_checks.extend(pending_checks(left_pending))
    extra_checks.extend(witness_checks(doc, decisions, sorted(doc.registry)))
    extra_checks.append({"check": "suggestion_count", "equals": len(left_pending)})

    brief = BRIEF_TEMPLATE.format(
        title=task.title,
        document_id=doc_id,
        words=document.word_count,
        count=target,
        comments=walk.comments,
        preamble=task.preamble.strip(),
        rules=task.rules_markdown(),
        closing=task.closing,
    )

    return Scenario(
        id=scenario_id,
        tier=TIER,
        difficulty=5,
        tags=[
            "stress",
            "high-volume",
            "multi-author",
            "predicate",
            "section",
            "overlap",
            "context-pressure",
            f"n{target}",
        ],
        authors=[r.name for r in PANEL],
        seed=seed_spec,
        document_id=doc_id,
        brief=brief,
        steps=steps,
        n_suggestions=target,
        extra_checks=extra_checks,
    )


#: ``(scenario_id, document, target, task, seed, comments, nested)``.
#: Seeds are fixed, so the corpus is reproducible; they were chosen by
#: :func:`llmux.scenarios.stressgen.build.search_seed`, which walks upward
#: from 1 until a seed yields a fair, solvable scenario.
STRESS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "scenario_id": "stress-030-faq-copyedit",
        "document_key": "advice-faq",
        "target": 30,
        "task_factory": _task_faq,
        # seed 1 leaves two of marcus's cards pending inside an insertion the
        # rules reject, which destroys them: unachievable as stated.
        "seed": 2,
        "comment_count": 4,
        "nest_count": 1,
    },
    {
        "scenario_id": "stress-060-career-style",
        "document_key": "compute-governance",
        "target": 60,
        "task_factory": _task_career,
        "seed": 1,
        "comment_count": 7,
        "nest_count": 2,
    },
    {
        "scenario_id": "stress-090-policy-triage",
        "document_key": "synthesis-screening",
        "target": 90,
        "task_factory": _task_policy,
        "seed": 1,
        "comment_count": 10,
        "nest_count": 3,
        "sentence_cap": 3,
    },
    {
        # The densest tier: 120 cards over 98 sentences, so up to three
        # reviewers land in one sentence. That is what a page with five
        # reviewers on it actually looks like, and it is the point of the
        # scenario.
        "scenario_id": "stress-120-research-full",
        "document_key": "forecasting-evidence",
        "target": 120,
        "task_factory": _task_research,
        "seed": 1,
        "comment_count": 12,
        "nest_count": 3,
        "sentence_cap": 3,
    },
)


def stress_catalog() -> list[Scenario]:
    return [build_stress_scenario(**spec) for spec in STRESS_SPECS]


def walk_for(spec: dict[str, Any]) -> Walk:
    """The walk behind a spec, for reporting the realised edit distribution."""
    document = prose.BY_KEY[spec["document_key"]]
    return run_walk(
        document,
        seed=spec["seed"],
        target=spec["target"],
        panel=PANEL,
        document_id=f"mockdoc-stress-{spec['target']:03d}-{spec['document_key']}",
        comment_count=spec["comment_count"],
        nest_count=spec["nest_count"],
        sentence_cap=spec.get("sentence_cap", 2),
        overlap_fraction=spec.get("overlap_fraction", 0.18),
    )


def decisions_for(spec: dict[str, Any]) -> tuple[MockDoc, dict[str, str], Task]:
    """Seeded document, intended assignment and task, for reporting."""
    walk = walk_for(spec)
    _backend, doc = seeded_backend(walk.seed_spec())
    task = spec["task_factory"](prose.BY_KEY[spec["document_key"]])
    return doc, decide(doc, task.rules), task


__all__ = [
    "STRESS_SPECS",
    "StressBuildError",
    "Task",
    "build_stress_scenario",
    "decisions_for",
    "in_section",
    "overlaps_other_author",
    "stress_catalog",
    "strikes_at_least",
    "walk_for",
]
