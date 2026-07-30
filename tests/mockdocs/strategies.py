"""Hypothesis generators for the suggestion mock.

Random base documents (multi-paragraph, emoji-bearing) plus random
multi-author operation sequences. Ranges are drawn as fractions and clamped
at application time, because the valid range for operation *n* depends on the
document state that operations 0..n-1 produced.
"""

from __future__ import annotations

from hypothesis import strategies as st

from mockdocs.model import MockDoc

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
def op_specs(draw: st.DrawFn) -> dict:
    """One edit operation, with positions as fractions to be clamped later."""
    return {
        "kind": draw(st.sampled_from(["insert", "delete", "replace"])),
        "author": draw(st.sampled_from(AUTHORS)),
        "start": draw(st.floats(min_value=0.0, max_value=1.0)),
        "span": draw(st.integers(min_value=1, max_value=6)),
        "text": draw(text_fragments),
    }


def apply_ops(doc: MockDoc, ops: list[dict], check: bool = True) -> MockDoc:
    """Apply clamped operations in order, asserting I1-I4 after each."""
    for op in ops:
        n = len(doc.chars)
        start = min(int(op["start"] * n), n) if n else 0
        end = min(start + op["span"], n)
        kind = op["kind"]
        if kind == "insert":
            doc.insert(start, op["text"], op["author"])
        elif kind == "delete":
            if end > start:
                doc.delete(start, end, op["author"])
        else:
            if end > start:
                doc.replace(start, end, op["text"], op["author"])
            else:
                doc.insert(start, op["text"], op["author"])
        if check:
            doc.check_invariants()
    return doc


@st.composite
def suggestion_docs(draw: st.DrawFn, max_ops: int = 6) -> MockDoc:
    """A document carrying a random multi-author suggestion state."""
    text = draw(base_texts())
    ops = draw(st.lists(op_specs(), min_size=0, max_size=max_ops))
    doc = MockDoc(text=text, document_id="mockdoc-prop", title="Property Doc")
    return apply_ops(doc, ops)


@st.composite
def small_suggestion_docs(draw: st.DrawFn) -> MockDoc:
    """Same, but biased small so exhaustive laws (L3) stay cheap."""
    doc = draw(suggestion_docs(max_ops=3))
    return doc
