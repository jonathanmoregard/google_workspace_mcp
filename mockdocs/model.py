"""In-memory Google-Docs-style suggesting mode -- SPEC §2-§8 and §11.

Pure implementation of ``docs/plans/2026-07-30-suggestion-mock-spec.md``:
the two-layer data model (§2), the three projections (§3), the rendering
state table (§4), the edit operations (§5.1-§5.3), same-author merge (§6),
accept/reject (§7), and cards/labels (§8). Structural invariants I1-I4 are
checkable via :meth:`MockDoc.check_invariants`.

Deliberately NOT implemented (see the spec's §14 adapter addendum):

- §5.4 backspace-burst destructive deletion -- an editor-interaction
  behaviour with no MCP tool surface, so nothing under test can reach it.
- §9 undo -- likewise editor-only. (Nothing here is undo-safe as a result:
  merge is destructive per L10, exactly as the spec warns.)

Everything counts in **grapheme clusters**. UTF-16 conversion happens at the
API boundary in :mod:`mockdocs.adapter`, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from mockdocs.graphemes import split_graphemes

#: SPEC §6 / open question §13.2. Zero means "abut or overlap only". The
#: value is a real product decision (L8), not a rendering tweak.
MERGE_TOLERANCE = 0

#: §8: single-line label truncation width.
LABEL_MAX_CHARS = 60

# Rendering states, §4. Total and mutually exclusive (I4).
RENDER_NORMAL = "normal"
RENDER_INSERT = "insert"  # underline, author colour
RENDER_DELETE = "delete"  # strikethrough, author colour
RENDER_BOTH = "both"  # underline AND strikethrough
RENDER_STATES = (RENDER_NORMAL, RENDER_INSERT, RENDER_DELETE, RENDER_BOTH)


class MockDocsError(Exception):
    """Invalid operation against the model (adapter maps these to HTTP 400)."""


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

    def clone(self) -> "Char":
        return Char(self.cp, set(self.ins), set(self.dels), self.colour)

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


class MockDoc:
    """A document: a flat ``Char`` array plus a suggestion registry (§2).

    ``'\\n'`` separates blocks, per §2 and §12's rationale for the flat
    representation.
    """

    def __init__(
        self,
        text: str = "",
        document_id: str = "mockdoc-1",
        title: str = "Mock Document",
    ) -> None:
        self.document_id = document_id
        self.title = title
        self.chars: list[Char] = [Char(cp) for cp in split_graphemes(text)]
        self.registry: dict[str, Suggestion] = {}
        self._clock = 0
        self._counters: dict[str, int] = {}
        #: Merge events, flagged because whether the real API merges adjacent
        #: same-author batch suggestions is UNCERTAIN (spec §14).
        self.merge_log: list[tuple[str, str]] = []
        #: GC events (§11.1 I2). §10 says warn in dev builds: a GC usually
        #: means a bug, and it silently drops a comment thread.
        self.gc_log: list[str] = []

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
        other.chars = [c.clone() for c in self.chars]
        other.registry = {k: v.clone() for k, v in self.registry.items()}
        other._clock = self._clock
        other._counters = dict(self._counters)
        other.merge_log = list(self.merge_log)
        other.gc_log = list(self.gc_log)
        return other

    # -- projections (§3) ------------------------------------------------
    def original(self) -> list[Char]:
        """Reject everything."""
        return [c for c in self.chars if not c.ins]

    def final(self) -> list[Char]:
        """Accept everything."""
        return [c for c in self.chars if not c.dels]

    def display(self) -> list[Char]:
        return list(self.chars)

    @staticmethod
    def text_of(chars: Iterable[Char]) -> str:
        return "".join(c.cp for c in chars)

    def original_text(self) -> str:
        return self.text_of(self.original())

    def final_text(self) -> str:
        return self.text_of(self.final())

    def display_text(self) -> str:
        return self.text_of(self.chars)

    # -- edit operations (§5) --------------------------------------------
    def insert(self, index: int, text: str, author: str) -> Optional[str]:
        """§5.1 -- insert text at a collapsed cursor.

        Returns the surviving suggestion id (merge, §6, may absorb the fresh
        suggestion into an existing one), or ``None`` for an empty insert.
        """
        if not 0 <= index <= len(self.chars):
            raise MockDocsError(
                f"insert index {index} out of range [0, {len(self.chars)}]"
            )
        clusters = split_graphemes(text)
        if not clusters:
            return None
        sug = self._new_suggestion(author)

        left = self.chars[index - 1] if index > 0 else None
        right = self.chars[index] if index < len(self.chars) else None

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
        self.chars[index:index] = new_chars
        return self._settle(sug.id)

    def delete(self, start: int, end: int, author: str) -> Optional[str]:
        """§5.2 -- mark a selection deleted.

        Characters are NOT removed, and chars that already carry ``S`` in
        ``ins`` are deliberately not special-cased: they become the
        both-marks state (the ``"ul"`` case, L4).
        """
        if not 0 <= start <= end <= len(self.chars):
            raise MockDocsError(
                f"delete range [{start}, {end}) out of range [0, {len(self.chars)}]"
            )
        if start == end:
            return None
        sug = self._new_suggestion(author)
        for c in self.chars[start:end]:
            c.dels.add(sug.id)
            c.colour = author
        return self._settle(sug.id)

    def replace(self, start: int, end: int, text: str, author: str) -> Optional[str]:
        """§5.3 -- not atomic: 5.2 then 5.1 at the selection start.

        The two halves share one suggestion id via §6, which is why they
        present as a single card.
        """
        deleted = self.delete(start, end, author)
        inserted = self.insert(start, text, author)
        return inserted or deleted

    def _settle(self, sid: str) -> Optional[str]:
        """Every §5 operation ends by running §6 (merge) then §11.1 (GC), in
        that order."""
        survivor = self._merge_around(sid)
        self._gc()
        return survivor if survivor in self.registry else None

    # -- merge (§6) ------------------------------------------------------
    def ranges(self) -> dict[str, tuple[int, int]]:
        """Half-open ``[first, last+1)`` char range of every marked id."""
        spans: dict[str, tuple[int, int]] = {}
        for i, c in enumerate(self.chars):
            for sid in c.marks:
                lo, hi = spans.get(sid, (i, i + 1))
                spans[sid] = (min(lo, i), max(hi, i + 1))
        return spans

    def _merge_around(self, sid: str) -> str:
        """Absorb same-author neighbours into the survivor, to a fixpoint.

        Survivor selection is the candidate with the greatest ``touched_at``
        (§6), which reproduces the observed behaviour where a select-all
        deletion absorbs prior word deletions and presents as one card.
        """
        while True:
            spans = self.ranges()
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
        for c in self.chars:
            if absorbed in c.ins:
                c.ins.discard(absorbed)
                c.ins.add(survivor)
            if absorbed in c.dels:
                c.dels.discard(absorbed)
                c.dels.add(survivor)
        surv = self.registry[survivor]
        gone = self.registry.pop(absorbed)
        # §10: migrate the absorbed thread onto the survivor, ordered by
        # createdAt. Docs appears to drop it; the spec argues migration is
        # correct and it costs one array concat. Open question §13.3.
        surv.thread = sorted(surv.thread + gone.thread, key=lambda p: p.created_at)
        surv.touched_at = self._tick()
        self.merge_log.append((survivor, absorbed))

    # -- garbage collection (§11.1 I2) -----------------------------------
    def _gc(self) -> None:
        live = {sid for c in self.chars for sid in c.marks}
        for sid in list(self.registry):
            if sid not in live:
                self.gc_log.append(sid)
                del self.registry[sid]

    # -- resolve (§7) ----------------------------------------------------
    def accept(self, sid: str) -> bool:
        """§7. Note the deliberate asymmetry versus :meth:`reject` in which
        set is filtered on and which is stripped. Both remove a char carrying
        ``S`` in *both* sets (L4)."""
        if sid not in self.registry:
            return False  # L2: destructive-idempotent
        self.chars = [c for c in self.chars if sid not in c.dels]
        for c in self.chars:
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
        self.chars = [c for c in self.chars if sid not in c.ins]
        for c in self.chars:
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
        return [c for c in self.chars if sid in c.dels]

    def added(self, sid: str) -> list[Char]:
        """Underlined and not struck -- only what will actually survive."""
        return [c for c in self.chars if sid in c.ins and sid not in c.dels]

    def label(self, sid: str) -> dict[str, Any]:
        """§8. A pure function of the rendering, not of the marks (L11/L12).

        Recomputed on read, never stored: a stored label goes stale the
        moment a merge rewrites the range.
        """
        struck_text = self.text_of(self.struck(sid))
        added_text = self.text_of(self.added(sid))
        if not added_text:
            kind, text = "Delete", f'Delete: "{flat(struck_text)}"'
        elif not struck_text:
            kind, text = "Add", f'Add: "{flat(added_text)}"'
        else:
            kind = "Replace"
            text = f'Replace: "{flat(struck_text)}" with "{flat(added_text)}"'
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
        view concern (§8)."""
        spans = self.ranges()
        out = []
        for sid in sorted(self.registry, key=lambda s: (spans.get(s, (0, 0))[0], s)):
            sug = self.registry[sid]
            card = self.label(sid)
            card.update(
                author=sug.author,
                anchor=spans.get(sid, (None, None))[0],
                thread=[
                    {"post_id": p.post_id, "author": p.author, "content": p.content}
                    for p in sug.thread
                ],
            )
            out.append(card)
        return out

    # -- structural invariants (§11.1) -----------------------------------
    def check_invariants(self) -> None:
        """Assert I1-I4. Raises ``AssertionError`` naming the invariant."""
        # I1 -- no orphan marks.
        for i, c in enumerate(self.chars):
            for sid in c.marks:
                assert sid in self.registry, (
                    f"I1 violated: char {i} carries orphan mark {sid!r}"
                )
        # I2 -- no empty suggestions (post-GC).
        live = {sid for c in self.chars for sid in c.marks}
        for sid in self.registry:
            assert sid in live, f"I2 violated: {sid!r} in registry marks no char"
        # I3 -- colour determinism: run_colour is a pure function of the char.
        for c in self.chars:
            assert run_colour(c) == run_colour(c.clone()), (
                "I3 violated: run_colour is not a pure function of the char"
            )
            if c.marks:
                assert c.colour is not None, (
                    "I3 violated: marked char has no colour author"
                )
        # I4 -- render totality.
        for c in self.chars:
            assert c.render_state() in RENDER_STATES, "I4 violated"
