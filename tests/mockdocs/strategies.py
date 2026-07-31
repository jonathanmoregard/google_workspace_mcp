"""Hypothesis generators for the suggestion mock.

Random base documents (multi-paragraph, emoji-bearing) plus random
multi-author operation sequences. Ranges are drawn as fractions and clamped
at application time, because the valid range for operation *n* depends on the
document state that operations 0..n-1 produced.

The **segment** a operation targets is drawn as a fraction too, for the same
reason and one more: a document's segment list is not known until its tabs and
headers have been built, and the point of the multi-segment generators is to
spread one op sequence over every coordinate space the document has. On a
single-tab body-only document -- which is what :func:`suggestion_docs`
produces and what every pre-existing scenario is -- there is exactly one
segment, so the fraction always resolves to the body and the generated
documents are bit-for-bit what they were before segments existed.
"""

from __future__ import annotations

from hypothesis import strategies as st

from mockdocs.model import FOOTER, FOOTNOTE, HEADER, MockDoc, Segment

AUTHORS = ("alice", "bob", "carol")

#: Astral-plane and combining characters are the point: the model counts
#: grapheme clusters, the API counts UTF-16 code units, and the adapter has to
#: keep them straight (spec §14).
INTERESTING_CHARS = (
    "a",
    "b",
    "c",
    " ",
    ".",
    "\n",
    "\U0001f600",  # emoji, 2 UTF-16 units
    "\U0001f389",
    "é",  # e + combining acute: 1 cluster, 2 code points
    "\U0001f1f8\U0001f1ea",  # regional indicator pair (flag)
    "ü",
)

text_fragments = st.lists(
    st.sampled_from(INTERESTING_CHARS), min_size=0, max_size=8
).map("".join)


@st.composite
def base_texts(draw: st.DrawFn) -> str:
    """A multi-paragraph base document ending in a newline, as real Docs
    bodies always do."""
    paragraphs = draw(
        st.lists(
            st.text(alphabet="abcde 🎉é.", min_size=0, max_size=14),
            min_size=1,
            max_size=3,
        )
    )
    return "".join(p + "\n" for p in paragraphs)


@st.composite
def segment_texts(draw: st.DrawFn) -> str:
    """A header/footer/footnote base text: short, and one or two paragraphs.

    Real non-body segments are small, and keeping them small here is load
    bearing rather than cosmetic: their index space starts at 0, so a short
    segment puts a large share of the generated ops on and around index 0 --
    the position that only exists outside the body and that a proto3 payload
    never spells out.
    """
    paragraphs = draw(
        st.lists(
            st.text(alphabet="abcde 🎉é.", min_size=0, max_size=8),
            min_size=1,
            max_size=2,
        )
    )
    return "".join(p + "\n" for p in paragraphs)


@st.composite
def op_specs(draw: st.DrawFn) -> dict:
    """One edit operation, with position and target segment as fractions to be
    clamped later."""
    return {
        "kind": draw(st.sampled_from(["insert", "delete", "replace"])),
        "author": draw(st.sampled_from(AUTHORS)),
        "segment": draw(st.floats(min_value=0.0, max_value=1.0)),
        "start": draw(st.floats(min_value=0.0, max_value=1.0)),
        "span": draw(st.integers(min_value=1, max_value=6)),
        "text": draw(text_fragments),
    }


def pick_segment(doc: MockDoc, fraction: float) -> Segment:
    """Resolve an op's ``segment`` fraction against the document's segments.

    Document order, so the body of the first tab is always fraction 0 and a
    body-only document always resolves to its body whatever the fraction is.
    """
    segments = doc.ordered_segments()
    return segments[min(int(fraction * len(segments)), len(segments) - 1)]


def apply_ops(doc: MockDoc, ops: list[dict], check: bool = True) -> MockDoc:
    """Apply clamped operations in order, asserting I1-I5 after each.

    Each op is clamped to **its own segment's** length, which is the only
    length that means anything to it: an index clamped against the document's
    total character count would be out of range in every segment but the
    largest, and clamped against the body would land wherever the header
    happened to be long enough to accept it.
    """
    for op in ops:
        segment = pick_segment(doc, op.get("segment", 0.0))
        key = segment.key
        n = len(segment.chars)
        start = min(int(op["start"] * n), n) if n else 0
        end = min(start + op["span"], n)
        kind = op["kind"]
        if kind == "insert":
            doc.insert(start, op["text"], op["author"], key)
        elif kind == "delete":
            if end > start:
                doc.delete(start, end, op["author"], key)
        else:
            if end > start:
                doc.replace(start, end, op["text"], op["author"], key)
            else:
                doc.insert(start, op["text"], op["author"], key)
        if check:
            doc.check_invariants()
    return doc


@st.composite
def suggestion_docs(draw: st.DrawFn, max_ops: int = 6) -> MockDoc:
    """A single-tab, body-only document carrying a random multi-author
    suggestion state -- the shape every pre-tabs scenario has."""
    text = draw(base_texts())
    ops = draw(st.lists(op_specs(), min_size=0, max_size=max_ops))
    doc = MockDoc(text=text, document_id="mockdoc-prop", title="Property Doc")
    return apply_ops(doc, ops)


@st.composite
def tabbed_docs(draw: st.DrawFn, max_ops: int = 6) -> MockDoc:
    """A document with several tabs and non-body segments, edited in all of
    them.

    Deliberately *includes* the degenerate single-tab body-only case: the two
    shapes have to satisfy the same laws, and a generator that could only
    produce exotic documents would stop testing the common one.
    """
    doc = MockDoc(
        text=draw(base_texts()), document_id="mockdoc-prop", title="Property Doc"
    )
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        doc.add_tab(text=draw(base_texts()))
    for tab in list(doc.tabs):
        kinds = draw(
            st.lists(
                st.sampled_from([HEADER, FOOTER, FOOTNOTE]),
                min_size=0,
                max_size=2,
                unique=True,
            )
        )
        for kind in kinds:
            doc.add_segment(kind, text=draw(segment_texts()), tab_id=tab.tab_id)
    ops = draw(st.lists(op_specs(), min_size=0, max_size=max_ops))
    return apply_ops(doc, ops)


def any_docs(max_ops: int = 6) -> st.SearchStrategy[MockDoc]:
    """Both shapes at once: the single-tab body-only document every existing
    scenario is, and the multi-tab multi-segment one prod actually serves.

    SPEC §11's laws are stated over a document, not over a body, so every law
    that survived the flat-array model has to survive this one unchanged.
    """
    return st.one_of(suggestion_docs(max_ops=max_ops), tabbed_docs(max_ops=max_ops))


@st.composite
def small_suggestion_docs(draw: st.DrawFn) -> MockDoc:
    """Same, but biased small so exhaustive laws (L3) stay cheap."""
    doc = draw(any_docs(max_ops=3))
    return doc
