"""The scenario corpus: composed seeds, briefs, and intended solutions.

Every entry follows the same shape -- build a seed out of §5 moves, seed a
throwaway backend, evaluate the task's predicate against the *resulting*
registry, and turn the decisions into oracle steps. Nothing here states an
expected document; the expected document is what the model does.

The ladder:

======================  =================================================
tier                    what it exercises
======================  =================================================
easy (1-2)              single author, pure add/delete, accept-all-by-predicate
medium (3)              multi-author, replacements, selective predicates,
                        thread replies, anchored comments at computed ranges
hard (4)                8-12 suggestions, nested insertions (conjunctive
                        rule), cross-author overlapping deletions, merged
                        same-author runs, decoys, astral-plane emoji,
                        multi-phase tasks
adversarial (5)         tasks whose naive execution silently produces the
                        wrong end state
======================  =================================================

Each hard/adversarial docstring names the trap and why an LLM plausibly
falls into it -- that claim is the scenario's reason to exist, so it belongs
next to the scenario and not in a report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from llmux.scenarios.primitives import (
    Predicate,
    Rule,
    SeedBuilder,
    add,
    adds_text,
    after,
    always,
    before,
    by_author,
    decide,
    deletes_part_of,
    is_noop,
    kind_is,
    remove,
    rewrite,
    seeded_backend,
    select,
    span,
)
from llmux.scenarios.steps import (
    Accept,
    AnchorComment,
    Reject,
    ReplyToSuggestion,
    Step,
    SuggestEdit,
)

TIER_DIFFICULTY = {"easy": 1, "medium": 3, "hard": 4, "adversarial": 5}


@dataclass
class Scenario:
    """Everything the generator needs to write one scenario directory."""

    id: str
    tier: str
    difficulty: int
    tags: list[str]
    authors: list[str]
    seed: dict[str, Any]
    document_id: str
    brief: str
    steps: list[Step]
    n_suggestions: int
    extra_checks: list[dict[str, Any]] = field(default_factory=list)


def _brief(title: str, document_id: str, body: str) -> str:
    return f"""# {title}

**Document ID:** `{document_id}`
**You are signed in as** `reviewer@example.com`. Anything you suggest, reply
or comment is authored by `reviewer`.

{body.strip()}

## Ground rules

- Work only through the document review tools. Read the document before you
  change it; suggestion ids are never given to you, you discover them.
- Do exactly what is asked and nothing else: no extra suggestions, comments,
  replies, accepts or rejects.
- When a task says "exactly", it means exactly -- character for character.
"""


def _resolution_steps(
    doc: Any, rules: list[Rule], first: Optional[Predicate] = None
) -> list[Step]:
    """Turn rules into accept/reject steps, in card order.

    ``first`` promotes a subset to the front. That matters exactly once --
    when resolving A would garbage-collect B (§11.1 I2), which makes a later
    ``manage_document_suggestion`` on B a hard 400. The end state is the same
    either way (L3), so the ordering is the oracle's convenience, not part of
    the task.
    """
    decisions = decide(doc, rules)
    promoted = set(select(doc, first)) if first is not None else set()
    ordered = [sid for sid in decisions if sid in promoted]
    ordered += [sid for sid in decisions if sid not in promoted]
    return [
        Accept(sid) if decisions[sid] == "accept" else Reject(sid) for sid in ordered
    ]


# ---------------------------------------------------------------------------
# easy
# ---------------------------------------------------------------------------


def easy_accept_all_insertions() -> Scenario:
    """Three pure insertions by one author; accept the lot."""
    doc_id = "mockdoc-e1-accept-all"
    b = SeedBuilder(
        base_text=(
            "Project Orion status\n"
            "The launch window is on track.\n"
            "Risks are documented in the appendix.\n"
        ),
        document_id=doc_id,
        title="Project Orion status",
    )
    b.move("a1", add("alice", after("status"), " (draft)"))
    b.move("a2", add("alice", before("on track"), "currently "))
    b.move("a3", add("alice", after("appendix"), " and the tracker"))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(doc, [Rule(always(), "accept")])
    return Scenario(
        id="easy-accept-all-insertions",
        tier="easy",
        difficulty=1,
        tags=["accept", "insertion", "single-author", "accept-all"],
        authors=["alice"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Accept every pending suggestion",
            doc_id,
            """
Alice has proposed a few wording additions to the status note.

## What to do

Accept every pending suggestion in the document. Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[{"check": "suggestion_count", "equals": 0}],
    )


def easy_reject_all_deletions() -> Scenario:
    """Three pure deletions; reject the lot, restoring the base text."""
    doc_id = "mockdoc-e2-reject-all"
    b = SeedBuilder(
        base_text=(
            "Meeting notes for the weekly sync.\n"
            "Attendees: Ana, Ben, Cara.\n"
            "Action items are listed below.\n"
        ),
        document_id=doc_id,
        title="Weekly sync notes",
    )
    b.move("d1", remove("bob", span(" for the weekly sync")))
    b.move("d2", remove("bob", span(", Cara")))
    b.move("d3", remove("bob", span(" below")))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(doc, [Rule(always(), "reject")])
    return Scenario(
        id="easy-reject-all-deletions",
        tier="easy",
        difficulty=1,
        tags=["reject", "deletion", "single-author", "reject-all"],
        authors=["bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Reject every pending suggestion",
            doc_id,
            """
Bob proposed cutting several details out of the meeting notes. The team
decided to keep all of them.

## What to do

Reject every pending suggestion, so the note reads exactly as it did before
Bob touched it. Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_present", "text": "Ana, Ben, Cara"},
            {"check": "text_present", "text": "listed below"},
        ],
    )


def easy_kind_split() -> Scenario:
    """Accept the additions, reject the removals -- predicate on card kind."""
    doc_id = "mockdoc-e3-kind-split"
    b = SeedBuilder(
        base_text=(
            "The report covers the first quarter.\n"
            "We expect a maybe positive outcome.\n"
        ),
        document_id=doc_id,
        title="Quarter report note",
    )
    b.move("add1", add("alice", after("report"), " (final)"))
    b.move("add2", add("alice", before("outcome"), "clearly "))
    b.move("del1", remove("alice", span(" maybe")))
    b.move("del2", remove("alice", span(" first")))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc, [Rule(kind_is("Add"), "accept"), Rule(kind_is("Delete"), "reject")]
    )
    return Scenario(
        id="easy-kind-split",
        tier="easy",
        difficulty=2,
        tags=["accept", "reject", "predicate", "single-author"],
        authors=["alice"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Keep the additions, drop the cuts",
            doc_id,
            """
Alice both added and removed wording. Only her additions were agreed.

## What to do

- Accept every suggestion that **only adds** text.
- Reject every suggestion that **only removes** text.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_present", "text": "maybe"},
            {"check": "text_present", "text": "first quarter"},
        ],
    )


def easy_delete_marker_predicate() -> Scenario:
    """Accept only the deletions that remove a specific marker."""
    doc_id = "mockdoc-e4-todo"
    b = SeedBuilder(
        base_text=(
            "Draft outline.\n"
            "TODO: confirm the venue.\n"
            "The agenda is fixed.\n"
            "TODO: book the caterer.\n"
            "Send invitations next week.\n"
        ),
        document_id=doc_id,
        title="Event outline",
    )
    b.move("todo1", remove("alice", span("TODO: ", 0)))
    b.move("todo2", remove("alice", span("TODO: ", 1)))
    b.move("other1", remove("alice", span(" next week")))
    b.move("other2", remove("alice", span("Draft ")))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(deletes_part_of("TODO: "), "accept"), Rule(always(), "reject")],
    )
    return Scenario(
        id="easy-delete-todo-markers",
        tier="easy",
        difficulty=2,
        tags=["accept", "reject", "predicate", "deletion", "single-author"],
        authors=["alice"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Strip the TODO markers only",
            doc_id,
            """
Alice suggested a batch of deletions. Only the ones that remove a `TODO: `
marker were agreed; the rest change meaning and were not.

## What to do

- Accept every suggestion that deletes the text `TODO: `.
- Reject every other pending suggestion.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_absent", "text": "TODO"},
            {"check": "text_present", "text": "next week"},
            {"check": "text_present", "text": "Draft outline"},
        ],
    )


# ---------------------------------------------------------------------------
# medium
# ---------------------------------------------------------------------------


def medium_author_split() -> Scenario:
    """Six suggestions, two authors, replacements included; split by author.

    Authorship is only visible through the Developer Preview suggestion
    threads, so this fails outright on any surface that reports
    ``author: null``.
    """
    doc_id = "mockdoc-m1-author-split"
    b = SeedBuilder(
        base_text=(
            "Quarterly update\n"
            "Revenue grew by 12 percent this quarter.\n"
            "The support backlog shrank.\n"
            "We hired two engineers in March.\n"
        ),
        document_id=doc_id,
        title="Quarterly update",
    )
    b.move("a1", add("alice", after("update"), " — internal"))
    b.move("a2", remove("alice", span(" this quarter")))
    b.move("a3", rewrite("alice", span("shrank"), "shrank by half"))
    b.move("b1", add("bob", before("two"), "exactly "))
    b.move("b2", remove("bob", span(" in March")))
    b.move("b3", rewrite("bob", span("12"), "twelve"))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(by_author("alice"), "accept"), Rule(by_author("bob"), "reject")],
    )
    return Scenario(
        id="medium-author-split",
        tier="medium",
        difficulty=3,
        tags=["multi-author", "replacement", "accept", "reject", "predicate"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Take Alice's edits, drop Bob's",
            doc_id,
            """
Two reviewers went over the quarterly update. Alice is the document owner
and her edits were signed off; Bob's were not.

## What to do

- Accept every suggestion authored by **Alice**.
- Reject every suggestion authored by **Bob**.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_present", "text": "shrank by half"},
            {"check": "text_present", "text": "12 percent"},
            {"check": "text_absent", "text": "exactly two"},
        ],
    )


def medium_touching_word() -> Scenario:
    """Reject anything that removes part of a word; decoys that only mention it.

    The decoys matter: one suggestion *adds* the word "legacy" and another
    deletes text immediately next to it. Both look relevant to a keyword
    match over the tool output and neither strikes a character of the word.
    """
    doc_id = "mockdoc-m2-legacy"
    b = SeedBuilder(
        base_text=(
            "The legacy exporter still runs nightly.\n"
            "We keep the legacy schema for now.\n"
            "The new exporter is faster.\n"
        ),
        document_id=doc_id,
        title="Exporter notes",
    )
    b.move("kill_word", remove("alice", span("legacy ", 0)))
    b.move("kill_phrase", rewrite("bob", span("legacy schema"), "current schema"))
    b.move("decoy_add_time", add("carol", after("nightly"), " at 02:00"))
    b.move("decoy_mentions", add("bob", after("faster"), " than the legacy one"))
    b.move("decoy_adjacent", remove("carol", span(" still")))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(deletes_part_of("legacy"), "reject"), Rule(always(), "accept")],
    )
    return Scenario(
        id="medium-reject-touching-legacy",
        tier="medium",
        difficulty=3,
        tags=["multi-author", "predicate", "decoy", "replacement"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Protect the word “legacy”",
            doc_id,
            """
Legal asked us not to remove the word `legacy` from this document yet.
Everything else in review is fine to take.

## What to do

- Reject every suggestion that **deletes any part of an occurrence of the
  word `legacy`**. A suggestion that merely adds the word, or that deletes
  text sitting next to it, does not count.
- Accept every other pending suggestion.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_present", "text": "legacy exporter"},
            {"check": "text_present", "text": "legacy schema"},
            {"check": "text_present", "text": "at 02:00"},
            {"check": "text_absent", "text": "still runs"},
        ],
    )


def medium_reply_then_accept() -> Scenario:
    """Reply on every thread of one author, resolve the other author's."""
    doc_id = "mockdoc-m3-reply"
    b = SeedBuilder(
        base_text=(
            "Sprint review notes\n"
            "The API migration finished ahead of schedule.\n"
            "Two flaky tests remain in the suite.\n"
            "Documentation was not updated.\n"
        ),
        document_id=doc_id,
        title="Sprint review notes",
    )
    b.move("a1", add("alice", after("schedule"), " with no rollbacks"))
    b.move("a2", remove("alice", span(" flaky")))
    b.move("b1", rewrite("bob", span("Two"), "Three"))
    b.move("b2", add("bob", after("suite"), " and are being triaged"))
    b.move("b3", remove("bob", span(" not")))

    backend, doc = seeded_backend(b.seed_spec())
    bob_ids = select(doc, by_author("bob"))
    steps: list[Step] = [
        ReplyToSuggestion(
            sid,
            "Could you say what evidence this is based on?",
            content_regex=r"\?\s*$",
        )
        for sid in bob_ids
    ]
    steps += _resolution_steps(
        doc,
        [Rule(by_author("alice"), "accept"), Rule(by_author("bob"), "none")],
    )
    return Scenario(
        id="medium-reply-to-bob-accept-alice",
        tier="medium",
        difficulty=3,
        tags=["multi-author", "threads", "reply", "accept", "two-phase"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Question Bob, take Alice",
            doc_id,
            """
Bob's suggestions need justifying before anyone acts on them. Alice's are
agreed.

## What to do

1. For **every** suggestion authored by **Bob**, post a reply on that
   suggestion's own thread asking him a clarifying question. Every reply you
   post must end with a question mark.
2. Do **not** accept or reject any of Bob's suggestions -- they stay pending
   with your question on them.
3. Accept every suggestion authored by **Alice**.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "authored_suggestion_count", "author": "bob", "equals": 3},
            {"check": "authored_suggestion_count", "author": "alice", "equals": 0},
            {"check": "suggestion_count", "equals": 3},
        ],
    )


def medium_anchored_comment() -> Scenario:
    """Resolve, then anchor a comment on an exact phrase after an emoji."""
    doc_id = "mockdoc-m4-anchor"
    b = SeedBuilder(
        base_text=(
            "Launch checklist \U0001f389\n"
            "The revenue forecast needs a second pass.\n"
            "Ops sign-off is pending.\n"
        ),
        document_id=doc_id,
        title="Launch checklist",
    )
    b.move("keeper", add("alice", before("pass"), "quarterly "))
    b.move("drop1", remove("bob", span(" is pending")))
    b.move("drop2", add("carol", after("\U0001f389"), " v2"))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc, [Rule(adds_text("quarterly"), "accept"), Rule(always(), "reject")]
    )
    steps.append(
        AnchorComment(
            span("revenue forecast"),
            "Confirm the source before sign-off.",
            content_contains="Confirm the source",
        )
    )
    return Scenario(
        id="medium-anchored-comment",
        tier="medium",
        difficulty=3,
        tags=["multi-author", "comments", "anchor", "utf16", "emoji"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Accept one edit, then anchor a comment",
            doc_id,
            """
## What to do

1. Accept the one pending suggestion that adds the word `quarterly`.
2. Reject every other pending suggestion.
3. Then leave a comment anchored to **exactly** the phrase
   `revenue forecast` -- that phrase and nothing else, no leading or
   trailing spaces -- with the content:
   `Confirm the source before sign-off.`
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "comment_count", "equals": 1},
            {"check": "text_present", "text": "quarterly pass"},
        ],
    )


def medium_replacement_kinds() -> Scenario:
    """Accept replacements, reject anything one-sided -- across two authors."""
    doc_id = "mockdoc-m5-replacements"
    b = SeedBuilder(
        base_text=(
            "Roadmap for Q3\n"
            "Q3 targets are unchanged.\n"
            "The draft plan ships Monday.\n"
            "Owners: Ana and Ben.\n"
        ),
        document_id=doc_id,
        title="Roadmap",
    )
    b.move("r1", rewrite("alice", span("Roadmap for Q3"), "Roadmap for Q4"))
    b.move("r2", rewrite("alice", span("Q3 targets"), "Q4 targets"))
    b.move("r3", rewrite("bob", span("draft plan"), "final plan"))
    b.move("d1", remove("bob", span(" and Ben")))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc, [Rule(kind_is("Replace"), "accept"), Rule(always(), "reject")]
    )
    return Scenario(
        id="medium-accept-replacements",
        tier="medium",
        difficulty=3,
        tags=["multi-author", "replacement", "predicate"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Take the rewrites, leave the rest",
            doc_id,
            """
## What to do

- Accept every suggestion that **replaces** text, i.e. removes some text and
  adds some text in its place.
- Reject every suggestion that only removes text, or only adds text.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_present", "text": "Roadmap for Q4"},
            {"check": "text_present", "text": "Q4 targets"},
            {"check": "text_present", "text": "Ana and Ben"},
            {"check": "text_absent", "text": "draft plan"},
        ],
    )


# ---------------------------------------------------------------------------
# hard
# ---------------------------------------------------------------------------


def hard_nested_insertion() -> Scenario:
    """Bob typed inside Alice's pending insertion; Alice is rejected.

    Trap (SPEC §2's conjunctive rule, open question §13.5): Bob's nested text
    has ``ins = {A, B}``. Accepting B and rejecting A still removes it -- the
    text is meaningless without the sentence it lives in. An agent that
    "accepted Bob's suggestion" and then notices his words are gone is very
    likely to re-add them, which is precisely the wrong end state. Worse, if
    it rejects Alice *first*, Bob's suggestion is garbage-collected (I2) and
    the accept it was told to perform fails with a 400 on a suggestion id
    that was valid one call earlier.
    """
    doc_id = "mockdoc-h1-nested"
    b = SeedBuilder(
        base_text=("Status: green.\nThe pipeline is stable.\nNext steps follow.\n"),
        document_id=doc_id,
        title="Status",
    )
    b.move(
        "alice_sentence", add("alice", after("green."), " We are ahead of schedule.")
    )
    b.move("bob_nested", add("bob", before("ahead"), "comfortably "))
    b.move("bob_load", add("bob", after("stable"), " under load"))
    b.move("carol_stable", remove("carol", span(" is stable")))
    b.move("alice_owner", add("alice", after("Next steps"), " (owner: Ana)"))
    b.move("carol_follow", remove("carol", span(" follow")))
    b.move("alice_amber", rewrite("alice", span("green"), "amber"))
    b.move("bob_now", add("bob", before("The pipeline"), "Right now, "))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(by_author("alice"), "reject"), Rule(always(), "accept")],
        first=by_author("bob"),
    )
    return Scenario(
        id="hard-nested-insertion",
        tier="hard",
        difficulty=4,
        tags=["multi-author", "nested-insertion", "conjunctive", "gc", "trap"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Alice's pass is withdrawn",
            doc_id,
            """
Alice has withdrawn her whole review pass. Bob's and Carol's edits stand.

## What to do

- Reject every suggestion authored by **Alice**.
- Accept every suggestion authored by **Bob** or **Carol**.

Leave nothing pending. Do not add any text of your own, and do not try to
put back text that disappears as a consequence of these decisions -- the
result of the decisions is the result.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            # Bob's nested words die with Alice's sentence: conjunctive ins.
            {"check": "text_absent", "text": "comfortably"},
            {"check": "text_absent", "text": "ahead of schedule"},
            {"check": "text_present", "text": "under load"},
            {"check": "text_present", "text": "Right now,"},
            {"check": "text_present", "text": "Status: green."},
        ],
    )


def hard_overlap_and_merge() -> Scenario:
    """Cross-author overlapping deletions plus a merged same-author decoy.

    Trap (§6 / L8): Carol typed "stale " immediately before deleting
    "guidance", so §6 absorbed the two into a *single* Replace card. It reads
    as "the suggestion that deletes guidance" to anyone scanning for
    deletions, but it adds text, so the predicate excludes it. Accepting it
    is the single most likely error, and it is unrecoverable within the task.
    """
    doc_id = "mockdoc-h2-overlap"
    b = SeedBuilder(
        base_text=(
            "The quick brown fox jumps over the lazy dog.\n"
            "This paragraph contains obsolete guidance.\n"
            "Keep the final sentence intact.\n"
        ),
        document_id=doc_id,
        title="Overlap fixture",
    )
    b.move("alice_quick", remove("alice", span("quick brown ")))
    b.move("bob_brown", remove("bob", span("brown fox ")))
    b.move("carol_obsolete", remove("carol", span(" obsolete")))
    b.move("carol_stale", add("carol", before("guidance"), "stale "))
    b.move("carol_guidance", remove("carol", span("guidance")))
    b.move("bob_preserve", rewrite("bob", span("Keep the"), "Preserve the"))
    b.move("alice_today", add("alice", after("dog"), " today"))
    b.move("alice_over", remove("alice", span(" over the lazy")))
    b.move("alice_unchanged", add("alice", after("intact"), " and unchanged"))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc, [Rule(kind_is("Delete"), "accept"), Rule(always(), "reject")]
    )
    return Scenario(
        id="hard-overlap-and-merge",
        tier="hard",
        difficulty=4,
        tags=["multi-author", "overlap", "merge", "decoy", "predicate"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Cuts only",
            doc_id,
            """
This pass is a length cut. Only suggestions that purely remove text are in
scope; anything that also introduces new wording has to go back to its
author.

## What to do

- Accept every suggestion that **only removes** text and adds none.
- Reject every other pending suggestion, including any suggestion that
  removes text *and* adds text.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "text_absent", "text": "quick"},
            {"check": "text_absent", "text": "brown"},
            {"check": "text_absent", "text": "fox"},
            {"check": "text_absent", "text": "stale"},
            {"check": "text_present", "text": "guidance"},
            {"check": "text_present", "text": "Keep the final sentence intact"},
        ],
    )


def hard_utf16_emoji() -> Scenario:
    """Nine suggestions over emoji and combining sequences, then two anchors.

    Trap: every index in this document differs between Python ``len()``,
    code points and UTF-16 code units. The astral emoji is 2 UTF-16 units,
    the ZWJ family is 8, and ``cafe`` + combining acute is 2 units for one
    visible character. Anchoring a comment on an exact phrase downstream of
    all three is only possible with real UTF-16 arithmetic -- and the mock
    rejects an index that lands mid-cluster, so a near miss is a 400 rather
    than a silent success.
    """
    doc_id = "mockdoc-h3-utf16"
    b = SeedBuilder(
        base_text=(
            "Release \U0001f389 notes for the platform team \U0001f468‍\U0001f469‍\U0001f467\n"
            "The café rollout reached 40 percent of users.\n"
            "Latency improved after the cache rewrite.\n"
            "Owner: José Nunez.\n"
        ),
        document_id=doc_id,
        title="Release notes",
    )
    b.move("alice_45", rewrite("alice", span("40"), "45"))
    b.move("bob_team", remove("bob", span(" for the platform team")))
    b.move("carol_rebuild", add("carol", after("rewrite"), " and the index rebuild"))
    b.move("alice_primary", add("alice", before("Owner"), "Primary "))
    b.move("bob_tilde", rewrite("bob", span("Nunez"), "Nuñez"))
    b.move("carol_users", remove("carol", span(" of users")))
    b.move("alice_v2", add("alice", after("\U0001f389"), " v2"))
    b.move("bob_measured", add("bob", before("Latency"), "Measured: "))
    b.move("carol_coffee", rewrite("carol", span("café"), "coffee"))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(by_author("bob"), "reject"), Rule(always(), "accept")],
    )
    steps.append(
        AnchorComment(
            span("index rebuild"),
            "Anchored by the reviewer.",
            content_contains="Anchored by the reviewer",
        )
    )
    steps.append(
        AnchorComment(
            span("Primary"),
            "Anchored by the reviewer.",
            content_contains="Anchored by the reviewer",
        )
    )
    return Scenario(
        id="hard-utf16-emoji-anchors",
        tier="hard",
        difficulty=4,
        tags=["multi-author", "utf16", "emoji", "combining", "comments", "anchor"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Resolve by author, then anchor two comments",
            doc_id,
            """
## What to do

1. Reject every suggestion authored by **Bob**.
2. Accept every suggestion authored by **Alice** or **Carol**.
3. Then, on the document as it stands afterwards, create two comments, each
   with the content `Anchored by the reviewer.`:
   - one anchored to **exactly** the phrase `index rebuild`;
   - one anchored to **exactly** the word `Primary`.

Neither anchor may include a neighbouring space or character.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "comment_count", "equals": 2},
            {"check": "text_present", "text": "45 percent"},
            {"check": "text_present", "text": "for the platform team"},
            {"check": "text_absent", "text": "Measured:"},
            {"check": "text_present", "text": "coffee rollout"},
        ],
    )


def hard_multi_phase() -> Scenario:
    """Twelve suggestions, three authors, two ordered phases.

    Trap: the reply phase is only verifiable on threads that are still open,
    because §7 deletes a suggestion's thread along with the suggestion. An
    agent that resolves first and replies afterwards finds the threads gone
    and, if it is inventive, will post document-level comments instead --
    which is not what was asked and is separately observable.
    """
    doc_id = "mockdoc-h4-phases"
    b = SeedBuilder(
        base_text=(
            "Incident review\n"
            "The outage began at 03:12 and lasted 47 minutes.\n"
            "Root cause was a bad config push.\n"
            "Customer impact was limited to the EU region.\n"
            "Follow-up actions are tracked in the incident doc.\n"
        ),
        document_id=doc_id,
        title="Incident review",
    )
    b.move("a1", rewrite("alice", span("47"), "52"))
    b.move("a2", add("alice", after("push"), " to the edge tier"))
    b.move("a3", remove("alice", span(" limited to")))
    b.move("a4", add("alice", before("Follow-up"), "Open "))
    b.move("b1", rewrite("bob", span("03:12"), "03:15"))
    b.move("b2", remove("bob", span(" in the incident doc")))
    b.move("b3", add("bob", after("region"), " and the UK"))
    b.move("b4", rewrite("bob", span("bad config push"), "misapplied config push"))
    b.move("c1", add("carol", after("review"), " (draft 2)"))
    b.move("c2", remove("carol", span(" began")))
    b.move("c3", rewrite("carol", span("EU"), "European"))
    b.move("c4", add("carol", before("Root cause"), "Suspected "))

    _, doc = seeded_backend(b.seed_spec())
    steps: list[Step] = [
        ReplyToSuggestion(
            sid,
            "What is the source for this change?",
            content_regex=r"\?\s*$",
        )
        for sid in select(doc, by_author("carol"))
    ]
    steps += _resolution_steps(
        doc,
        [
            Rule(by_author("alice"), "accept"),
            Rule(by_author("bob"), "reject"),
            Rule(by_author("carol"), "none"),
        ],
    )
    return Scenario(
        id="hard-multi-phase-review",
        tier="hard",
        difficulty=4,
        tags=["multi-author", "threads", "two-phase", "replacement", "reply"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Two-phase incident review",
            doc_id,
            """
Carol's suggestions need sourcing before anyone decides on them. Alice's are
signed off; Bob's were rejected in the review meeting.

## What to do, in this order

1. **First**, for every suggestion authored by **Carol**, post a reply on
   that suggestion's own thread asking where the change comes from. Every
   reply must end with a question mark.
2. **Then** accept every suggestion authored by **Alice** and reject every
   suggestion authored by **Bob**.
3. Leave every suggestion authored by **Carol** pending, with your question
   on it. Do not accept or reject any of Carol's.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "authored_suggestion_count", "author": "carol", "equals": 4},
            {"check": "suggestion_count", "equals": 4},
            {"check": "comment_count", "equals": 0},
        ],
    )


# ---------------------------------------------------------------------------
# adversarial
# ---------------------------------------------------------------------------


def adversarial_stale_index() -> Scenario:
    """Resolve first, then insert at a position the resolutions moved.

    Trap: L3 says *resolution* commutes, but edits do not. Accepting a
    deletion physically removes characters (§7 filters the char array) and
    rejecting an insertion removes them too, so every index after them
    shifts. An agent that reads the document once, plans all its calls, and
    then executes them -- the default shape of a tool-using LLM turn -- will
    insert its text into the middle of an earlier sentence and report
    success, because the API call succeeds.
    """
    doc_id = "mockdoc-a1-stale-index"
    b = SeedBuilder(
        base_text=(
            "Section 1: Overview\n"
            "The legacy pipeline is deprecated and will be removed.\n"
            "Section 2: Plan\n"
            "Migrate the remaining jobs by the end of the month.\n"
        ),
        document_id=doc_id,
        title="Migration plan",
    )
    b.move("cut1", remove("alice", span("legacy ")))
    b.move("addition1", add("bob", after("deprecated"), " (2024)"))
    b.move("cut2", remove("alice", span(" and will be removed")))
    b.move("addition2", add("carol", before("Migrate"), "First, "))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc,
        [Rule(kind_is("Delete"), "accept"), Rule(kind_is("Add"), "reject")],
    )
    steps.append(SuggestEdit(after("Section 2: Plan"), text=" (approved)"))
    return Scenario(
        id="adversarial-stale-index",
        tier="adversarial",
        difficulty=5,
        tags=["multi-author", "stale-index", "suggest", "trap", "ordering"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Clean up, then mark the plan approved",
            doc_id,
            """
## What to do, in this order

1. Accept every suggestion that **only removes** text.
2. Reject every suggestion that **only adds** text.
3. **Then**, as a new pending suggestion, insert the text ` (approved)`
   immediately after `Section 2: Plan` -- between the final `n` of `Plan`
   and the line break that follows it.

Leave your new suggestion pending; do not accept it.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "authored_suggestion_count", "author": "reviewer", "equals": 1},
            {"check": "suggestion_count", "equals": 1},
            {"check": "text_present", "text": "Section 2: Plan (approved)\n"},
            {"check": "text_absent", "text": "legacy"},
            {"check": "text_absent", "text": "(2024)"},
            {"check": "text_absent", "text": "First,"},
        ],
    )


def adversarial_noop() -> Scenario:
    """Two suggestions that change nothing, two that look like they don't.

    Trap: a Replace card whose struck and added text are identical is a
    genuine no-op -- accepting and rejecting produce byte-identical
    documents, so the *decision* is only observable through the comment the
    task requires. The near misses (a comma, a lower-cased first letter)
    look identical at a glance in a suggestion listing. An agent that reads
    the card summary rather than comparing pre/post text gets the partition
    wrong in both directions.
    """
    doc_id = "mockdoc-a2-noop"
    b = SeedBuilder(
        base_text="Please review the attached summary before Friday and share feedback.\n",
        document_id=doc_id,
        title="Review request",
    )
    b.move("noop_summary", rewrite("alice", span("summary"), "summary"))
    b.move("real_friday", rewrite("bob", span("Friday"), "Friday,"))
    b.move("noop_review", rewrite("carol", span("review"), "review"))
    b.move("real_please", rewrite("alice", span("Please"), "please"))

    _, doc = seeded_backend(b.seed_spec())
    noops = select(doc, is_noop())
    steps: list[Step] = []
    for sid in noops:
        word = doc.label(sid)["added"]
        steps.append(
            AnchorComment(
                span(word),
                "No-op: this suggestion changes nothing.",
                content_contains="No-op",
            )
        )
        steps.append(Reject(sid))
    steps += _resolution_steps(doc, [Rule(is_noop(), "none"), Rule(always(), "accept")])
    return Scenario(
        id="adversarial-noop-suggestions",
        tier="adversarial",
        difficulty=5,
        tags=["multi-author", "no-op", "decoy", "comments", "anchor", "trap"],
        authors=["alice", "bob", "carol"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Find the suggestions that do nothing",
            doc_id,
            """
Four rewrites are pending on this one sentence. Some of them replace a piece
of text with **exactly the same text** -- the text they remove and the text
they put in its place are character-for-character identical, so accepting
them would change nothing at all. Those are the ones we want out.

## What to do

For each pending suggestion, compare the text it removes with the text it
adds.

- If the two are identical, then **first** leave a comment anchored to
  exactly the word it replaces, with the content
  `No-op: this suggestion changes nothing.`, and **then** reject that
  suggestion.
- Otherwise -- the two differ, however slightly -- accept it.

Leave nothing pending.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "comment_count", "equals": 2},
            {"check": "text_present", "text": "Friday,"},
            {"check": "text_present", "text": "please review"},
            {"check": "text_present", "text": "attached summary"},
        ],
    )


def adversarial_both_marks() -> Scenario:
    """ "Accept the replacement" removes text the agent thinks it is keeping.

    Trap (SPEC §5.2, confirmed against real Docs): Alice typed "ular" inside
    "popular" and then deleted "popul". §6 merged the two, so the card reads
    ``Replace: "popul" with "ular"``. Reading the card as
    ``"popul" -> "ular"`` and mentally concatenating gives "popular", so
    accepting looks like a no-op that fixes nothing. It is not: §7 removes
    every character carrying the suggestion in ``dels``, including the
    "popul" that the reader assumed was being kept, leaving "ularar". The
    only way to see this before committing is the computed post-text.
    """
    doc_id = "mockdoc-a3-both-marks"
    b = SeedBuilder(
        base_text="The popular greeting is short. The unclear wording is worse.\n",
        document_id=doc_id,
        title="Wording pass",
    )
    b.move("alice_ular", add("alice", after("popul"), "ular"))
    b.move("alice_cut", remove("alice", span("popul")))
    b.move("bob_clear", rewrite("bob", span("unclear"), "clear"))

    _, doc = seeded_backend(b.seed_spec())
    target = "The popular greeting is short. The clear wording is worse.\n"
    steps = _resolution_steps(
        doc,
        [Rule(by_author("bob"), "accept"), Rule(always(), "reject")],
    )
    return Scenario(
        id="adversarial-both-marks-replacement",
        tier="adversarial",
        difficulty=5,
        tags=["both-marks", "merge", "replacement", "trap", "multi-author"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Land the sentence exactly",
            doc_id,
            f"""
Two suggestions are pending. When you are done, the document must read
exactly this, and nothing else:

```
{target}```

## What to do

Work out, for each pending suggestion, what the document would say if you
accepted it.

- Accept a suggestion if accepting it moves the document towards the text
  above.
- Reject a suggestion if accepting it would move the document away from it.

Leave nothing pending. Do not create any new suggestion, edit or comment of
your own -- the target text is reachable using only accept and reject.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            {"check": "suggestion_count", "equals": 0},
            {"check": "projection_text", "projection": "display", "equals": target},
            {"check": "text_absent", "text": "ularar"},
            {"check": "comment_count", "equals": 0},
        ],
    )


def adversarial_header_segment() -> Scenario:
    """An index taken out of a header lands in the BODY, silently.

    Trap (the bug class this branch has now found three times, made
    reachable): Docs numbers every ``(tabId, segmentId)`` pair from its own
    start, and ``suggest_doc_edit`` defaults to ``segment_id=None`` -- the
    body of the default tab. A card in the page header reports
    ``start_index: 13``; handing that number back without the ``segment_id``
    it came with writes at index 13 of the BODY.

    It does not fail. The body here is long enough that [13, 18) is a
    perfectly valid range in it, so the API accepts the write, the
    verification echo comes back clean, and the document quietly acquires a
    suggestion that guts a word in the first body sentence while the header
    -- the thing the task was about -- is untouched. Index 0 is the only
    index that fails loud, on the body's section-break floor check; every
    other index does not.

    The corpus could not express this before ``mockdocs`` had segments, which
    is exactly why three rounds of the same bug reached production code with
    every mock-backed test green. The naive bare-index form of this
    scenario's own solution is generated alongside it as
    ``naive_solution.json`` and the corpus gate REQUIRES it to fail.
    """
    doc_id = "mockdoc-a4-header-segment"
    b = SeedBuilder(
        base_text=(
            "The quarterly plan ships in March.\nRisks are unchanged.\n"
        ),
        document_id=doc_id,
        title="Quarterly plan",
    )
    header = b.segment("header", "kix.h1", "Confidential draft — do not circulate\n")
    # One card in the header, one in the body: the agent has to notice that
    # its cards are not all in the same coordinate space.
    b.move("bob_header", remove("bob", span("do not circulate", segment_id=header)))
    b.move("alice_body", add("alice", before("unchanged"), "still "))

    _, doc = seeded_backend(b.seed_spec())
    steps = _resolution_steps(
        doc, [Rule(by_author("alice"), "accept"), Rule(always(), "reject")]
    )
    # The trap. `span(..., segment_id=header)` is the whole difference
    # between this and a write into the body's first sentence.
    steps.append(
        SuggestEdit(span("draft", segment_id=header), text="final", delete=True)
    )
    return Scenario(
        id="adversarial-header-segment",
        tier="adversarial",
        difficulty=5,
        tags=["segment", "header", "address", "trap", "multi-author"],
        authors=["alice", "bob"],
        seed=b.seed_spec(),
        document_id=doc_id,
        brief=_brief(
            "Fix the header, leave the body alone",
            doc_id,
            """
This document has a page **header** as well as a body, and there are
pending suggestions in both.

## What to do

1. Accept every pending suggestion by `alice`.
2. Reject every other pending suggestion.
3. Then suggest replacing the word `draft` **in the page header** with
   `final` -- exactly that word, nothing around it.

The body's first sentence must come out of this untouched, character for
character.
""",
        ),
        steps=steps,
        n_suggestions=len(doc.registry),
        extra_checks=[
            # The header really was edited...
            {"check": "text_present", "text": "Confidential final"},
            # ...and the body's first sentence really was not. This is the
            # check the bare-index write fails: it lands at body index 13.
            {"check": "text_present", "text": "The quarterly plan ships in March."},
            {"check": "comment_count", "equals": 0},
        ],
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

CATALOG: list[Callable[[], Scenario]] = [
    easy_accept_all_insertions,
    easy_reject_all_deletions,
    easy_kind_split,
    easy_delete_marker_predicate,
    medium_author_split,
    medium_touching_word,
    medium_reply_then_accept,
    medium_anchored_comment,
    medium_replacement_kinds,
    hard_nested_insertion,
    hard_overlap_and_merge,
    hard_utf16_emoji,
    hard_multi_phase,
    adversarial_stale_index,
    adversarial_noop,
    adversarial_both_marks,
    adversarial_header_segment,
]


def build_all() -> list[Scenario]:
    return [factory() for factory in CATALOG]
