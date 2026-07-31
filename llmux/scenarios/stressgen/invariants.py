"""Grade a 120-suggestion review by the algebra, not by an authored answer.

At this scale nobody can write down the expected document. What *can* be
written down is the assignment -- which suggestion the task says to accept,
reject, or leave alone -- and SPEC §11.2 L5 turns an assignment into a
document with no judgement calls left in it:

    a character survives iff no suggestion in its ``ins`` was rejected and
    no suggestion in its ``del`` was accepted.

:func:`project` is that sentence as code, generalised to partial
assignments (a pending suggestion has not killed anything yet, so it
removes nothing). Because it never mutates a document it costs one pass per
assignment, which is what makes the per-suggestion counterfactuals below
affordable at 120 cards.

Two things are then derived from it:

**The end-state cross-check.** ``project(doc, decisions)`` must equal what
:mod:`mockdocs.model` produces when the oracle actually applies those
accepts and rejects. That is L1/L3/L7 checked against an independent
implementation: if merge changed content, or resolution order mattered, or
accept/reject got their asymmetry backwards, the two disagree and the build
fails.

**Per-suggestion witnesses.** For each suggestion, project the intended
assignment and project every alternative decision for that one suggestion,
then find a substring of the intended end text that appears in none of the
alternatives. That substring is an algebraic proof that the agent made the
right call on *that card*, gradeable independently of the other 119 -- which
is what turns the score from a cliff into a curve. Suggestions whose
decision provably cannot affect the text (a no-op, or one whose characters
another accept removes either way) have no witness, and get none: crediting
them would be crediting a coin flip.
"""

from __future__ import annotations

from typing import Any, Optional

from mockdocs.model import MockDoc

#: Growing context windows tried when isolating a witness, in characters.
_PADDING = (0, 6, 14, 30, 60, 120, 240, 480)

#: A witness shorter than this is too likely to occur somewhere else in a
#: long document by accident.
_MIN_WITNESS = 6


def project(doc: MockDoc, decisions: dict[str, str]) -> str:
    """SPEC L5, generalised: the display text under a partial assignment.

    ``decisions`` maps suggestion id to ``"accept"`` or ``"reject"``; ids
    absent from it are still pending and therefore still rendered.
    """
    out: list[str] = []
    for char in doc.chars:
        if any(decisions.get(sid) == "reject" for sid in char.ins):
            continue
        if any(decisions.get(sid) == "accept" for sid in char.dels):
            continue
        out.append(char.cp)
    return "".join(out)


def _alternatives(decisions: dict[str, str], sid: str) -> list[dict[str, str]]:
    """Every other decision the agent could have taken on ``sid``."""
    current = decisions.get(sid)
    out: list[dict[str, str]] = []
    for choice in ("accept", "reject", None):
        if choice == current:
            continue
        alternative = dict(decisions)
        if choice is None:
            alternative.pop(sid, None)
        else:
            alternative[sid] = choice
        out.append(alternative)
    return out


def _common_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def witness(doc: MockDoc, decisions: dict[str, str], sid: str) -> Optional[str]:
    """A substring of the intended end text that only the right call produces.

    ``None`` means the decision on ``sid`` is not observable in the text --
    the suggestion is a no-op, or something else removes its characters
    whatever is decided. Those cards are graded by the registry checks
    alone.
    """
    intended = project(doc, decisions)
    alternatives = [project(doc, alt) for alt in _alternatives(decisions, sid)]
    alternatives = [text for text in alternatives if text != intended]
    if not alternatives:
        return None

    lo = min(_common_prefix(intended, other) for other in alternatives)
    hi = min(_common_suffix(intended, other) for other in alternatives)
    end = max(lo, len(intended) - hi)
    for padding in _PADDING:
        start = max(0, lo - padding)
        stop = min(len(intended), end + padding)
        window = intended[start:stop]
        if len(window) < _MIN_WITNESS:
            continue
        if all(window not in other for other in alternatives):
            return window
    return None


def witness_checks(
    doc: MockDoc, decisions: dict[str, str], suggestion_ids: list[str]
) -> list[dict[str, Any]]:
    """One ``decision_witness`` check per suggestion whose call is observable."""
    checks: list[dict[str, Any]] = []
    for sid in suggestion_ids:
        text = witness(doc, decisions, sid)
        if text is None:
            continue
        checks.append(
            {
                "check": "decision_witness",
                "suggestion_id": sid,
                "decision": decisions.get(sid, "pending"),
                "text": text,
            }
        )
    return checks


def pending_checks(suggestion_ids: list[str]) -> list[dict[str, Any]]:
    """One ``suggestion_pending`` check per card the task says to leave alone."""
    return [
        {"check": "suggestion_pending", "suggestion_id": sid} for sid in suggestion_ids
    ]
