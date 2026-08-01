"""Property tests for the suggestion model: SPEC §11 invariants and laws.

The spec was written to be run as property tests ("generate a random base
document plus a random sequence of edit operations from §5, then assert"), so
this file is a direct transcription of §11.1-§11.4.

Two places where the implementation had to resolve a spec ambiguity, both
worth re-reading if a law ever fails:

1. **§7 does not mention garbage collection**, but I2 ("check after every
   operation") requires it: accepting a suggestion whose deletion covers
   another suggestion's last marked char removes that char, leaving the other
   suggestion in the registry marking nothing. The model therefore runs GC
   after accept/reject as well as after §5 edits. Without it, ``test_i2_gc``
   fails on the very first generated example that overlaps two suggestions.

2. **L12 is stated globally but only holds per-suggestion.** A char with
   ``ins = {S}`` and ``dels = {T}`` is in ``added(S)`` (§8: ``S ∈ ins ∧
   S ∉ del``) but renders underline *and* strikethrough (§4 row 4), so it is
   not "underline-only" in the global rendering. The law holds when read as
   the per-suggestion projection of the rendering, which is exactly how §8
   defines ``struck``/``added``; the test asserts that reading.

3. **§11 says "the document" and the spec's document is one flat array.**
   Prod's is not: it is one array per ``(tab, segment)``. Every law below is
   therefore quantified over :func:`tests.mockdocs.strategies.any_docs`, which
   generates both the single-tab body-only document the spec imagined and the
   multi-tab multi-segment one the API actually serves, and every test that
   used to walk ``doc.chars`` now walks ``doc.display()`` -- the whole
   document, in document order, rather than the default tab's body. The two
   are the same list on a body-only document, which is why the change is
   invisible to the single-segment cases and load-bearing everywhere else.
"""

from __future__ import annotations

import itertools

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from mockdocs.model import (
    RENDER_BOTH,
    RENDER_DELETE,
    RENDER_INSERT,
    RENDER_NORMAL,
    MockDoc,
)
from tests.mockdocs.strategies import (
    any_docs,
    base_texts,
    op_specs,
    small_suggestion_docs,
    tabbed_docs,
)

SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

#: For the properties that only mean anything on a document which HAS a
#: mergeable pair. They reach that subset with ``assume(mergeable_pairs(doc))``,
#: which discards most generated documents -- so Hypothesis' filter_too_much
#: health check fires on an unlucky generation run (observed: 9 kept, 50
#: filtered) and reddens a suite whose properties all hold. Suppressed here
#: rather than in SETTINGS so every other property keeps the check.
FILTERED_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.filter_too_much,
    ],
)


# ---------------------------------------------------------------------------
# §11.1 structural invariants
# ---------------------------------------------------------------------------


@SETTINGS
@given(text=base_texts(), ops=st.lists(op_specs(), min_size=0, max_size=8))
def test_i1_i5_hold_after_every_operation(text, ops):
    """I1 (no orphan marks), I2 (GC), I3 (colour determinism), I4 (render
    totality), I5 (one home segment per suggestion) after each of a random
    multi-author op sequence."""
    from tests.mockdocs.strategies import apply_ops

    doc = MockDoc(text=text, document_id="d", title="t")
    doc.check_invariants()
    apply_ops(doc, ops)  # asserts after every op
    doc.check_invariants()


@SETTINGS
@given(doc=any_docs())
def test_i2_gc_after_resolution(doc):
    """I2 must survive accept/reject, not just §5 edits -- see the module
    docstring, item 1."""
    for sid in sorted(doc.registry):
        doc.accept(sid)
        doc.check_invariants()
    assert not doc.registry


@SETTINGS
@given(doc=any_docs())
def test_i4_render_states_are_total_and_exclusive(doc):
    for c in doc.display():
        state = c.render_state()
        assert state == (
            RENDER_BOTH
            if (c.ins and c.dels)
            else RENDER_INSERT
            if c.ins
            else RENDER_DELETE
            if c.dels
            else RENDER_NORMAL
        )


# ---------------------------------------------------------------------------
# §11.2 resolution laws
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=any_docs())
def test_l1_extremes(doc):
    """accept-all ≡ final(D); reject-all ≡ original(D). Each yields a document
    with no marks and an empty registry."""
    expected_final = doc.final_text()
    expected_original = doc.original_text()

    accepted = doc.clone()
    accepted.accept_all()
    assert accepted.display_text() == expected_final
    assert not accepted.registry
    assert all(not c.ins and not c.dels for c in accepted.display())

    rejected = doc.clone()
    rejected.reject_all()
    assert rejected.display_text() == expected_original
    assert not rejected.registry
    assert all(not c.ins and not c.dels for c in rejected.display())


@SETTINGS
@given(doc=any_docs())
def test_l2_resolution_is_destructive_idempotent(doc):
    for sid in sorted(doc.registry):
        once = doc.clone()
        assert once.accept(sid) is True
        after_first = once.display_text()
        assert once.accept(sid) is False  # no-op: S has left the registry
        assert once.display_text() == after_first

        twice = doc.clone()
        assert twice.reject(sid) is True
        after_first = twice.display_text()
        assert twice.reject(sid) is False
        assert twice.display_text() == after_first


@SETTINGS
@given(doc=small_suggestion_docs())
def test_l3_resolution_commutes(doc):
    """The spec's most valuable law: for distinct S, T and any
    f, g ∈ {accept, reject}, f(S) ∘ g(T) ≡ g(T) ∘ f(S).

    Exhaustive over documents with at most 3 live suggestions, as §11.2 asks.
    """
    sids = sorted(doc.registry)
    assume(len(sids) <= 3)
    for s, t in itertools.permutations(sids, 2):
        for f, g in itertools.product(("accept", "reject"), repeat=2):
            a = doc.clone()
            getattr(a, f)(s)
            getattr(a, g)(t)

            b = doc.clone()
            getattr(b, g)(t)
            getattr(b, f)(s)

            assert a.display_text() == b.display_text(), (
                f"L3 violated: {f}({s}) then {g}({t}) != reversed"
            )
            assert sorted(a.registry) == sorted(b.registry)
            assert [(sorted(c.ins), sorted(c.dels)) for c in a.display()] == [
                (sorted(c.ins), sorted(c.dels)) for c in b.display()
            ]


@SETTINGS
@given(doc=any_docs())
def test_l4_self_cancelling_spans(doc):
    """If S ∈ c.ins ∧ S ∈ c.del then c is in neither extreme, and both
    accept(S) and reject(S) remove it. (The "ul" case.)"""
    original = doc.original()
    final = doc.final()
    for sid in sorted(doc.registry):
        chars = doc.display()
        both = [i for i, c in enumerate(chars) if sid in c.ins and sid in c.dels]
        if not both:
            continue
        for i in both:
            c = chars[i]
            assert c not in original and c not in final

        marker = object()
        for action in ("accept", "reject"):
            resolved = doc.clone()
            targets = [id(resolved.display()[i]) for i in both]
            getattr(resolved, action)(sid)
            survivors = {id(c) for c in resolved.display()}
            assert not (set(targets) & survivors), (
                f"L4 violated: both-marks char survived {action}({sid})"
            )
        del marker


@SETTINGS
@given(doc=any_docs(), seed=st.data())
def test_l5_survival(doc, seed):
    """Once every suggestion is resolved, a char survives iff every suggestion
    in its ``ins`` was accepted and every suggestion in its ``dels`` was
    rejected."""
    sids = sorted(doc.registry)
    decisions = {
        sid: seed.draw(st.sampled_from(["accept", "reject"]), label=sid) for sid in sids
    }

    predicted = [
        c.cp
        for c in doc.display()
        if all(decisions[s] == "accept" for s in c.ins)
        and all(decisions[s] == "reject" for s in c.dels)
    ]

    resolved = doc.clone()
    for sid, action in decisions.items():
        getattr(resolved, action)(sid)

    assert resolved.display_text() == "".join(predicted)
    assert not resolved.registry


@SETTINGS
@given(doc=any_docs())
def test_l6_extremes_are_mark_free_and_stable(doc):
    for projection in ("original", "final"):
        chars = getattr(doc, projection)()
        stable = MockDoc(text="", document_id="d", title="t")
        stable.chars = [c.clone() for c in chars]
        # A projection of a projection is the projection itself.
        assert MockDoc.text_of(getattr(stable, projection)()) == MockDoc.text_of(chars)


# ---------------------------------------------------------------------------
# §11.3 merge laws
# ---------------------------------------------------------------------------


def mergeable_pairs(doc):
    """Every ordered pair §6 is allowed to merge: same author, same segment.

    The segment half is not in §6 -- §6 has one flat array and so cannot see
    the distinction -- but it is a precondition of the law, not a shortcut in
    the test. ``gap`` is a distance in characters, and there is no distance
    between two coordinate spaces; merging across one would let ``accept``
    on a card in the body silently dispose of a proposal in a header. See
    ``MockDoc._merge_around``.
    """
    sids = sorted(doc.registry)
    homes = {sid: doc.segment_of(sid) for sid in sids}
    return [
        (a, b)
        for a, b in itertools.permutations(sids, 2)
        if doc.registry[a].author == doc.registry[b].author
        and homes[a] is not None
        and homes[a].key == homes[b].key
    ]


@SETTINGS
@given(doc=any_docs())
def test_l7_merge_preserves_both_extremes(doc):
    """Merge may change granularity of choice; it must never change content.

    Exercised directly against ``_merge`` for every mergeable pair, which is
    stronger than only observing the merges §6 chose to perform.
    """
    for survivor, absorbed in mergeable_pairs(doc):
        merged = doc.clone()
        merged._merge(survivor, absorbed)
        assert merged.original_text() == doc.original_text()
        assert merged.final_text() == doc.final_text()
        merged.check_invariants()


@SETTINGS
@given(doc=any_docs())
def test_l9_merge_preserves_rendering(doc):
    """display(merge(D, s, a)) differs from display(D) only in run colour --
    and not at all when both share an author, which §6 guarantees."""
    for survivor, absorbed in mergeable_pairs(doc):
        merged = doc.clone()
        merged._merge(survivor, absorbed)
        assert merged.display_text() == doc.display_text()
        assert [c.render_state() for c in merged.display()] == [
            c.render_state() for c in doc.display()
        ]


@FILTERED_SETTINGS
@given(doc=any_docs())
def test_merge_migrates_threads(doc):
    """§10 recommended policy: the absorbed thread is concatenated onto the
    survivor ordered by createdAt, never silently dropped."""
    from mockdocs.model import Comment

    pairs = mergeable_pairs(doc)
    assume(pairs)
    survivor, absorbed = pairs[0]

    doc.registry[survivor].thread.append(Comment("p1", "alice", "keep me", 1))
    doc.registry[absorbed].thread.append(Comment("p2", "bob", "migrate me", 2))
    doc._merge(survivor, absorbed)

    contents = [p.content for p in doc.registry[survivor].thread]
    assert contents == ["keep me", "migrate me"]


@SETTINGS
@given(doc=tabbed_docs())
def test_merge_never_crosses_a_segment_or_a_tab(doc):
    """§6 as it has to be read once indexes are per-segment.

    The generators put same-author edits at the same numeric index in several
    segments on purpose: a merge implemented over a document-wide range map
    sees those as adjacent (both report ``(4, 5)``) and absorbs one into the
    other. It must not. The evidence is in ``merge_log``: every merge the
    model performed joined two suggestions that lived in one segment, and I5
    -- one home segment per suggestion -- still holds afterwards.
    """
    doc.check_invariants()  # I5 is the invariant a cross-segment merge breaks
    for survivor, _ in doc.merge_log:
        if survivor not in doc.registry:
            continue  # later resolved or absorbed in turn
        home = doc.segment_of(survivor)
        assert home is not None
        assert sum(sid == survivor for c in home.chars for sid in c.marks) == sum(
            sid == survivor for _, _, c in doc.iter_chars() for sid in c.marks
        ), f"{survivor} carries marks outside {home.describe()} after a merge"


# ---------------------------------------------------------------------------
# §11.4 label laws
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=any_docs())
def test_l12_label_render_agreement(doc):
    """The struck side of the label is every strikethrough-rendered char
    marked S; the added side is every char marked S that S does not also
    strike. See the module docstring, item 2, for why this is the
    per-suggestion reading."""
    for sid in sorted(doc.registry):
        card = doc.label(sid)

        struck_rendered = "".join(
            c.cp
            for c in doc.display()
            if sid in c.dels and c.render_state() in (RENDER_DELETE, RENDER_BOTH)
        )
        added_rendered = "".join(
            c.cp
            for c in doc.display()
            if sid in c.ins
            and sid not in c.dels
            and c.render_state() in (RENDER_INSERT, RENDER_BOTH)
        )
        assert card["struck"] == struck_rendered
        assert card["added"] == added_rendered

        if not card["added"]:
            assert card["kind"] == "Delete"
        elif not card["struck"]:
            assert card["kind"] == "Add"
        else:
            assert card["kind"] == "Replace"


@SETTINGS
@given(doc=any_docs())
def test_l11_label_is_recomputed_not_stored(doc):
    """label(S) depends only on the chars marked S, in document order."""
    for sid in sorted(doc.registry):
        assert doc.label(sid) == doc.clone().label(sid)


def test_spec_5_2_confirmed_docs_behaviour():
    """The one behaviour §5.2 says was confirmed against real Docs: adding
    text then deleting across it leaves the overlap struck *and* underlined,
    and the card reads Replace with only the surviving text on the added
    side (§8's load-bearing asymmetry)."""
    doc = MockDoc("The popular greeting.\n", "d", "t")
    start = doc.display_text().index("popular")
    doc.insert(start + 5, "ular", "alice")  # "popul" + "ular" + "ar"
    doc.delete(start, start + 5, "alice")  # delete "popul"

    (sid,) = doc.registry
    card = doc.label(sid)
    assert card["kind"] == "Replace"
    assert card["struck"] == "popul"
    assert card["added"] == "ular"
    assert doc.original_text() == "The popular greeting.\n"
    assert doc.final_text() == "The ularar greeting.\n"


def test_merge_is_same_author_only():
    """§6: cross-author merging would let one person's accept/reject silently
    dispose of another person's proposal."""
    doc = MockDoc("abcdefgh\n", "d", "t")
    doc.insert(2, "X", "alice")
    doc.insert(3, "Y", "bob")
    assert len(doc.registry) == 2
    authors = {s.author for s in doc.registry.values()}
    assert authors == {"alice", "bob"}


def test_5_1_insert_into_a_deleted_region_inherits_the_deletion():
    """§5.1's inheritance rule, stated there with its rationale: without it,
    "typing into the middle of a deleted region produces text that survives a
    deletion the user thinks they made"."""
    doc = MockDoc("keep DELETEME keep\n", "d", "t")
    start = doc.display_text().index("DELETEME")
    deletion = doc.delete(start, start + len("DELETEME"), "alice")

    doc.insert(start + 4, "xx", "bob")  # bob types inside alice's deletion
    typed = [c for c in doc.chars if c.cp == "x"]
    assert typed, "sanity: the typed chars are in the document"
    assert all(deletion in c.dels for c in typed), (
        "§5.1: text typed inside a deleted region must inherit the deletion"
    )

    doc.accept(deletion)
    assert doc.display_text() == "keep  keep\n"


@SETTINGS
@given(doc=any_docs())
def test_unmerged_deletion_marks_stay_contiguous(doc):
    """Structural consequence of §5.1's inheritance rule: the chars a *single*
    suggestion strikes are one contiguous block, because anything typed into
    the middle of a struck region joins that region.

    Scoped to suggestions that never absorbed another one. Hypothesis
    disproved the unscoped version: §6 merges on a suggestion's whole range
    (ins ∪ del), so two same-author suggestions whose *combined* ranges abut
    can merge even when their *deletion* sub-ranges do not, leaving the
    survivor striking a discontiguous set. That is legitimate -- it is
    exactly the granularity loss L8 predicts -- but it means a merged card's
    struck text is not necessarily a substring of the document (§8's ``flat``
    concatenates across the hole).

    Contiguity is asserted **inside the suggestion's own segment**: indexes
    from two segments cannot be compared, so "contiguous" is only a statement
    about one of them. I5 guarantees there is exactly one to look at.
    """
    merged_survivors = {survivor for survivor, _ in doc.merge_log}
    for sid in sorted(doc.registry):
        if sid in merged_survivors:
            continue
        home = doc.segment_of(sid)
        if home is None:
            continue
        struck = [i for i, c in enumerate(home.chars) if sid in c.dels]
        if not struck:
            continue
        assert struck == list(range(struck[0], struck[-1] + 1)), (
            f"deletion marks for unmerged {sid} are not contiguous in "
            f"{home.describe()}: {struck}"
        )


def test_5_1_nesting_requires_sitting_inside_the_other_insertion():
    """§5.1 says the new chars get ``ins = {S, T}`` when the cursor "sits
    inside another author's pending insertion T". The mock reads *inside* as
    "both neighbours carry T", so typing at the trailing edge of alice's
    insertion starts an independent suggestion rather than extending hers.

    The spec does not pin this boundary; the test exists to lock the chosen
    reading in place so a change to it is a deliberate one.
    """
    doc = MockDoc("ab\n", "d", "t")
    a = doc.insert(1, "XY", "alice")

    inside = doc.clone()
    inside.insert(2, "Z", "bob")  # between X and Y
    (z,) = [c for c in inside.chars if c.cp == "Z"]
    assert a in z.ins, "typing inside alice's insertion is conjunctive on it"

    edge = doc.clone()
    edge.insert(3, "Z", "bob")  # after Y, before the base 'b'
    (z,) = [c for c in edge.chars if c.cp == "Z"]
    assert a not in z.ins, "typing at the edge does not join alice's insertion"


def test_conjunctive_insertion_nesting():
    """§13.5 / §2: A inserts, B types inside it, reject A -> B's text goes too.
    The most surprising consequence of the conjunctive rule, pinned here so a
    future 'fix' has to argue with the spec."""
    doc = MockDoc("start end\n", "d", "t")
    a = doc.insert(5, "MIDDLE", "alice")
    doc.insert(8, "bb", "bob")
    inner = [c for c in doc.chars if c.cp == "b"]
    assert all({a} <= c.ins for c in inner), "nested insert must carry both ids"
    doc.reject(a)
    assert doc.display_text() == "start end\n"
