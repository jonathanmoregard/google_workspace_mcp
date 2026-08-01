"""Algebraic primitives scenarios are composed from.

Three layers, all of which resolve against the model rather than against
hand-counted integers:

**Locators** address text by content (``span("legacy")``,
``after("Plan.")``) and resolve to *grapheme* indexes in the document as it
stands at that moment. Nothing in a scenario definition ever writes a
document index, which is what keeps a corpus editable: change the base text
and every op, every oracle step and the whole ground truth move with it.

**Moves** are the SPEC §5 edit operations (§5.1 insert, §5.2 delete, §5.3
replace) with a locator instead of an index. Applying them to a
:class:`~mockdocs.model.MockDoc` yields both the seed op dict that
``FakeBackend.seed`` replays and the surviving suggestion id -- surviving
because §6 merge may absorb the fresh suggestion into an existing one, which
is exactly the granularity loss (L8) several scenarios are built on.

**Predicates** are the task language: ``by_author("alice")``,
``kind_is("Delete")``, ``deletes_part_of("legacy")``, ``is_noop()``. A brief
states a predicate in English; the generator evaluates the same predicate
against the seeded registry to derive which suggestions the intended
solution resolves. The two must say the same thing -- that correspondence is
the one part of a scenario a human still has to get right, so predicates are
deliberately few and sharply defined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mockdocs.fake_services import FakeBackend
from mockdocs.graphemes import split_graphemes
from mockdocs.model import MockDoc, Segment, SegmentKey


class ScenarioError(RuntimeError):
    """A scenario could not be built: bad locator, empty predicate, ..."""


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------


def grapheme_spans(
    doc: MockDoc, needle: str, *, segment: Optional[SegmentKey] = None
) -> list[tuple[int, int]]:
    """Every ``[start, end)`` grapheme span of ``needle`` in ONE segment.

    Grapheme space, not code-point space: with an astral emoji in the
    document the two differ, and the model indexes in graphemes.

    ``segment`` is a ``(tab_id, segment_id)`` key; ``None`` means the default
    tab's body, which is the whole document for a single-tab body-only one.
    Searching across segments would be meaningless: Docs numbers each from
    its own start, so an offset found in a header does not name a position
    anywhere else.
    """
    clusters = split_graphemes(needle)
    if not clusters:
        raise ScenarioError("locator text must be non-empty")
    hay = [c.cp for c in doc.segment(segment).chars]
    n = len(clusters)
    return [(i, i + n) for i in range(len(hay) - n + 1) if hay[i : i + n] == clusters]


@dataclass(frozen=True)
class Locator:
    """Content-addressed position or range within one ``(tab, segment)``.

    ``segment_id``/``tab_id`` name the coordinate space; omitting both means
    the default tab's body, which is what every pre-segments scenario meant
    and still means. They are part of the locator rather than of the step
    because the address travels with the position: a step that knew the
    index but not the space would emit exactly the bare index this corpus
    exists to stop teaching.
    """

    text: str
    where: str = "span"  # span | start | end
    occurrence: int = 0
    segment_id: Optional[str] = None
    tab_id: Optional[str] = None

    def segment(self, doc: MockDoc) -> Segment:
        """The segment this locator resolves in."""
        try:
            return doc.resolve_segment(tab_id=self.tab_id, segment_id=self.segment_id)
        except Exception as error:
            raise ScenarioError(f"{self.describe()}: {error}") from error

    def resolve(self, doc: MockDoc) -> tuple[int, int]:
        segment = self.segment(doc)
        spans = grapheme_spans(doc, self.text, segment=segment.key)
        if len(spans) <= self.occurrence:
            raise ScenarioError(
                f"locator {self.text!r} occurrence {self.occurrence} not found "
                f"({len(spans)} occurrence(s) in {segment.describe()}: "
                f"{MockDoc.text_of(segment.chars)!r})"
            )
        start, end = spans[self.occurrence]
        if self.where == "start":
            return (start, start)
        if self.where == "end":
            return (end, end)
        return (start, end)

    def describe(self) -> str:
        where = ""
        if self.segment_id or self.tab_id:
            where = f"@{self.tab_id or 'default'}/{self.segment_id or 'body'}"
        return f"{self.where}({self.text!r}#{self.occurrence}){where}"


def span(
    text: str,
    occurrence: int = 0,
    *,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> Locator:
    """The range covering ``text`` itself."""
    return Locator(text, "span", occurrence, segment_id, tab_id)


def before(
    text: str,
    occurrence: int = 0,
    *,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> Locator:
    """The collapsed cursor immediately before ``text``."""
    return Locator(text, "start", occurrence, segment_id, tab_id)


def after(
    text: str,
    occurrence: int = 0,
    *,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> Locator:
    """The collapsed cursor immediately after ``text``."""
    return Locator(text, "end", occurrence, segment_id, tab_id)


# ---------------------------------------------------------------------------
# Moves (SPEC §5, located by content)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """One §5 edit operation. ``apply`` returns ``(seed_op, surviving_id)``."""

    author: str
    kind: str  # insert | delete | replace
    target: Locator
    text: str = ""

    def apply(self, doc: MockDoc) -> tuple[dict[str, Any], Optional[str]]:
        segment = self.target.segment(doc)
        start, end = self.target.resolve(doc)
        # The seed op names its segment for the same reason a tool call has
        # to: replayed without it, an index computed in a header would be
        # applied to the body -- silently, at a completely different place.
        where: dict[str, Any] = {}
        if self.target.segment_id is not None:
            where["segment_id"] = self.target.segment_id
        if self.target.tab_id is not None:
            where["tab_id"] = self.target.tab_id
        key = segment.key
        if self.kind == "insert":
            op = {
                "op": "insert",
                "index": start,
                "text": self.text,
                "author": self.author,
                **where,
            }
            return op, doc.insert(start, self.text, self.author, key)
        if self.kind == "delete":
            op = {
                "op": "delete",
                "start": start,
                "end": end,
                "author": self.author,
                **where,
            }
            return op, doc.delete(start, end, self.author, key)
        if self.kind == "replace":
            op = {
                "op": "replace",
                "start": start,
                "end": end,
                "text": self.text,
                "author": self.author,
                **where,
            }
            return op, doc.replace(start, end, self.text, self.author, key)
        raise ScenarioError(f"unknown move kind {self.kind!r}")


def add(author: str, at: Locator, text: str) -> Move:
    """§5.1 -- suggest inserting ``text`` at a collapsed cursor."""
    return Move(author, "insert", at, text)


def remove(author: str, target: Locator) -> Move:
    """§5.2 -- suggest deleting a range (marks, never removes)."""
    return Move(author, "delete", target)


def rewrite(author: str, target: Locator, text: str) -> Move:
    """§5.3 -- suggest replacing a range: 5.2 then 5.1, merged by §6."""
    return Move(author, "replace", target, text)


# ---------------------------------------------------------------------------
# Seed construction
# ---------------------------------------------------------------------------


@dataclass
class SeedBuilder:
    """Accumulates a ``FakeBackend.seed`` spec while mirroring the model.

    The mirror exists so that a move's locator sees its predecessors' edits
    (seed ops are replayed in order, each against the document as it then
    stands) and so that the id a move *ends up as* is known: §6 may absorb it
    into an earlier same-author suggestion, and every later reference has to
    follow that rename.
    """

    base_text: str
    document_id: str
    title: str = "Mock Document"
    me: str = "reviewer"
    doc: MockDoc = field(init=False)
    ops: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    named: dict[str, str] = field(default_factory=dict)
    #: Declared non-body segments, keyed by the seed spec's container name
    #: ("headers"/"footers"/"footnotes") -> {segment_id: base text}.
    segments: dict[str, dict[str, str]] = field(default_factory=dict)
    _merge_watermark: int = 0

    def __post_init__(self) -> None:
        self.doc = MockDoc(
            text=self.base_text, document_id=self.document_id, title=self.title
        )

    def segment(self, kind: str, segment_id: str, text: str) -> str:
        """Declare a header/footer/footnote on the default tab.

        Must be called before any move that targets it. Returns the segment
        id so a scenario can hand it straight to ``span(..., segment_id=...)``
        rather than repeating the string.
        """
        self.doc.add_segment(kind, text=text, segment_id=segment_id)
        self.segments.setdefault(f"{kind}s", {})[segment_id] = text
        return segment_id

    def move(self, name: str, move: Move) -> str:
        """Apply a move, record its seed op, remember its surviving id."""
        op, sid = move.apply(self.doc)
        self.ops.append(op)
        self._follow_merges()
        if sid is None:
            raise ScenarioError(f"move {name!r} produced no suggestion: {move}")
        self.named[name] = sid
        return sid

    def _follow_merges(self) -> None:
        """Rewrite remembered ids that §6 absorbed since the last move."""
        for survivor, absorbed in self.doc.merge_log[self._merge_watermark :]:
            for key, value in list(self.named.items()):
                if value == absorbed:
                    self.named[key] = survivor
        self._merge_watermark = len(self.doc.merge_log)

    def comment(
        self, content: str, quote: Optional[Locator] = None, author: str = "alice"
    ) -> None:
        """Seed a pre-existing comment thread (Drive comment surface)."""
        quote_text = ""
        if quote is not None:
            segment = quote.segment(self.doc)
            start, end = quote.resolve(self.doc)
            quote_text = MockDoc.text_of(segment.chars[start:end])
        self.comments.append(
            {"content": content, "quote": quote_text, "author": author}
        )

    def seed_spec(self) -> dict[str, Any]:
        return {
            "me": self.me,
            "documents": [
                {
                    "document_id": self.document_id,
                    "title": self.title,
                    "text": self.base_text,
                    **{k: dict(v) for k, v in self.segments.items()},
                    "suggestions": list(self.ops),
                    "comments": list(self.comments),
                }
            ],
        }


def seeded_backend(seed_spec: dict[str, Any]) -> tuple[FakeBackend, MockDoc]:
    """A fresh backend with ``seed_spec`` applied, plus its (single) document."""
    backend = FakeBackend()
    backend.seed(seed_spec)
    document_id = seed_spec["documents"][0]["document_id"]
    return backend, backend.get_document(document_id)


# ---------------------------------------------------------------------------
# Predicates -- the task language
# ---------------------------------------------------------------------------

Predicate = Callable[[MockDoc, str], bool]


def by_author(author: str) -> Predicate:
    """Authored by ``author``. Observable through the suggestion thread's
    headPost author (``list_document_suggestions`` -> ``author``)."""

    def pred(doc: MockDoc, sid: str) -> bool:
        return doc.registry[sid].author == author

    return pred


def kind_is(kind: str) -> Predicate:
    """§8 card kind: ``Add`` (adds only), ``Delete`` (removes only),
    ``Replace`` (both). Note that a §6 merge can turn two edits that each
    look like an Add and a Delete into a single Replace card."""

    def pred(doc: MockDoc, sid: str) -> bool:
        return doc.label(sid)["kind"] == kind

    return pred


def is_noop() -> Predicate:
    """The suggestion changes nothing: what it strikes equals what it adds,
    so accept and reject produce the same text (they differ only in *which*
    copy of the identical characters survives)."""

    def pred(doc: MockDoc, sid: str) -> bool:
        card = doc.label(sid)
        return bool(card["struck"]) and card["struck"] == card["added"]

    return pred


def deletes_part_of(word: str) -> Predicate:
    """Strikes at least one character of some occurrence of ``word``.

    Adjacency does not count -- an insertion butted against the word marks no
    character of it -- which is what makes "looks like it touches 'foo'"
    decoys separable from the real thing.
    """

    def pred(doc: MockDoc, sid: str) -> bool:
        # Per segment: an offset in a header is not an offset in the body,
        # so struck positions and word spans may only be intersected within
        # the one coordinate space they were both counted in.
        for segment in doc.ordered_segments():
            struck = {i for i, c in enumerate(segment.chars) if sid in c.dels}
            if not struck:
                continue
            if any(
                struck & set(range(start, end))
                for start, end in grapheme_spans(doc, word, segment=segment.key)
            ):
                return True
        return False

    return pred


def adds_text(fragment: str) -> Predicate:
    """The surviving added text (§8's ``added``) contains ``fragment``."""

    def pred(doc: MockDoc, sid: str) -> bool:
        return fragment in doc.label(sid)["added"]

    return pred


def spans_paragraph_break() -> Predicate:
    """Marks a ``'\\n'``: the suggestion crosses a block boundary."""

    def pred(doc: MockDoc, sid: str) -> bool:
        return any(c.cp == "\n" for _, _, c in doc.iter_chars() if sid in c.marks)

    return pred


def always() -> Predicate:
    def pred(doc: MockDoc, sid: str) -> bool:
        return True

    return pred


def p_and(*preds: Predicate) -> Predicate:
    def pred(doc: MockDoc, sid: str) -> bool:
        return all(p(doc, sid) for p in preds)

    return pred


def p_or(*preds: Predicate) -> Predicate:
    def pred(doc: MockDoc, sid: str) -> bool:
        return any(p(doc, sid) for p in preds)

    return pred


def p_not(inner: Predicate) -> Predicate:
    def pred(doc: MockDoc, sid: str) -> bool:
        return not inner(doc, sid)

    return pred


# ---------------------------------------------------------------------------
# Rules -> decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """First matching rule wins; ``action="none"`` means leave it pending."""

    predicate: Predicate
    action: str  # accept | reject | none


def ordered_suggestion_ids(doc: MockDoc) -> list[str]:
    """Live suggestion ids in document order (card order, §8).

    Delegated to :meth:`MockDoc.cards` rather than sorting on
    :meth:`MockDoc.ranges` directly: an index only orders ids WITHIN one
    segment, so sorting the flat map interleaved a header's card with the
    body's by comparing numbers counted from two different starts.
    """
    return [card["suggestion_id"] for card in doc.cards()]


def decide(doc: MockDoc, rules: list[Rule]) -> dict[str, str]:
    """Apply the rule list to every live suggestion, in card order."""
    decisions: dict[str, str] = {}
    for sid in ordered_suggestion_ids(doc):
        for rule in rules:
            if rule.predicate(doc, sid):
                if rule.action != "none":
                    decisions[sid] = rule.action
                break
    return decisions


def select(doc: MockDoc, predicate: Predicate) -> list[str]:
    """Live suggestion ids matching ``predicate``, in card order."""
    return [sid for sid in ordered_suggestion_ids(doc) if predicate(doc, sid)]
