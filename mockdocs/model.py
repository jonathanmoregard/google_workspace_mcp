"""In-memory Google-Docs-style suggesting mode -- SPEC §2-§8 and §11.

Pure implementation of ``docs/plans/2026-07-30-suggestion-mock-spec.md``:
the two-layer data model (§2), the three projections (§3), the rendering
state table (§4), the edit operations (§5.1-§5.3), same-author merge (§6),
accept/reject (§7), and cards/labels (§8). Structural invariants I1-I5 are
checkable via :meth:`MockDoc.check_invariants`.

Deliberately NOT implemented (see the spec's §14 adapter addendum):

- §5.4 backspace-burst destructive deletion -- an editor-interaction
  behaviour with no MCP tool surface, so nothing under test can reach it.
- §9 undo -- likewise editor-only. (Nothing here is undo-safe as a result:
  merge is destructive per L10, exactly as the spec warns.)

Everything counts in **grapheme clusters**. UTF-16 conversion happens at the
API boundary in :mod:`mockdocs.adapter`, never here.

Tabs and segments (added 2026-07-31)
------------------------------------

SPEC §2 models one flat ``Char`` array, which is one document coordinate
space -- and that is a fiction Google Docs does not share. Docs numbers
**every ``(tabId, segmentId)`` pair from its own start**, so ``start_index:
5`` in a header and ``start_index: 5`` in the body are different characters,
and an index emitted or compared without the tab and segment it belongs to is
not an address at all. Three consecutive review rounds found that same bug
class in the production code, and every one of them was invisible here,
because a single flat array cannot represent a wrong-segment or wrong-tab
write. It can now: this module stores one ``Char`` array per segment.

Verified against the live enrolled API 2026-07-31, and matched exactly:

1. Each tab is numbered from its own start -- a two-tab document with a
   suggestion near the top of each reports ``start_index: 1`` for BOTH.
2. The body's first insertable position is index 1 (index 0 is the leading
   section break). This holds in EVERY tab, not just the first.
3. A header/footer/footnote segment is numbered from its OWN start, and
   **index 0 is a valid position there** (verified by inserting at
   ``{"index": 0, "segmentId": <headerId>}``).
4. Suggestion ids and comment threads are DOCUMENT-wide, not per-tab or
   per-segment, so :attr:`MockDoc.registry` stays a single flat map.

The compatibility seam is :attr:`MockDoc.chars`, a read/write property
proxying the default tab's body segment. Every caller that predates tabs --
``mockdocs.adapter``, ``mockdocs.fake_services``, ``mockdocs.concurrency``,
``llmux.scenarios`` and the existing tests -- keeps working unchanged on a
single-tab body-only document, which is what the overwhelming majority of
scenarios still are.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Optional

from mockdocs.graphemes import split_graphemes

#: SPEC §6 / open question §13.2. Zero means "abut or overlap only". The
#: value is a real product decision (L8), not a rendering tweak.
#:
#: **MEASURED against prod 2026-08-01, not guessed** (docs/findings/merge.md,
#: e2e/test_merge_semantics.py): a SUGGEST edit that touches an existing
#: same-author suggestion joins it; one unchanged character between them is
#: enough to keep two cards. Identical for all four insert/delete orderings,
#: so §13.2's suspicion that insert-then-insert and insert-then-delete might
#: differ does NOT hold. Not a time window either -- 130 s between the two
#: batches still joins.
#:
#: What prod does with that tolerance is not §6's merge, though. See
#: :meth:`MockDoc._merge` for the divergence this mock knowingly keeps.
MERGE_TOLERANCE = 0

#: §8: single-line label truncation width.
LABEL_MAX_CHARS = 60

#: §8 label quoting. The live API uses typographic quotes in
#: ``SuggestionThread.summaryText`` (verified 2026-07-30); the spec's ASCII
#: quotes were a guess and prod wins.
QUOTE_OPEN = "“"
QUOTE_CLOSE = "”"

# Rendering states, §4. Total and mutually exclusive (I4).
RENDER_NORMAL = "normal"
RENDER_INSERT = "insert"  # underline, author colour
RENDER_DELETE = "delete"  # strikethrough, author colour
RENDER_BOTH = "both"  # underline AND strikethrough
RENDER_STATES = (RENDER_NORMAL, RENDER_INSERT, RENDER_DELETE, RENDER_BOTH)

# Segment kinds. ``body`` is the one segment every tab has and the only one
# addressable without a ``segmentId``; the rest are the non-body segments the
# Docs API keys by id under ``headers``/``footers``/``footnotes``.
BODY = "body"
HEADER = "header"
FOOTER = "footer"
FOOTNOTE = "footnote"
SEGMENT_KINDS = (BODY, HEADER, FOOTER, FOOTNOTE)

#: Where each segment kind's index space starts -- **the whole point of the
#: segment model**. Verified against the live API 2026-07-31: a body's first
#: insertable position is 1 (index 0 is the leading section break, and this
#: holds in every tab), while a header/footer/footnote is numbered from its
#: own 0 and index 0 is a valid insert location there.
INDEX_BASE = {BODY: 1, HEADER: 0, FOOTER: 0, FOOTNOTE: 0}

#: Payload container and id field per non-body kind, as
#: ``docs/preview-api-reference.md`` records them:
#: ``headers: {segId: {"headerId": segId, "content": [...]}}``.
SEGMENT_CONTAINERS = {
    HEADER: ("headers", "headerId"),
    FOOTER: ("footers", "footerId"),
    FOOTNOTE: ("footnotes", "footnoteId"),
}

#: Document order within one tab: body first, then headers, footers,
#: footnotes -- the order ``gdocs_preview.analysis._collect_segments`` walks,
#: so a mock payload and its analysis agree on which segment is "first".
_KIND_ORDER = {BODY: 0, HEADER: 1, FOOTER: 2, FOOTNOTE: 3}

#: The first tab of every Google Doc. Verified 2026-07-31.
DEFAULT_TAB_ID = "t.0"

#: ``(tab_id, segment_id)``; ``segment_id is None`` means that tab's body.
SegmentKey = tuple[str, Optional[str]]


class MockDocsError(Exception):
    """Invalid operation against the model (adapter maps these to HTTP 400)."""


def opaque_id(namespace: str, n: int, width: int = 12) -> str:
    """A deterministic but *unordered-looking* id, base-36, ``width`` chars.

    Prod tab ids are ``t.0`` for the first tab and opaque after that
    (``t.sxw3lc9vb0lk``, verified 2026-07-31); header/footer/footnote ids are
    opaque too (``kix.…``). The mock reproduces the opacity on purpose rather
    than handing out ``t.1`` / ``kix.h1``: an id that looked ordered would let
    a caller sort on it, do arithmetic with it, or assume ``t.0`` is a prefix
    of every other tab -- and never find out here. This mock exists to make
    exactly that class of mistake fail.
    """
    digest = hashlib.sha1(f"{namespace}.{n}".encode("utf-8")).digest()
    value = int.from_bytes(digest, "big")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    for _ in range(width):
        value, rem = divmod(value, len(alphabet))
        out.append(alphabet[rem])
    return "".join(out)


@dataclass
class Comment:
    """One post in a suggestion's thread (§10)."""

    post_id: str
    author: str
    content: str
    created_at: int

    def clone(self) -> "Comment":
        return Comment(self.post_id, self.author, self.content, self.created_at)


@dataclass
class Suggestion:
    """SPEC §2. Identity persists across mark rewrites; that is what keeps a
    comment thread attached while its range changes shape."""

    id: str
    author: str
    created_at: int
    touched_at: int
    thread: list[Comment] = field(default_factory=list)

    def clone(self) -> "Suggestion":
        return Suggestion(
            self.id,
            self.author,
            self.created_at,
            self.touched_at,
            [c.clone() for c in self.thread],
        )


@dataclass
class Char:
    """SPEC §2.

    ``ins`` is **conjunctive** (the char exists only if every insertion mark
    is accepted; any rejection kills it). ``dels`` is **disjunctive** (the
    char survives only if every deletion mark is rejected; any acceptance
    kills it). Getting this backwards is the spec's stated single most likely
    source of bugs.

    Named ``dels`` rather than ``del`` because ``del`` is a Python keyword.
    """

    cp: str
    ins: set[str] = field(default_factory=set)
    dels: set[str] = field(default_factory=set)
    #: Author of the most recently applied mark -- backs ``runColour`` (§4).
    #: Not part of the spec's Char; carried here so I3 (colour determinism)
    #: is checkable as a pure function of the char.
    colour: Optional[str] = None
    #: Boolean character-style flags (``bold``, ``italic``, ...), sorted.
    #: Nothing in the suggestion algebra reads them -- SPEC §1/§12 keep style
    #: out of scope -- but the API CHUNKS on them, and that chunking is
    #: load-bearing downstream: prod splits a ``textRun`` at every style
    #: boundary, so one suggested deletion across a bold seam arrives as TWO
    #: deletion-marked runs (verified against the live API 2026-07-31,
    #: deleting "brave new" where only "brave" is bold). A mock that
    #: coalesced by mark set alone could not build that payload, so the whole
    #: class of "the code assumed one run per suggestion" was invisible to
    #: every unit test and every llmux scenario, and reachable only in prod.
    style: tuple[str, ...] = ()

    def clone(self) -> "Char":
        return Char(self.cp, set(self.ins), set(self.dels), self.colour, self.style)

    @property
    def marks(self) -> set[str]:
        return self.ins | self.dels

    def render_state(self) -> str:
        """§4's table, as a total function (I4)."""
        if self.ins and self.dels:
            return RENDER_BOTH
        if self.ins:
            return RENDER_INSERT
        if self.dels:
            return RENDER_DELETE
        return RENDER_NORMAL


def run_colour(char: Char) -> Optional[str]:
    """§4 / open question §13.1: cross-author colour precedence is unresolved.

    Interim rule per §4: the colour of the most recently applied mark. Pure
    function of the char (I3), and a one-line change once §13.1 is settled.
    """
    return char.colour


def flat(text: str) -> str:
    """§8's ``flat``: normalise for single-line display.

    Block boundaries become one space, whitespace runs collapse, and the
    result truncates at ``LABEL_MAX_CHARS`` with a trailing ellipsis.
    """
    collapsed = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if len(collapsed) > LABEL_MAX_CHARS:
        return collapsed[:LABEL_MAX_CHARS] + "…"
    return collapsed


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Distance in chars between two half-open ranges; 0 when they touch or
    overlap (§6's ``gap``)."""
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


@dataclass
class TabProperties:
    """``Tab.tabProperties`` -- ``{tabId, title, index}`` (verified
    2026-07-31). ``index`` is the tab's position among its siblings, which is
    the only ordering the API gives: the id itself carries none."""

    tab_id: str
    title: str
    index: int

    def clone(self) -> "TabProperties":
        return TabProperties(self.tab_id, self.title, self.index)


@dataclass
class Segment:
    """One ``(tab, segment)`` coordinate space and the chars living in it.

    A segment is the unit SPEC §2's ``Doc.chars`` should have been: an index
    only means something relative to one of these. ``index_base`` is what
    makes a header's first character index 0 while a body's is index 1, and
    :meth:`MockDoc.insert` and friends take a segment precisely so that a
    write into the wrong one is representable.
    """

    kind: str
    tab_id: str
    segment_id: Optional[str]
    chars: list[Char] = field(default_factory=list)

    @property
    def key(self) -> SegmentKey:
        return (self.tab_id, self.segment_id)

    @property
    def index_base(self) -> int:
        """First addressable index in this segment: 1 for a body, 0 else."""
        return INDEX_BASE[self.kind]

    @property
    def is_body(self) -> bool:
        return self.kind == BODY

    def clone(self) -> "Segment":
        return Segment(
            self.kind, self.tab_id, self.segment_id, [c.clone() for c in self.chars]
        )

    def describe(self) -> str:
        return f"tab {self.tab_id} {self.kind} {self.segment_id or '(body)'}"


class MockDoc:
    """A document: one ``Char`` array per ``(tab, segment)`` plus one
    document-wide suggestion registry (§2, extended per this module's header).

    ``'\\n'`` separates blocks, per §2 and §12's rationale for the flat
    representation -- flat *within a segment*, which is as flat as Docs gets.

    A freshly constructed document is single-tab (``t.0``) and body-only, so
    it is byte-for-byte the pre-tabs model; :meth:`add_tab` and
    :meth:`add_segment` are what make a wrong-tab or wrong-segment write
    reachable.
    """

    def __init__(
        self,
        text: str = "",
        document_id: str = "mockdoc-1",
        title: str = "Mock Document",
    ) -> None:
        self.document_id = document_id
        self.title = title
        self.tabs: list[TabProperties] = [TabProperties(DEFAULT_TAB_ID, "Tab 1", 0)]
        self.segments: dict[SegmentKey, Segment] = {
            (DEFAULT_TAB_ID, None): Segment(
                BODY, DEFAULT_TAB_ID, None, [Char(cp) for cp in split_graphemes(text)]
            )
        }
        self.registry: dict[str, Suggestion] = {}
        self._clock = 0
        self._counters: dict[str, int] = {}
        #: Merge events. No longer flagged as UNCERTAIN: the live API was
        #: measured on 2026-08-02 (docs/findings/merge.md). It DOES join
        #: adjacent same-author edits across separate batches, at tolerance 0
        #: and with no insert/delete asymmetry — but it does so by ABSORBING a
        #: new edit into a pre-existing card, never by merging two cards that
        #: already exist. The mock still does the latter, so this log stays:
        #: it is the diff between the two models.
        self.merge_log: list[tuple[str, str]] = []
        #: GC events (§11.1 I2). §10 says warn in dev builds: a GC usually
        #: means a bug, and it silently drops a comment thread.
        self.gc_log: list[str] = []

    # -- tabs and segments -----------------------------------------------
    @property
    def default_tab_id(self) -> str:
        """The tab a request that names none lands in.

        This is the footgun the whole segment model exists to reproduce: the
        API resolves an omitted ``tabId`` silently, so a caller that forgot
        one gets a successful write into the wrong tab rather than an error.
        """
        return self.tabs[0].tab_id

    @property
    def default_key(self) -> SegmentKey:
        """The default tab's body -- what :attr:`chars` proxies."""
        return (self.default_tab_id, None)

    @property
    def chars(self) -> list[Char]:
        """The default tab's body chars.

        **Compatibility seam.** Every caller written before segments existed
        reads and writes this, and on a single-tab body-only document it is
        the whole document. The list is the live one, not a copy: callers do
        ``doc.chars[i:i] = …``, ``del doc.chars[a:b]`` and ``doc.chars[0] =
        …`` and must keep mutating the document.
        """
        return self.segments[self.default_key].chars

    @chars.setter
    def chars(self, value: Iterable[Char]) -> None:
        self.segments[self.default_key].chars = list(value)

    def ordered_segments(self) -> list[Segment]:
        """Every segment in document order: tabs in ``index`` order, and
        within a tab body, headers, footers, footnotes (each by id).

        The same order :func:`gdocs_preview.analysis._collect_segments`
        walks, so "the segment a suggestion is in" means one thing on both
        sides of the API boundary.
        """
        position = {tab.tab_id: i for i, tab in enumerate(self.tabs)}
        return sorted(
            self.segments.values(),
            key=lambda s: (
                position.get(s.tab_id, len(position)),
                _KIND_ORDER[s.kind],
                s.segment_id or "",
            ),
        )

    def tab_segments(self, tab_id: str, kind: Optional[str] = None) -> list[Segment]:
        """One tab's segments, optionally of one kind, in document order."""
        return [
            s
            for s in self.ordered_segments()
            if s.tab_id == tab_id and (kind is None or s.kind == kind)
        ]

    def iter_chars(self) -> Iterator[tuple[Segment, int, Char]]:
        """``(segment, index-within-segment, char)`` over the whole document."""
        for segment in self.ordered_segments():
            for i, char in enumerate(segment.chars):
                yield segment, i, char

    def add_tab(
        self,
        text: str = "\n",
        tab_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> TabProperties:
        """Append a tab with its own body, numbered from its own index 1.

        ``addDocumentTab`` is the request that creates one in the real API,
        and it is unsupported in SUGGEST write mode (it is already in
        ``adapter.SUGGEST_UNSUPPORTED_OFFICIAL``) -- so this is a seeding /
        fixture operation, not something a tool under test can reach.
        """
        if tab_id is None:
            tab_id = "t." + opaque_id("mockdocs.tab", len(self.tabs))
        if any(tab.tab_id == tab_id for tab in self.tabs):
            raise MockDocsError(f"tab {tab_id!r} already exists")
        tab = TabProperties(
            tab_id, title or f"Tab {len(self.tabs) + 1}", len(self.tabs)
        )
        self.tabs.append(tab)
        self.segments[(tab_id, None)] = Segment(
            BODY, tab_id, None, [Char(cp) for cp in split_graphemes(text)]
        )
        return tab

    def add_segment(
        self,
        kind: str,
        text: str = "\n",
        segment_id: Optional[str] = None,
        tab_id: Optional[str] = None,
    ) -> Segment:
        """Add a header/footer/footnote segment to a tab.

        Its first character is at index **0**, not 1: a non-body segment is
        numbered from its own start and index 0 is addressable there
        (verified against the live API 2026-07-31 by inserting at
        ``{"index": 0, "segmentId": <headerId>}``).
        """
        if kind not in SEGMENT_CONTAINERS:
            raise MockDocsError(
                f"{kind!r} is not a non-body segment kind; expected one of "
                f"{', '.join(sorted(SEGMENT_CONTAINERS))}"
            )
        tab_id = tab_id or self.default_tab_id
        if not any(tab.tab_id == tab_id for tab in self.tabs):
            raise MockDocsError(f"no tab {tab_id!r} in this document")
        if segment_id is None:
            segment_id = "kix." + opaque_id(
                f"mockdocs.segment.{kind}", len(self.segments)
            )
        # Prod MINTS segment ids document-wide but RESOLVES them per tab, and
        # conflating the two is what let a header entry travel without its
        # tab. Verified against the live API 2026-07-31: creating a header in
        # each of two tabs returned two DIFFERENT ``kix.…`` ids, so refusing a
        # duplicate builds the documents Docs builds -- while a request naming
        # a segment id that belongs to another tab is REJECTED ("Segment with
        # ID kix.… was not found"), so an id is not an address on its own,
        # whatever its uniqueness. That half is :meth:`resolve_segment`.
        if any(s.segment_id == segment_id for s in self.segments.values()):
            raise MockDocsError(
                f"segment id {segment_id!r} is already used in this document "
                "(Docs mints segment ids document-wide, though it resolves "
                "them per tab)"
            )
        segment = Segment(
            kind, tab_id, segment_id, [Char(cp) for cp in split_graphemes(text)]
        )
        self.segments[segment.key] = segment
        return segment

    def style_range(
        self,
        start: int,
        end: int,
        *flags: str,
        segment: Optional[SegmentKey] = None,
    ) -> None:
        """Seed character styling over ``[start, end)`` -- a FIXTURE operation.

        Style suggestions are out of scope (SPEC §1/§12) and no tool under
        test emits ``updateTextStyle`` in a way the mock applies, so this is
        the seeding counterpart of :meth:`add_tab` / :meth:`add_segment`: it
        exists so a scenario can build the payload prod builds. What it
        changes is CHUNKING -- :func:`mockdocs.adapter._coalesce_runs` breaks
        a run at every style boundary, as prod does -- and nothing else. The
        suggestion algebra never reads ``Char.style``.
        """
        seg = self.segment(segment)
        if not 0 <= start <= end <= len(seg.chars):
            raise MockDocsError(
                f"style range [{start}, {end}) out of range "
                f"[0, {len(seg.chars)}] in {seg.describe()}"
            )
        value = tuple(sorted(set(flags)))
        for c in seg.chars[start:end]:
            c.style = value

    def segment(self, key: Optional[SegmentKey] = None) -> Segment:
        """Resolve a segment key; ``None`` means the default tab's body."""
        if key is None:
            return self.segments[self.default_key]
        if isinstance(key, Segment):  # tolerated: callers hold Segment objects
            return key
        return self.resolve_segment(tab_id=key[0], segment_id=key[1])

    def resolve_segment(
        self, tab_id: Optional[str] = None, segment_id: Optional[str] = None
    ) -> Segment:
        """Resolve a request's ``tabId``/``segmentId`` pair to a segment.

        **An omitted ``tabId`` resolves to the default tab, silently.** That
        is the API's own behaviour and the reason a multi-tab document is
        dangerous: the write succeeds, in the wrong place. An omitted
        ``segmentId`` means that tab's body, which is likewise silent.
        An id that names nothing is an error, as it is in prod.

        **A segment id is resolved WITHIN the resolved tab.** A header id
        from tab 2 handed to a request that names no tab does not find that
        header; it fails to find anything in tab 1. The message is prod's own,
        verified verbatim against the live API 2026-07-31 by inserting at
        ``{"index": 0, "segmentId": <a second tab's headerId>}``.
        """
        tab_id = tab_id or self.default_tab_id
        if not any(tab.tab_id == tab_id for tab in self.tabs):
            raise MockDocsError(f"Invalid tab ID {tab_id}.")
        key: SegmentKey = (tab_id, segment_id or None)
        segment = self.segments.get(key)
        if segment is None:
            raise MockDocsError(
                f"Segment with ID {segment_id} was not found. If a segment ID "
                "is provided, it must be a header, footer or footnote ID. Use "
                "an empty segment ID to reference the body."
                if segment_id
                else f"Invalid tab ID {tab_id}."
            )
        return segment

    # -- bookkeeping -----------------------------------------------------
    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def _new_suggestion(self, author: str) -> Suggestion:
        """Deterministic ids: author prefix + per-author counter."""
        n = self._counters.get(author, 0) + 1
        self._counters[author] = n
        now = self._tick()
        sug = Suggestion(
            id=f"sug.{author}.{n}", author=author, created_at=now, touched_at=now
        )
        self.registry[sug.id] = sug
        return sug

    def clone(self) -> "MockDoc":
        other = MockDoc.__new__(MockDoc)
        other.document_id = self.document_id
        other.title = self.title
        other.tabs = [t.clone() for t in self.tabs]
        other.segments = {k: s.clone() for k, s in self.segments.items()}
        other.registry = {k: v.clone() for k, v in self.registry.items()}
        other._clock = self._clock
        other._counters = dict(self._counters)
        other.merge_log = list(self.merge_log)
        other.gc_log = list(self.gc_log)
        return other

    # -- projections (§3) ------------------------------------------------
    #
    # The three projections are whole-document: they concatenate every
    # segment in :meth:`ordered_segments` order. That keeps L1/L5/L7 stated
    # over the document the way §11 states them, and on a single-tab
    # body-only document it is exactly the pre-tabs behaviour. Per-segment
    # projections are :meth:`segment_text`.
    def original(self) -> list[Char]:
        """Reject everything."""
        return [c for _, _, c in self.iter_chars() if not c.ins]

    def final(self) -> list[Char]:
        """Accept everything."""
        return [c for _, _, c in self.iter_chars() if not c.dels]

    def display(self) -> list[Char]:
        return [c for _, _, c in self.iter_chars()]

    def segment_text(
        self, key: Optional[SegmentKey] = None, projection: str = "display"
    ) -> str:
        """One segment's text under one §3 projection."""
        chars = self.segment(key).chars
        if projection == "original":
            chars = [c for c in chars if not c.ins]
        elif projection == "final":
            chars = [c for c in chars if not c.dels]
        elif projection != "display":
            raise MockDocsError(f"unknown projection {projection!r}")
        return self.text_of(chars)

    @staticmethod
    def text_of(chars: Iterable[Char]) -> str:
        return "".join(c.cp for c in chars)

    def original_text(self) -> str:
        return self.text_of(self.original())

    def final_text(self) -> str:
        return self.text_of(self.final())

    def display_text(self) -> str:
        return self.text_of(self.display())

    # -- edit operations (§5) --------------------------------------------
    #
    # Every one takes an optional segment key. It defaults to the default
    # tab's body, which is both what the pre-tabs callers expect and what a
    # batchUpdate request that omits ``tabId``/``segmentId`` resolves to --
    # the silent-wrong-place footgun, reproduced rather than papered over.
    def insert(
        self,
        index: int,
        text: str,
        author: str,
        segment: Optional[SegmentKey] = None,
    ) -> Optional[str]:
        """§5.1 -- insert text at a collapsed cursor in one segment.

        Returns the surviving suggestion id (merge, §6, may absorb the fresh
        suggestion into an existing one), or ``None`` for an empty insert.
        """
        seg = self.segment(segment)
        chars = seg.chars
        if not 0 <= index <= len(chars):
            raise MockDocsError(
                f"insert index {index} out of range [0, {len(chars)}] "
                f"in {seg.describe()}"
            )
        clusters = split_graphemes(text)
        if not clusters:
            return None
        sug = self._new_suggestion(author)

        left = chars[index - 1] if index > 0 else None
        right = chars[index] if index < len(chars) else None

        # "If the cursor sits inside another author's pending insertion T, the
        # new chars get ins = {S, T}" -- *inside* means both neighbours carry
        # T, so inserting at the edge of a run does not inherit it.
        inherited_ins: set[str] = set()
        if left is not None and right is not None:
            inherited_ins = set(left.ins) & set(right.ins)

        # "Inherit del from the character to the left if that char is itself
        # struck" -- otherwise typing into the middle of a deleted region
        # produces text that survives a deletion the user thinks they made.
        inherited_dels: set[str] = set(left.dels) if left is not None else set()

        new_chars = [
            Char(
                cp=cp,
                ins={sug.id} | inherited_ins,
                dels=set(inherited_dels),
                colour=author,
            )
            for cp in clusters
        ]
        chars[index:index] = new_chars
        return self._settle(sug.id, seg)

    def delete(
        self,
        start: int,
        end: int,
        author: str,
        segment: Optional[SegmentKey] = None,
    ) -> Optional[str]:
        """§5.2 -- mark a selection deleted.

        Characters are NOT removed, and chars that already carry ``S`` in
        ``ins`` are deliberately not special-cased: they become the
        both-marks state (the ``"ul"`` case, L4).
        """
        seg = self.segment(segment)
        chars = seg.chars
        if not 0 <= start <= end <= len(chars):
            raise MockDocsError(
                f"delete range [{start}, {end}) out of range [0, {len(chars)}] "
                f"in {seg.describe()}"
            )
        if start == end:
            return None
        sug = self._new_suggestion(author)
        for c in chars[start:end]:
            c.dels.add(sug.id)
            c.colour = author
        return self._settle(sug.id, seg)

    def replace(
        self,
        start: int,
        end: int,
        text: str,
        author: str,
        segment: Optional[SegmentKey] = None,
    ) -> Optional[str]:
        """§5.3 -- not atomic: 5.2 then 5.1 at the selection start.

        The two halves share one suggestion id via §6, which is why they
        present as a single card.
        """
        deleted = self.delete(start, end, author, segment)
        inserted = self.insert(start, text, author, segment)
        return inserted or deleted

    def _settle(self, sid: str, segment: Segment) -> Optional[str]:
        """Every §5 operation ends by running §6 (merge) then §11.1 (GC), in
        that order."""
        survivor = self._merge_around(sid, segment)
        self._gc()
        return survivor if survivor in self.registry else None

    # -- merge (§6) ------------------------------------------------------
    def ranges(self) -> dict[str, tuple[int, int]]:
        """Half-open ``[first, last+1)`` range of every marked id, **in that
        id's own segment's index space**.

        A suggestion lives in exactly one segment (I5), so this stays a flat
        ``{id: (start, end)}`` map -- but two ids from different segments can
        now report the same numbers and mean different characters. Use
        :meth:`segment_of` whenever the answer leaves the model.
        """
        spans: dict[str, tuple[int, int]] = {}
        for _, i, c in self.iter_chars():
            for sid in c.marks:
                lo, hi = spans.get(sid, (i, i + 1))
                spans[sid] = (min(lo, i), max(hi, i + 1))
        return spans

    def _ranges_in(self, segment: Segment) -> dict[str, tuple[int, int]]:
        """:meth:`ranges` restricted to one segment -- what §6 merges over."""
        spans: dict[str, tuple[int, int]] = {}
        for i, c in enumerate(segment.chars):
            for sid in c.marks:
                lo, hi = spans.get(sid, (i, i + 1))
                spans[sid] = (min(lo, i), max(hi, i + 1))
        return spans

    def segment_of(self, sid: str) -> Optional[Segment]:
        """The segment carrying ``sid``'s marks, or ``None`` if it marks
        nothing. Half of a suggestion's address; :meth:`ranges` is the other."""
        for segment in self.ordered_segments():
            if any(sid in c.marks for c in segment.chars):
                return segment
        return None

    def _merge_around(self, sid: str, segment: Segment) -> str:
        """Absorb same-author neighbours into the survivor, to a fixpoint.

        Survivor selection is the candidate with the greatest ``touched_at``
        (§6), which reproduces the observed behaviour where a select-all
        deletion absorbs prior word deletions and presents as one card.

        **Merging never crosses a segment or a tab.** §6's ``gap`` is a
        distance in chars, and there is no such distance between two segments:
        they are separate index spaces. Two same-author suggestions at index 4
        of two different tabs are two different places, and merging them --
        which a document-wide range map would do, since both report ``(4, 5)``
        -- would fabricate an adjacency the document does not have and would
        take away one of the reviewer's two independent decisions (L8) on the
        strength of a coincidence.
        """
        while True:
            spans = self._ranges_in(segment)
            if sid not in spans:
                return sid
            author = self.registry[sid].author
            partner = None
            for other, span in spans.items():
                if other == sid or other not in self.registry:
                    continue
                if self.registry[other].author != author:
                    continue
                if _gap(spans[sid], span) <= MERGE_TOLERANCE:
                    partner = other
                    break
            if partner is None:
                return sid
            if self.registry[sid].touched_at >= self.registry[partner].touched_at:
                survivor, absorbed = sid, partner
            else:
                survivor, absorbed = partner, sid
            self._merge(survivor, absorbed)
            sid = survivor

    def _merge(self, survivor: str, absorbed: str) -> None:
        """§6's merge -- and a KNOWN, deliberate divergence from prod.

        Measured 2026-08-01 (docs/findings/merge.md,
        e2e/test_merge_semantics.py): the live API does not merge existing
        suggestions at all. It **absorbs a new edit at creation time** into an
        abutting/overlapping same-author suggestion -- no second id is ever
        minted, the PRE-EXISTING id survives, and the write reports it under
        ``updatedSummarySuggestionIds`` with no ``createdSuggestionIds``. Two
        suggestions that already exist stay two forever, even when a later
        edit pushes them into contact or spans both.

        Three consequences of that, all of which this method gets wrong on
        purpose:

        - survivor selection here is greatest ``touched_at`` (§6), i.e. the
          NEW id wins; prod keeps the older one;
        - :meth:`_merge_around` runs to a fixpoint, so an edit touching two
          cards collapses all three; prod joins exactly one and leaves the
          other untouched (and *which* one is nondeterministic);
        - the thread migration below (§10's *recommended* column, open
          question §13.3) answers a question prod never asks: nothing is
          absorbed, so no thread is ever orphaned.

        Left as-is deliberately. Making the model prod-faithful failed 51
        tests, mostly the checked-in llmux scenario ground truth, whose
        regeneration would invalidate the recorded benchmark numbers.
        docs/findings/merge.md § "What changes in the repo" records the
        measurement and the decision.

        The ``ix-merge-absorb`` interference case no longer rides on any of
        this. It used to be founded on two live cards merging -- the state
        prod cannot produce -- and was re-founded on absorption at creation
        time, which prod does do; it now grades an end state both rules reach
        and nothing about which id survives
        (docs/findings/merge-absorb-premise.md).
        """
        for _, _, c in self.iter_chars():
            if absorbed in c.ins:
                c.ins.discard(absorbed)
                c.ins.add(survivor)
            if absorbed in c.dels:
                c.dels.discard(absorbed)
                c.dels.add(survivor)
        surv = self.registry[survivor]
        gone = self.registry.pop(absorbed)
        # §10: migrate the absorbed thread onto the survivor, ordered by
        # createdAt -- the spec's *recommended* column. §13.3 is RESOLVED
        # vacuously: prod never absorbs a suggestion, so it never orphans a
        # thread (docs/findings/merge.md Q2). This line is therefore the
        # right behaviour for a model that does absorb.
        surv.thread = sorted(surv.thread + gone.thread, key=lambda p: p.created_at)
        surv.touched_at = self._tick()
        self.merge_log.append((survivor, absorbed))

    # -- garbage collection (§11.1 I2) -----------------------------------
    def _gc(self) -> None:
        live = {sid for _, _, c in self.iter_chars() for sid in c.marks}
        for sid in list(self.registry):
            if sid not in live:
                self.gc_log.append(sid)
                del self.registry[sid]

    # -- resolve (§7) ----------------------------------------------------
    #
    # Resolution is document-wide: suggestion ids are document-wide (verified
    # 2026-07-31), so ``acceptSuggestion`` names an id and nothing else -- no
    # tab, no segment. Sweeping every segment is therefore not defensive, it
    # is the semantics.
    def accept(self, sid: str) -> bool:
        """§7. Note the deliberate asymmetry versus :meth:`reject` in which
        set is filtered on and which is stripped. Both remove a char carrying
        ``S`` in *both* sets (L4)."""
        if sid not in self.registry:
            return False  # L2: destructive-idempotent
        for seg in self.segments.values():
            seg.chars = [c for c in seg.chars if sid not in c.dels]
            for c in seg.chars:
                c.ins.discard(sid)
        del self.registry[sid]
        # Not spelled out in §7, but I2 says a suggestion whose last marked
        # char is removed MUST leave the registry -- and accepting a deletion
        # can remove another suggestion's last marked char. See the module
        # note in tests/mockdocs/test_model_properties.py.
        self._gc()
        return True

    def reject(self, sid: str) -> bool:
        if sid not in self.registry:
            return False
        for seg in self.segments.values():
            seg.chars = [c for c in seg.chars if sid not in c.ins]
            for c in seg.chars:
                c.dels.discard(sid)
        del self.registry[sid]
        self._gc()
        return True

    def accept_all(self) -> None:
        """A fold; by L3 the order is irrelevant, so no ordering logic."""
        for sid in sorted(self.registry):
            self.accept(sid)

    def reject_all(self) -> None:
        for sid in sorted(self.registry):
            self.reject(sid)

    # -- cards and labels (§8) -------------------------------------------
    def struck(self, sid: str) -> list[Char]:
        """Everything with a strikethrough."""
        return [c for _, _, c in self.iter_chars() if sid in c.dels]

    def added(self, sid: str) -> list[Char]:
        """Underlined and not struck -- only what will actually survive."""
        return [
            c for _, _, c in self.iter_chars() if sid in c.ins and sid not in c.dels
        ]

    def label(self, sid: str) -> dict[str, Any]:
        """§8. A pure function of the rendering, not of the marks (L11/L12).

        Recomputed on read, never stored: a stored label goes stale the
        moment a merge rewrites the range.

        The grammar matches the live API's ``SuggestionThread.summaryText``
        verbatim, quotation marks included -- verified 2026-07-30 against an
        enrolled account, which produced ``Add: “Zero”``, ``Delete: “beta”``
        and ``Replace: “brave” with “bold”``. Prod is the oracle here: the
        spec's §8 straight ASCII quotes were a guess, and Google uses
        typographic quotes (U+201C/U+201D).
        """
        struck_text = self.text_of(self.struck(sid))
        added_text = self.text_of(self.added(sid))
        if not added_text:
            kind, text = (
                "Delete",
                f"Delete: {QUOTE_OPEN}{flat(struck_text)}{QUOTE_CLOSE}",
            )
        elif not struck_text:
            kind, text = "Add", f"Add: {QUOTE_OPEN}{flat(added_text)}{QUOTE_CLOSE}"
        else:
            kind = "Replace"
            text = (
                f"Replace: {QUOTE_OPEN}{flat(struck_text)}{QUOTE_CLOSE} with "
                f"{QUOTE_OPEN}{flat(added_text)}{QUOTE_CLOSE}"
            )
        return {
            "suggestion_id": sid,
            "kind": kind,
            "struck": struck_text,
            "added": added_text,
            "text": text,
        }

    def cards(self) -> list[dict[str, Any]]:
        """One card per live suggestion, anchored to the first char carrying
        its id. Cards map 1:1 to registry entries -- grouping is entirely a
        view concern (§8).

        ``anchor`` is an index **in the card's own segment**, so it travels
        with ``tab_id``/``segment_id``: the three together are the address,
        and the index alone is not one.
        """
        spans = self.ranges()
        homes = {sid: self.segment_of(sid) for sid in self.registry}
        order = {seg.key: i for i, seg in enumerate(self.ordered_segments())}
        out = []

        def sort_key(sid: str) -> tuple[int, int, str]:
            home = homes.get(sid)
            return (
                order.get(home.key, len(order)) if home else len(order),
                spans.get(sid, (0, 0))[0],
                sid,
            )

        for sid in sorted(self.registry, key=sort_key):
            sug = self.registry[sid]
            home = homes.get(sid)
            card = self.label(sid)
            card.update(
                author=sug.author,
                anchor=spans.get(sid, (None, None))[0],
                tab_id=home.tab_id if home else None,
                segment_id=home.segment_id if home else None,
                segment=home.kind if home else None,
                thread=[
                    {"post_id": p.post_id, "author": p.author, "content": p.content}
                    for p in sug.thread
                ],
            )
            out.append(card)
        return out

    # -- structural invariants (§11.1) -----------------------------------
    def check_invariants(self) -> None:
        """Assert I1-I5. Raises ``AssertionError`` naming the invariant.

        I5 is not in the spec: the spec has one flat array and so cannot
        state it. It says a suggestion's marks live in exactly one segment,
        which is what makes :meth:`ranges` a flat map and what §6's merge is
        forbidden from breaking.
        """
        # I1 -- no orphan marks.
        for seg, i, c in self.iter_chars():
            for sid in c.marks:
                assert sid in self.registry, (
                    f"I1 violated: char {i} of {seg.describe()} carries orphan "
                    f"mark {sid!r}"
                )
        # I2 -- no empty suggestions (post-GC).
        live = {sid for _, _, c in self.iter_chars() for sid in c.marks}
        for sid in self.registry:
            assert sid in live, f"I2 violated: {sid!r} in registry marks no char"
        # I3 -- colour determinism: run_colour is a pure function of the char.
        for _, _, c in self.iter_chars():
            assert run_colour(c) == run_colour(c.clone()), (
                "I3 violated: run_colour is not a pure function of the char"
            )
            if c.marks:
                assert c.colour is not None, (
                    "I3 violated: marked char has no colour author"
                )
        # I4 -- render totality.
        for _, _, c in self.iter_chars():
            assert c.render_state() in RENDER_STATES, "I4 violated"
        # I5 -- one home segment per suggestion.
        homes: dict[str, SegmentKey] = {}
        for seg, _, c in self.iter_chars():
            for sid in c.marks:
                previous = homes.setdefault(sid, seg.key)
                assert previous == seg.key, (
                    f"I5 violated: {sid!r} marks chars in both {previous} and "
                    f"{seg.key}; a suggestion belongs to one segment"
                )
        # A tab's body is the one segment it must have, and it is the only
        # segment addressable without a segmentId.
        for tab in self.tabs:
            assert (tab.tab_id, None) in self.segments, (
                f"tab {tab.tab_id} has no body segment"
            )
