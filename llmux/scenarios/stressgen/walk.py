"""A seeded random walk over the edit algebra, in reviewers' voices.

The walk turns a base document plus a seed into a fully populated review:
N pending suggestions spread over five named reviewers with different
editorial habits, a realistic mix of edit kinds, natural overlap where two
people touched the same sentence, a few insertions nested inside other
people's pending insertions, and a handful of anchored comment threads
asking the sort of question reviewers actually ask.

Three properties are load-bearing.

**Deterministic.** Everything is drawn from one ``random.Random(seed)``.
The same seed yields the same corpus, byte for byte, which is what lets the
generated corpus live in git and be regenerated as a test.

**Located linguistically.** Positions come from :mod:`.edits`, which finds
them by scanning prose for phrases a copyeditor would actually change. No
step of this module ever picks a character offset.

**Base-coordinate tracking.** Suggestions accumulate; insertions physically
lengthen the display text, so an offset measured in the base document stops
being valid the moment anything is inserted before it. :class:`SpanTracker`
maintains the base-to-live mapping, which is what lets a later edit land on
*the same word* rather than on whatever happens to sit at that index now --
and it is what makes overlap between two reviewers a property of the prose
rather than of an index collision.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

from mockdocs.model import MockDoc, Segment

from llmux.scenarios.primitives import Move, SeedBuilder
from llmux.scenarios.stressgen.edits import Candidate, all_sentences, candidates
from llmux.scenarios.stressgen.prose import Document


class WalkError(RuntimeError):
    """The walk could not reach its target suggestion count."""


# ---------------------------------------------------------------------------
# base <-> live coordinate tracking
# ---------------------------------------------------------------------------


class SpanTracker:
    """Maps base-text grapheme offsets to live-document offsets.

    Only insertions move things: SPEC §5.2 marks characters deleted without
    removing them, so every base character stays in the array, in order, for
    the whole seeding phase. That makes the mapping a running offset rather
    than a diff.
    """

    def __init__(self, length: int) -> None:
        self._pos = list(range(length + 1))

    def cur(self, base_index: int) -> int:
        return self._pos[base_index]

    def shift(self, at: int, length: int) -> None:
        """Record ``length`` characters inserted at live offset ``at``."""
        if length <= 0:
            return
        for i, value in enumerate(self._pos):
            if value >= at:
                self._pos[i] = value + length


@dataclass(frozen=True)
class TrackedSpan:
    """A locator addressing the base text, resolved through the tracker.

    Duck-types :class:`llmux.scenarios.primitives.Locator`: ``Move`` calls
    ``resolve`` and ``segment``, and reads ``segment_id``/``tab_id`` to write
    the address into the seed op.
    """

    tracker: SpanTracker
    base_start: int
    base_end: int

    #: The stress corpus is a long body document with no headers, footers or
    #: extra tabs -- these locators name the default tab's body, which is
    #: the space they have always resolved in.
    segment_id: ClassVar[Optional[str]] = None
    tab_id: ClassVar[Optional[str]] = None

    def segment(self, doc: MockDoc) -> Segment:
        return doc.segment(None)

    def resolve(self, doc: MockDoc) -> tuple[int, int]:
        return (self.tracker.cur(self.base_start), self.tracker.cur(self.base_end))

    def describe(self) -> str:
        return f"base[{self.base_start}:{self.base_end}]"


@dataclass(frozen=True)
class LiveIndex:
    """A collapsed cursor at an already-computed live offset.

    Used only by the nesting pass, which reads the offset out of the live
    document immediately before applying the move, so it cannot go stale.
    """

    index: int

    #: Body of the default tab; see :class:`TrackedSpan`.
    segment_id: ClassVar[Optional[str]] = None
    tab_id: ClassVar[Optional[str]] = None

    def segment(self, doc: MockDoc) -> Segment:
        return doc.segment(None)

    def resolve(self, doc: MockDoc) -> tuple[int, int]:
        return (self.index, self.index)

    def describe(self) -> str:
        return f"live[{self.index}]"


# ---------------------------------------------------------------------------
# reviewers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reviewer:
    """One named reviewer with an editorial style.

    ``share`` is their slice of the suggestion budget; ``kind_bias``
    multiplies the base frequency of each edit kind, so a copyeditor makes
    many small fixes and a subject expert makes few large ones. Kinds absent
    from ``kind_bias`` keep their base weight.
    """

    name: str
    role: str
    share: float
    kind_bias: dict[str, float] = field(default_factory=dict)


#: The review panel. Five people, deliberately unequal: in a real review one
#: person makes half the marks and the manager makes almost none.
PANEL: tuple[Reviewer, ...] = (
    Reviewer(
        "priya",
        "copyeditor",
        0.34,
        {
            "uk_us_spelling": 3.0,
            "tighten_wordy": 2.5,
            "cut_intensifier": 2.5,
            "word_choice": 2.5,
            "that_which": 2.5,
            "cut_optional_that": 2.5,
            "cut_filler": 2.0,
            "number_format": 1.5,
            "rewrite_claim": 0.0,
            "cut_sentence": 0.0,
            "insert_citation": 0.2,
            "insert_caveat": 0.1,
            "insert_hedge": 0.2,
        },
    ),
    Reviewer(
        "marcus",
        "subject expert",
        0.20,
        {
            "rewrite_claim": 12.0,
            "hedge_weaken": 4.0,
            "insert_hedge": 3.0,
            "insert_caveat": 3.0,
            "cut_sentence": 2.5,
            "uk_us_spelling": 0.1,
            "that_which": 0.1,
            "cut_optional_that": 0.1,
            "word_choice": 0.3,
            "add_transition": 0.2,
        },
    ),
    Reviewer(
        "dana",
        "house style",
        0.22,
        {
            "terminology": 6.0,
            "cut_throat_clearing": 4.0,
            "split_run_on": 3.0,
            "add_transition": 2.5,
            "jargon_replace": 3.0,
            "passive_to_active": 2.5,
            "rewrite_claim": 0.0,
            "insert_citation": 0.2,
            "insert_hedge": 0.5,
        },
    ),
    Reviewer(
        "sam",
        "research assistant",
        0.14,
        {
            "insert_citation": 8.0,
            "insert_caveat": 4.0,
            "number_format": 4.0,
            "rewrite_claim": 0.0,
            "cut_sentence": 0.0,
            "cut_intensifier": 0.3,
            "uk_us_spelling": 0.3,
            "insert_hedge": 0.5,
        },
    ),
    Reviewer(
        "nadia",
        "managing editor",
        0.10,
        {
            "cut_sentence": 8.0,
            "hedge_strengthen": 6.0,
            "insert_hedge": 2.0,
            "cut_filler": 3.0,
            "rewrite_claim": 1.5,
            "uk_us_spelling": 0.1,
            "insert_citation": 0.1,
            "add_transition": 0.2,
        },
    ),
)


# ---------------------------------------------------------------------------
# reviewer questions (anchored comment threads)
# ---------------------------------------------------------------------------

QUESTIONS: tuple[str, ...] = (
    "Do we have a source for this? I could not find one in the draft folder.",
    "This reads as more confident than the evidence behind it. Can we soften?",
    "Is this still true as of this year? The figure looks like it is from the old draft.",
    "Who is the audience for this paragraph? It feels aimed at a different reader.",
    "Can we cut this? It repeats the point made two paragraphs above.",
    "I would like a number here rather than a qualitative claim.",
    "Flagging for legal review before this goes live.",
    "This is the strongest claim in the piece and it has no citation.",
    "Should this be a callout box rather than body text?",
    "Terminology: we use two different words for this across the piece.",
    "I disagree with this framing but I do not want to block on it.",
    "Can someone check the arithmetic here against the spreadsheet?",
    "This sentence is doing a lot of work. Consider splitting it.",
    "Is the hedging here deliberate, or a leftover from the earlier version?",
)


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """One move that was actually made, with the metadata reports need."""

    reviewer: str
    kind: str
    op: str
    base_start: int
    base_end: int
    section: str
    size: int
    #: Suggestion id at the moment it was created; may later be absorbed by
    #: a §6 merge, which is why the walk also keeps ``builder.named``.
    name: str


@dataclass
class Walk:
    """The result of one seeded walk: a seed spec plus what went into it."""

    document: Document
    seed: int
    target: int
    builder: SeedBuilder
    applied: list[Applied]
    nested: int
    overlaps: int
    comments: int

    @property
    def doc(self) -> MockDoc:
        return self.builder.doc

    def seed_spec(self) -> dict[str, Any]:
        return self.builder.seed_spec()

    def sid_of(self, applied: Applied) -> Optional[str]:
        """Surviving suggestion id for a move, following §6 merges."""
        return self.builder.named.get(applied.name)

    def kind_histogram(self) -> Counter[str]:
        return Counter(a.kind for a in self.applied)

    def reviewer_histogram(self) -> Counter[str]:
        return Counter(a.reviewer for a in self.applied)

    def size_histogram(self) -> Counter[str]:
        buckets: Counter[str] = Counter()
        for a in self.applied:
            if a.size <= 12:
                buckets["tiny (<=12 chars)"] += 1
            elif a.size <= 40:
                buckets["small (13-40)"] += 1
            elif a.size <= 100:
                buckets["medium (41-100)"] += 1
            else:
                buckets["large (>100)"] += 1
        return buckets


def _weighted(rng: random.Random, options: list[Any], weights: list[float]) -> Any:
    total = sum(weights)
    if total <= 0:
        raise WalkError("no weighted options left")
    draw = rng.random() * total
    for option, weight in zip(options, weights):
        draw -= weight
        if draw <= 0:
            return option
    return options[-1]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """True when two half-open spans share a character, or a cursor sits
    strictly inside a range (which is what nesting is made of)."""
    if a[0] == a[1] or b[0] == b[1]:
        cursor, span = (a, b) if a[0] == a[1] else (b, a)
        return span[0] < cursor[0] < span[1]
    return a[0] < b[1] and b[0] < a[1]


def _plan(
    rng: random.Random,
    document: Document,
    target: int,
    panel: tuple[Reviewer, ...],
    *,
    kind_share_cap: float,
    sentence_cap: int,
    overlap_fraction: float,
) -> list[tuple[Reviewer, Candidate]]:
    """Choose who edits what, before anything is applied.

    Planning ahead of application keeps the two concerns apart: this
    function decides the editorial content of the review, and
    :func:`run_walk` decides what the algebra does with it. Overlap is
    budgeted rather than accidental -- two reviewers land on the same
    sentence because the plan allowed it, up to ``overlap_fraction`` of the
    review.
    """
    from llmux.scenarios.stressgen.edits import KINDS

    all_candidates = candidates(document)
    pool: dict[str, list[Candidate]] = {}
    for candidate in all_candidates:
        pool.setdefault(candidate.kind, []).append(candidate)

    # Merges and GC can swallow a planned move, so plan a little more than
    # the target -- but only a little, because a plan that is mostly never
    # applied throws away the deliberate overlap pairs built below.
    budget = target + 12
    kind_cap = max(3, int(budget * kind_share_cap))
    overlap_target = int(target * overlap_fraction)

    chosen: list[tuple[Reviewer, Candidate]] = []
    taken: list[tuple[tuple[int, int], str]] = []
    per_kind: Counter[str] = Counter()
    per_sentence: Counter[tuple[int, int]] = Counter()
    used: set[tuple[int, int, str]] = set()
    overlaps_used = 0

    def try_add(reviewer: Reviewer, candidate: Candidate, allow_overlap: bool) -> bool:
        nonlocal overlaps_used
        key = (candidate.start, candidate.end, candidate.op)
        if key in used:
            return False
        if per_kind[candidate.kind] >= kind_cap:
            return False
        span = (candidate.start, candidate.end)
        clashing = [who for other, who in taken if _overlaps(span, other)]
        if clashing:
            # Same author overlapping himself merges into one card (§6) and
            # loses the edit. A different author is the interesting case.
            if reviewer.name in clashing or not allow_overlap:
                return False
            overlaps_used += 1
        elif per_sentence[candidate.sentence] >= sentence_cap:
            return False
        used.add(key)
        per_kind[candidate.kind] += 1
        per_sentence[candidate.sentence] += 1
        taken.append((span, reviewer.name))
        chosen.append((reviewer, candidate))
        return True

    reviewers = list(panel)
    shares = [r.share for r in reviewers]
    kinds = list(KINDS)

    # -- pass 1: independent edits, no two people on the same span ---------
    for _ in range(budget * 200):
        if len(chosen) >= budget - overlap_target:
            break
        reviewer = _weighted(rng, reviewers, shares)
        weights = [
            kind.weight * reviewer.kind_bias.get(kind.name, 1.0)
            if per_kind[kind.name] < kind_cap and pool.get(kind.name)
            else 0.0
            for kind in kinds
        ]
        if sum(weights) <= 0:
            break
        kind = _weighted(rng, kinds, weights)
        options = pool[kind.name]
        try_add(reviewer, options[rng.randrange(len(options))], allow_overlap=False)

    # -- pass 2: deliberate collisions ------------------------------------
    # Overlap in a real review is not an index accident: it is a second
    # editor working over a sentence someone has already touched, or an
    # expert rewriting a claim across a copyeditor's small fix. Build those
    # pairs explicitly, from the same candidate pool, so the overlapping
    # edits are still edits a person would make.
    hosts = list(chosen)
    rng.shuffle(hosts)
    for host_reviewer, host in hosts:
        if overlaps_used >= overlap_target or len(chosen) >= budget:
            break
        partners = [
            c
            for c in all_candidates
            if _overlaps((c.start, c.end), (host.start, host.end))
            and (c.start, c.end, c.op) not in used
        ]
        if not partners:
            continue
        rng.shuffle(partners)
        others = [r for r in reviewers if r.name != host_reviewer.name]
        for partner in partners[:6]:
            reviewer = _weighted(rng, others, [r.share for r in others])
            if try_add(reviewer, partner, allow_overlap=True):
                break

    if len(chosen) < target:
        raise WalkError(
            f"{document.key}: could only plan {len(chosen)} edits, need {target}. "
            f"The candidate pool ({len(all_candidates)} candidates) is too "
            f"small, or the caps are too tight."
        )
    rng.shuffle(chosen)
    return chosen


def run_walk(
    document: Document,
    *,
    seed: int,
    target: int,
    panel: tuple[Reviewer, ...] = PANEL,
    document_id: str,
    title: Optional[str] = None,
    me: str = "reviewer",
    comment_count: int = 0,
    nest_count: int = 0,
    kind_share_cap: float = 0.22,
    sentence_cap: int = 2,
    overlap_fraction: float = 0.18,
) -> Walk:
    """Apply a planned review to a fresh document until ``target`` cards stand.

    Stops at exactly ``target`` *live* suggestions, not ``target`` applied
    moves: §6 merges two of one author's abutting edits into one card and
    §11.1 garbage-collects a suggestion whose last marked character was
    struck by someone else. Counting the registry rather than the moves is
    what makes ``n_suggestions`` mean what a reviewer would count.
    """
    rng = random.Random(seed)
    builder = SeedBuilder(
        base_text=document.text,
        document_id=document_id,
        title=title or document.title,
        me=me,
    )
    tracker = SpanTracker(len(document.text))

    # Comments first: seeded before any move, so a reviewer's quote is the
    # sentence as written rather than the sentence with three other people's
    # marks in it.
    sentence_spans = [
        span
        for span in all_sentences(document.text)
        if span[1] - span[0] > 60 and document.text[span[0]].isupper()
    ]
    comments = 0
    if comment_count and sentence_spans:
        picks = rng.sample(sentence_spans, min(comment_count, len(sentence_spans)))
        for i, span in enumerate(sorted(picks)):
            reviewer = _weighted(rng, list(panel), [r.share for r in panel])
            builder.comment(
                QUESTIONS[(seed + i) % len(QUESTIONS)],
                quote=TrackedSpan(tracker, span[0], span[1]),
                author=reviewer.name,
            )
            comments += 1

    plan = _plan(
        rng,
        document,
        target,
        panel,
        kind_share_cap=kind_share_cap,
        sentence_cap=sentence_cap,
        overlap_fraction=overlap_fraction,
    )

    applied: list[Applied] = []
    overlaps = 0
    live_spans: list[tuple[int, int, str]] = []
    cursor = 0

    def apply_until(limit: int) -> None:
        nonlocal cursor, overlaps
        while cursor < len(plan) and len(builder.doc.registry) < limit:
            reviewer, candidate = plan[cursor]
            span = TrackedSpan(tracker, candidate.start, candidate.end)
            start, _end = span.resolve(builder.doc)
            op = candidate.op
            if op == "replace" and not candidate.text:
                op = "delete"  # a replacement with nothing on the right is a cut
            builder.move(
                f"m{cursor:04d}", Move(reviewer.name, op, span, candidate.text)
            )
            if op != "delete":
                tracker.shift(start, len(candidate.text))
            if any(
                _overlaps((candidate.start, candidate.end), (s, e))
                and who != reviewer.name
                for s, e, who in live_spans
            ):
                overlaps += 1
            live_spans.append((candidate.start, candidate.end, reviewer.name))
            applied.append(
                Applied(
                    reviewer=reviewer.name,
                    kind=candidate.kind,
                    op=op,
                    base_start=candidate.start,
                    base_end=candidate.end,
                    section=candidate.section,
                    size=candidate.size,
                    name=f"m{cursor:04d}",
                )
            )
            cursor += 1

    # Leave room for the nested insertions, then fill back up to target.
    apply_until(max(1, target - nest_count))
    nested = _nest(rng, builder, tracker, applied, panel, nest_count, target)
    apply_until(target)

    if len(builder.doc.registry) != target:
        raise WalkError(
            f"{document.key}: walk produced {len(builder.doc.registry)} live "
            f"suggestions, wanted {target} (applied {len(applied)} moves; "
            f"merges={len(builder.doc.merge_log)}, gc={len(builder.doc.gc_log)})"
        )

    return Walk(
        document=document,
        seed=seed,
        target=target,
        builder=builder,
        applied=applied,
        nested=nested,
        overlaps=overlaps,
        comments=comments,
    )


def _nest(
    rng: random.Random,
    builder: SeedBuilder,
    tracker: SpanTracker,
    applied: list[Applied],
    panel: tuple[Reviewer, ...],
    nest_count: int,
    target: int,
) -> int:
    """Insert inside another reviewer's pending insertion (SPEC §5.1).

    This is the conjunctive case: the nested characters carry both
    suggestion ids, so rejecting the outer insertion takes the inner one
    with it. It is the most surprising consequence of the model (spec §13.5)
    and the one a reviewer is most likely to mis-handle, so the corpus
    should contain a few rather than none.

    Offsets are read out of the live document immediately before the move,
    so nothing here can go stale.
    """
    if nest_count <= 0:
        return 0
    doc = builder.doc
    insertions = [a for a in applied if a.op == "insert"]
    rng.shuffle(insertions)
    made = 0
    for host in insertions:
        if made >= nest_count or len(doc.registry) >= target:
            break
        sid = builder.named.get(host.name)
        if sid is None or sid not in doc.registry:
            continue
        spans = doc.ranges()
        if sid not in spans:
            continue
        start, end = spans[sid]
        if end - start < 8:
            continue
        author = next(r for r in panel if r.name != host.reviewer)
        at = start + (end - start) // 2
        text = rng.choice((" roughly", " approximately", " we think", " reportedly"))
        builder.move(
            f"nest{made:02d}", Move(author.name, "insert", LiveIndex(at), text)
        )
        tracker.shift(at, len(text))
        made += 1
    return made
