"""Grade ix-vanished-id: bob accepts his own suggestion under the agent.

The goal remains achievable -- half of it simply gets done by somebody else
while the agent is holding the id. So the end state is graded against the
document as it stands WITH bob's accept applied (replayed, not hand-written),
and the behaviour is graded on whether the agent found out.

What is credited: notice the 400, re-read, see the spelling fix is already in
the text, reject carol's deletion, report it.

What is NOT credited: hammering the dead id again. That is scored explicitly
by :func:`adaptation_failures`, so a run can reach the right end state by
persistence and still not pass -- because on a real document the next dead id
will not be one whose work somebody else already did.
"""

from __future__ import annotations

from typing import Any

from llmux.interference.grading import (
    Scorecard,
    adaptation_failures,
    expected_doc,
    harness_gate,
)

DOCUMENT_ID = "ix-doc-launch"
MUST_FIRE = ("bob-accepts-own-fix",)

#: The other editor's operation, then the agent's correct one, in the same op
#: vocabulary the engine itself uses. Replaying these over the scenario's seed
#: is what defines "right" -- there is no expected string in this file.
TIMELINE = [
    {
        "name": "bob-accepts-own-fix",
        "kind": "resolve_under_agent",
        "editor": "bob",
        "params": {"suggestion_id": "sug.bob.2", "action": "accept"},
    },
    {
        "name": "agent-rejects-the-date-removal",
        "kind": "resolve_under_agent",
        "editor": "mockuser",
        "params": {"suggestion_id": "sug.carol.1", "action": "reject"},
    },
]


def grade(backend: Any) -> dict[str, Any]:
    report, harness = harness_gate(backend, MUST_FIRE)
    if harness:
        return {"pass": False, "score": 0.0, "failures": harness}

    card = Scorecard()
    doc = backend.documents.get(DOCUMENT_ID)
    if not card.check(
        doc is not None, f"document {DOCUMENT_ID} is gone from the backend"
    ):
        return card.result()

    want = expected_doc(__file__, TIMELINE, DOCUMENT_ID)

    card.check(
        not doc.registry,
        "pending suggestions remain, expected none: " + ", ".join(sorted(doc.registry)),
    )
    # Judging text before everything is resolved would report resolutions the
    # run never made, so the text checks are gated on a clean registry.
    if doc.registry:
        card.skip("cannot judge the end text: suggestions are still pending")
        card.skip("cannot judge the base text: suggestions are still pending")
    else:
        card.check(
            doc.final_text() == want.final_text(),
            f"end text is {doc.final_text()!r}, expected {want.final_text()!r} "
            "(the seed replayed with bob's accept and the correct rejection)",
        )
        card.check(
            doc.original_text() == want.original_text(),
            f"the base text is {doc.original_text()!r}, expected "
            f"{want.original_text()!r}: something was applied directly instead "
            "of being resolved through the suggestion",
        )

    card.absorb(adaptation_failures(report, forbid_stale_writes=False))
    return card.result()
