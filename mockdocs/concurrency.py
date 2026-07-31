"""A scripted second editor working in the document while the agent reviews it.

SPEC §12 defers concurrency ("everything above is single-writer"), and a
single-writer mock structurally cannot produce the failures that dominate a
real review: an agent's suggestion id vanishing because a colleague accepted
it, indexes shifting under a cached read, a thread resolved before the reply
lands, two authors' marks overlapping, a same-author edit absorbing the
agent's own pending suggestion through §6 merge.

This module adds exactly that -- a *second writer* -- without adding
concurrency. There is no thread, no timer and no sleep anywhere in here. The
other editor's operations fire at points defined **in the agent's own call
sequence**:

    "after the agent's 2nd list_document_suggestions, Bob accepts sug.bob.1"

Determinism is therefore structural rather than best-effort:

1. **The clock is the agent's call sequence.** A trigger names a tool, an
   occurrence ordinal, and optional argument substrings. Nothing consults
   wall-clock time, a random source, or the number of API calls a tool
   happens to make internally.
2. **Tool calls are serialised.** :class:`InterferenceMiddleware` holds one
   lock for the whole of ``on_call_tool``, so a client that issues parallel
   ``tool_use`` blocks still produces one totally ordered call sequence
   server-side. Without this the ordinals would depend on scheduling.
3. **Each interference fires at most once**, guarded by name, and counts only
   the calls that match its own trigger. Two interferences on the same call
   are independent counters.
4. **The other editor's edits go through the SPEC §5 model operations**, the
   same ones the seed replay and the adapter use. It cannot reach a state the
   editor could not have produced.
5. **Every located interference names a coordinate space.** ``tab_id`` and
   ``segment_id`` in an interference's ``params`` pick the segment an anchor
   resolves in and the edit lands in; omitting both means the default tab's
   body, which is what every pre-tabs script meant and still means. Anchors
   are searched inside one segment rather than across the document, because
   the index they produce is only valid there -- a script that searched
   document-wide would hand back an index from the wrong space and the edit
   would land somewhere numerically valid and semantically wrong, which is
   the production bug class this harness is supposed to expose, not commit.

After every interference the model is re-checked (I1-I5, plus L1/L5 and the
per-op extreme-preservation half of L7). Violations are recorded on the
backend rather than raised: a bug in *this* module must be distinguishable
from an agent mistake at grading time, which means the grader has to be able
to see it. :class:`ConcurrencyRecord` -- the agent's call log, what fired and
where, and any invariant violation -- rides out in the ``mockdocs.state``
snapshot, so an out-of-process grader gets the whole interleaving.

The same op vocabulary is used by :func:`replay`, so a grader computes the
"correct" end state by replaying the other editor's operations *and* the
right agent actions through this algebra instead of hand-writing an expected
string that would silently rot the moment an interference changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from mockdocs.graphemes import split_graphemes, utf16_len
from mockdocs.model import Char, MockDoc, MockDocsError, Segment

#: Bumped when the on-disk interference script shape changes incompatibly.
SCHEMA_VERSION = 1

#: The interference catalogue. Each is a named, reusable operation of the
#: "other editor"; ``reply_thread`` is the one that is not an interference at
#: all -- it exists so a grader can replay the *agent's* correct actions
#: through the same algebra (see :func:`replay`).
KINDS = (
    "resolve_under_agent",
    "shift_indexes",
    "resolve_thread",
    "overlapping_suggestion",
    "merge_absorb",
    "reply_thread",
)

#: Trigger phases. ``before`` fires ahead of the tool body (the agent is about
#: to act on state that is no longer what it read); ``after`` fires once the
#: tool has answered (the agent now holds a stale answer).
PHASES = ("before", "after")

#: Tool arguments longer than this are clipped in the call log. The log is
#: written into every state snapshot, and a snapshot is rewritten on each API
#: call, so it has to stay small.
ARG_CLIP = 200


class InterferenceError(Exception):
    """A malformed interference script, or one that cannot be applied."""


# ---------------------------------------------------------------------------
# Script model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """When an interference fires, in agent-call space.

    ``nth`` counts only the calls this trigger matches, so
    ``{"tool": "list_document_suggestions", "nth": 2}`` means the agent's
    second listing regardless of what it did in between.

    ``tool`` accepts a list. That is not a convenience: two agents can reach
    the same understanding of a document through different read tools, and a
    trigger pinned to one of them would silently never fire on the other --
    which looks like a well-behaved agent when it is really a dead scenario.
    """

    when: str = "after"
    tools: tuple[str, ...] = ()
    nth: int = 1
    args_contain: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Trigger":
        data = data or {}
        if not isinstance(data, dict):
            raise InterferenceError(
                f"trigger must be an object, got {type(data).__name__}"
            )
        when = str(data.get("when", "after")).lower()
        if when not in PHASES:
            raise InterferenceError(
                f"trigger.when must be one of {PHASES}, got {when!r}"
            )
        try:
            nth = int(data.get("nth", 1))
        except (TypeError, ValueError) as exc:
            raise InterferenceError(f"trigger.nth is not an integer: {exc}") from exc
        if nth < 1:
            raise InterferenceError(f"trigger.nth must be >= 1, got {nth}")
        args_contain = data.get("args_contain") or {}
        if not isinstance(args_contain, dict):
            raise InterferenceError("trigger.args_contain must be an object")
        raw_tool = data.get("tool")
        if raw_tool is None:
            tools: tuple[str, ...] = ()
        elif isinstance(raw_tool, (list, tuple)):
            tools = tuple(str(t) for t in raw_tool if t)
        else:
            tools = (str(raw_tool),)
        return cls(
            when=when,
            tools=tools,
            nth=nth,
            args_contain={str(k): str(v) for k, v in args_contain.items()},
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"when": self.when, "nth": self.nth}
        if self.tools:
            out["tool"] = list(self.tools)
        if self.args_contain:
            out["args_contain"] = dict(self.args_contain)
        return out

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        if self.tools and tool not in self.tools:
            return False
        for key, needle in self.args_contain.items():
            if needle not in str(args.get(key, "")):
                return False
        return True


@dataclass(frozen=True)
class Interference:
    """One named operation of the other editor, plus when it fires."""

    name: str
    kind: str
    trigger: Trigger
    editor: str = "other"
    document_id: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Interference":
        if not isinstance(data, dict):
            raise InterferenceError(
                f"an interference must be an object, got {type(data).__name__}"
            )
        kind = str(data.get("kind") or "")
        if kind not in KINDS:
            raise InterferenceError(
                f"unknown interference kind {kind!r}; known kinds: {', '.join(KINDS)}"
            )
        name = str(data.get("name") or kind)
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise InterferenceError(f"{name}: params must be an object")
        return cls(
            name=name,
            kind=kind,
            trigger=Trigger.from_dict(data.get("trigger")),
            editor=str(data.get("editor") or "other"),
            document_id=data.get("document_id"),
            params=dict(params),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "trigger": self.trigger.as_dict(),
            "editor": self.editor,
            "params": dict(self.params),
        }
        if self.document_id:
            out["document_id"] = self.document_id
        return out


def parse_script(data: Any) -> list[Interference]:
    """Validate an interference script; a bad script must fail loudly here.

    Accepts either ``{"version": 1, "interferences": [...]}`` or a bare list.
    """
    if isinstance(data, list):
        entries: Any = data
    elif isinstance(data, dict):
        version = data.get("version", SCHEMA_VERSION)
        if int(version) != SCHEMA_VERSION:
            raise InterferenceError(
                f"unsupported interference script version {version!r} "
                f"(this build reads {SCHEMA_VERSION})"
            )
        entries = data.get("interferences") or []
    else:
        raise InterferenceError(
            f"interference script must be a list or object, got {type(data).__name__}"
        )
    if not isinstance(entries, list):
        raise InterferenceError("interferences must be a list")
    parsed = [Interference.from_dict(entry) for entry in entries]
    names = [i.name for i in parsed]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise InterferenceError(
            f"interference names must be unique; duplicated: {', '.join(duplicates)}"
        )
    return parsed


def load_script(path: str | Path) -> list[Interference]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InterferenceError(
            f"cannot read interference script {source}: {exc}"
        ) from exc
    except ValueError as exc:
        raise InterferenceError(f"{source} is not valid JSON: {exc}") from exc
    return parse_script(data)


# ---------------------------------------------------------------------------
# Run record (rides out in the state snapshot)
# ---------------------------------------------------------------------------


@dataclass
class AgentCall:
    """One MCP tool call the agent made, as the server saw it."""

    ordinal: int
    tool: str
    tool_ordinal: int
    args: dict[str, Any] = field(default_factory=dict)
    ok: Optional[bool] = None
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "tool": self.tool,
            "tool_ordinal": self.tool_ordinal,
            "args": self.args,
            "ok": self.ok,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentCall":
        return cls(
            ordinal=int(data.get("ordinal") or 0),
            tool=str(data.get("tool") or ""),
            tool_ordinal=int(data.get("tool_ordinal") or 0),
            args=dict(data.get("args") or {}),
            ok=data.get("ok"),
            error=data.get("error"),
        )


@dataclass
class Fired:
    """One interference that actually fired, and what it did."""

    name: str
    kind: str
    when: str
    editor: str
    at_call: int
    at_tool: str
    document_id: str
    effect: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "when": self.when,
            "editor": self.editor,
            "at_call": self.at_call,
            "at_tool": self.at_tool,
            "document_id": self.document_id,
            "effect": self.effect,
            "violations": list(self.violations),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fired":
        return cls(
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or ""),
            when=str(data.get("when") or ""),
            editor=str(data.get("editor") or ""),
            at_call=int(data.get("at_call") or 0),
            at_tool=str(data.get("at_tool") or ""),
            document_id=str(data.get("document_id") or ""),
            effect=dict(data.get("effect") or {}),
            violations=list(data.get("violations") or []),
        )


@dataclass
class ConcurrencyRecord:
    """Everything an out-of-process grader needs to judge an interleaved run.

    Attached to the backend as ``backend.concurrency`` and serialised by
    :mod:`mockdocs.state`, because the grader runs in the harness process and
    the interleaving happened in the server's.
    """

    declared: list[str] = field(default_factory=list)
    fired: list[Fired] = field(default_factory=list)
    agent_calls: list[AgentCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def fired_names(self) -> list[str]:
        return [f.name for f in self.fired]

    @property
    def unfired(self) -> list[str]:
        fired = set(self.fired_names)
        return [name for name in self.declared if name not in fired]

    @property
    def violations(self) -> list[str]:
        """Every invariant violation, plus any engine error. Non-empty means
        the *harness* is at fault, not the agent."""
        out = list(self.errors)
        for entry in self.fired:
            out.extend(f"{entry.name}: {v}" for v in entry.violations)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": list(self.declared),
            "fired": [f.as_dict() for f in self.fired],
            "agent_calls": [c.as_dict() for c in self.agent_calls],
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ConcurrencyRecord":
        data = data or {}
        return cls(
            declared=[str(x) for x in (data.get("declared") or [])],
            fired=[Fired.from_dict(f) for f in (data.get("fired") or [])],
            agent_calls=[
                AgentCall.from_dict(c) for c in (data.get("agent_calls") or [])
            ],
            errors=[str(e) for e in (data.get("errors") or [])],
        )


# ---------------------------------------------------------------------------
# Locating an edit in the document
# ---------------------------------------------------------------------------


def segment_for(doc: MockDoc, params: dict[str, Any]) -> Segment:
    """The segment an interference's ``params`` name.

    ``{"tab_id": …, "segment_id": …}``, either or both omitted. Omitting both
    is the default tab's body, so every pre-tabs script keeps meaning what it
    meant. Unlike the API's own resolution -- which is silent by design and is
    reproduced as such in :meth:`mockdocs.adapter.BatchUpdateApplier._segment`
    -- a script that names a segment which does not exist is a *script* bug,
    so it raises here and the engine records it as a harness fault rather than
    quietly editing the body instead.
    """
    try:
        return doc.resolve_segment(
            tab_id=params.get("tab_id"), segment_id=params.get("segment_id")
        )
    except MockDocsError as exc:
        raise InterferenceError(str(exc)) from None


def find_clusters(
    doc: MockDoc,
    needle: str,
    occurrence: int = 1,
    segment: Optional[Segment] = None,
) -> int:
    """Grapheme index of ``needle`` in one segment's SUGGESTIONS_INLINE text.

    Cluster-wise rather than code-point-wise, so an anchor never lands inside
    an emoji or a combining sequence -- the same discipline
    ``adapter._do_replaceAllText`` uses. The search is scoped to one segment
    because the index it returns only means anything there.
    """
    chars = (segment or doc.segment()).chars
    haystack = [c.cp for c in chars]
    clusters = split_graphemes(needle)
    if not clusters:
        raise InterferenceError("anchor text must be non-empty")
    hits = [
        n
        for n in range(len(haystack) - len(clusters) + 1)
        if haystack[n : n + len(clusters)] == clusters
    ]
    if len(hits) < occurrence:
        raise InterferenceError(
            f"anchor text {needle!r} occurs {len(hits)} time(s) in "
            f"{(segment or doc.segment()).describe()}; "
            f"occurrence {occurrence} was requested"
        )
    return hits[occurrence - 1]


def _anchor_index(
    doc: MockDoc, params: dict[str, Any], segment: Optional[Segment] = None
) -> int:
    """Resolve ``at`` / ``before_text`` / ``after_text`` / ``at_end`` to an
    index **in ``segment``** (the default tab's body when unnamed)."""
    segment = segment or segment_for(doc, params)
    size = len(segment.chars)
    occurrence = int(params.get("occurrence", 1))
    if "at" in params:
        index = int(params["at"])
    elif "before_text" in params:
        index = find_clusters(doc, str(params["before_text"]), occurrence, segment)
    elif "after_text" in params:
        needle = str(params["after_text"])
        index = find_clusters(doc, needle, occurrence, segment) + len(
            split_graphemes(needle)
        )
    elif params.get("at_end"):
        index = size
    else:
        raise InterferenceError(
            "need one of at / before_text / after_text / at_end to locate the edit"
        )
    if not 0 <= index <= size:
        raise InterferenceError(
            f"resolved index {index} is outside [0, {size}] in {segment.describe()}"
        )
    return index


def _span(
    doc: MockDoc, params: dict[str, Any], segment: Optional[Segment] = None
) -> tuple[int, int]:
    """Resolve a half-open ``[start, end)`` span from explicit indexes or an
    anchor plus its own length, in ``segment``."""
    segment = segment or segment_for(doc, params)
    size = len(segment.chars)
    if "anchor_text" in params:
        needle = str(params["anchor_text"])
        start = find_clusters(doc, needle, int(params.get("occurrence", 1)), segment)
        return start, start + len(split_graphemes(needle))
    if "start" in params and "end" in params:
        start, end = int(params["start"]), int(params["end"])
    elif "start" in params and "span" in params:
        start = int(params["start"])
        end = start + int(params["span"])
    else:
        raise InterferenceError("need anchor_text, or start plus end/span")
    if not 0 <= start <= end <= size:
        raise InterferenceError(
            f"span [{start}, {end}) is outside [0, {size}] in {segment.describe()}"
        )
    return start, end


# ---------------------------------------------------------------------------
# Invariant checking under interference
# ---------------------------------------------------------------------------


def check_model(
    doc: MockDoc,
    *,
    original_before: Optional[str] = None,
    final_before: Optional[str] = None,
) -> list[str]:
    """SPEC §11 checks, run after every interference.

    Returns violation strings rather than raising: a violation is a bug in
    *this* module or in the model, and the grader has to be able to tell that
    apart from a mistake by the agent under test.

    - **I1-I4** via :meth:`MockDoc.check_invariants`.
    - **L1 (extremes)** -- accepting everything yields ``final``, rejecting
      everything yields ``original``, each leaving an empty registry and a
      mark-free document.
    - **L5 (survival)** -- under a mixed accept/reject assignment a char
      survives iff every ``ins`` was accepted and every ``del`` rejected.
    - **L7 (merge preserves both extremes)**, checked in situ: the caller
      passes whichever extreme its operation must not have moved, so a merge
      triggered inside the operation cannot have changed content. Suggestion
      operations never move ``original``; ``accept`` never moves ``final``;
      ``reject`` never moves ``original``.
    """
    violations: list[str] = []
    try:
        doc.check_invariants()
    except AssertionError as exc:
        violations.append(str(exc))

    # L1 -- the two folds reproduce the two projections exactly.
    accepted = doc.clone()
    accepted.accept_all()
    if accepted.display_text() != doc.final_text():
        violations.append(
            f"L1 violated: accept-all gave {accepted.display_text()!r}, "
            f"final() is {doc.final_text()!r}"
        )
    if accepted.registry:
        violations.append("L1 violated: accept-all left suggestions in the registry")
    rejected = doc.clone()
    rejected.reject_all()
    if rejected.display_text() != doc.original_text():
        violations.append(
            f"L1 violated: reject-all gave {rejected.display_text()!r}, "
            f"original() is {doc.original_text()!r}"
        )
    if rejected.registry:
        violations.append("L1 violated: reject-all left suggestions in the registry")

    # L5 -- mixed assignment. Deterministic split (every other id in document
    # order) so a failure reproduces exactly. Over every segment of every tab,
    # because ``display_text`` is: scoping the prediction to the body while
    # comparing against the whole document would fail on any multi-tab doc.
    ids = sorted(doc.registry)
    accept_set = set(ids[::2])
    expected = "".join(
        c.cp for c in doc.display() if c.ins <= accept_set and not (c.dels & accept_set)
    )
    mixed = doc.clone()
    for sid in ids:
        if sid in accept_set:
            mixed.accept(sid)
        else:
            mixed.reject(sid)
    if mixed.display_text() != expected:
        violations.append(
            f"L5 violated: mixed assignment gave {mixed.display_text()!r}, "
            f"the survival rule predicts {expected!r}"
        )

    # L7 -- whichever extreme this operation was not allowed to move.
    if original_before is not None and doc.original_text() != original_before:
        violations.append(
            f"L7 violated: original() moved from {original_before!r} to "
            f"{doc.original_text()!r}"
        )
    if final_before is not None and doc.final_text() != final_before:
        violations.append(
            f"L7 violated: final() moved from {final_before!r} to {doc.final_text()!r}"
        )
    return violations


# ---------------------------------------------------------------------------
# The interference catalogue
#
# Every handler returns ``(effect, (original_preserved, final_preserved))``.
# The second element is the op's own L7/§7 obligation, handed straight to
# :func:`check_model`: a suggestion never moves ``original``, ``accept``
# never moves ``final``, ``reject`` never moves ``original``. A base-text
# edit (``shift_indexes`` with ``as_suggestion`` false) is the one operation
# that legitimately moves both -- the other editor is typing in accepted
# text, not suggesting.
# ---------------------------------------------------------------------------


def _document(backend: Any, interference: Interference) -> MockDoc:
    doc_id = interference.params.get("document_id") or interference.document_id
    if doc_id:
        doc = backend.documents.get(str(doc_id))
        if doc is None:
            raise InterferenceError(f"no document {doc_id!r} in the backend")
        return doc
    if len(backend.documents) != 1:
        raise InterferenceError(
            "document_id is required when the backend holds "
            f"{len(backend.documents)} documents"
        )
    return next(iter(backend.documents.values()))


def _do_resolve_under_agent(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """The other editor accepts or rejects a suggestion the agent has listed.

    The agent's remembered id is gone by the time it acts -- the vanished-id
    case. ``also_removed`` names the suggestions the resolution took with it
    (SPEC §11.1 I2 garbage collection), because those are ids the agent may
    also be holding.
    """
    params = interference.params
    action = str(params.get("action") or "accept").lower()
    if action not in ("accept", "reject"):
        raise InterferenceError(
            f"resolve_under_agent action must be accept/reject, got {action!r}"
        )
    sid = params.get("suggestion_id")
    if not sid:
        raise InterferenceError("resolve_under_agent needs a suggestion_id")
    sid = str(sid)
    before = set(doc.registry)
    existed = sid in doc.registry
    if action == "accept":
        doc.accept(sid)
        preserves = (False, True)  # accept commits: final() cannot move
    else:
        doc.reject(sid)
        preserves = (True, False)  # reject discards: original() cannot move
    effect = {
        "suggestion_id": sid,
        "action": action,
        "existed": existed,
        "also_removed": sorted(before - set(doc.registry) - {sid}),
        "pending_after": sorted(doc.registry),
    }
    return effect, preserves


def _do_shift_indexes(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """The other editor inserts or deletes text BEFORE the agent's cached range.

    The dangerous case, and the reason this one is worth more than the rest:
    the agent's next write still SUCCEEDS. Nothing 400s. It simply lands
    ``utf16_shift`` code units away from what the agent read, which is silent
    corruption rather than a visible error.

    Default is a base-text edit (the other editor is not in suggesting mode),
    which is what makes the shift invisible to a suggestion listing. Set
    ``as_suggestion`` to have them suggest instead -- indexes still shift,
    because SUGGESTIONS_INLINE counts marked text.

    The shift is confined to ONE segment: each is numbered from its own start,
    so typing in the header moves the header's indexes and nothing else. The
    effect therefore reports ``tab_id``/``segment_id`` next to
    ``utf16_shift``, because a grader that applied the shift to the wrong
    coordinate space would be making the very mistake the scenario is about.
    """
    params = interference.params
    mode = str(params.get("mode") or "insert").lower()
    as_suggestion = bool(params.get("as_suggestion"))
    segment = segment_for(doc, params)
    where = {"tab_id": segment.tab_id, "segment_id": segment.segment_id}
    if mode == "insert":
        text = str(params.get("text") or "")
        if not text:
            raise InterferenceError("shift_indexes insert needs text")
        index = _anchor_index(doc, params, segment)
        if as_suggestion:
            sid = doc.insert(index, text, interference.editor, segment.key)
            preserves = (True, False)
        else:
            segment.chars[index:index] = [Char(cp) for cp in split_graphemes(text)]
            sid = None
            preserves = (False, False)
        effect = {
            "mode": "insert",
            "index": index,
            "text": text,
            "utf16_shift": utf16_len(text),
            "as_suggestion": as_suggestion,
            "suggestion_id": sid,
            **where,
        }
    elif mode == "delete":
        start, end = _span(doc, params, segment)
        removed = "".join(c.cp for c in segment.chars[start:end])
        if as_suggestion:
            sid = doc.delete(start, end, interference.editor, segment.key)
            preserves = (True, False)
        else:
            del segment.chars[start:end]
            doc._gc()
            sid = None
            preserves = (False, False)
        effect = {
            "mode": "delete",
            "index": start,
            "end": end,
            "text": removed,
            "utf16_shift": -utf16_len(removed) if not as_suggestion else 0,
            "as_suggestion": as_suggestion,
            "suggestion_id": sid,
            **where,
        }
    else:
        raise InterferenceError(
            f"shift_indexes mode must be insert/delete, got {mode!r}"
        )
    return effect, preserves


def _do_resolve_thread(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """The other editor resolves, reopens or deletes a comment thread before
    the agent's reply lands."""
    params = interference.params
    action = str(params.get("action") or "resolve").lower()
    comment_id = params.get("comment_id")
    if not comment_id:
        raise InterferenceError("resolve_thread needs a comment_id")
    comment_id = str(comment_id)
    threads = backend.comments.get(doc.document_id) or []
    existed = any(t["commentId"] == comment_id for t in threads)
    if action == "delete":
        if existed:
            backend.delete_comment_thread(doc.document_id, comment_id)
    elif action in ("resolve", "reopen"):
        if existed:
            backend.add_comment_reply(
                doc.document_id,
                comment_id,
                params.get("content") or "",
                "RESOLVE" if action == "resolve" else "REOPEN",
                author=interference.editor,
            )
    else:
        raise InterferenceError(
            f"resolve_thread action must be resolve/reopen/delete, got {action!r}"
        )
    effect = {
        "comment_id": comment_id,
        "action": action,
        "existed": existed,
        "threads_after": [
            t["commentId"] for t in backend.comments.get(doc.document_id) or []
        ],
    }
    return effect, (True, True)


def _do_overlapping_suggestion(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """The other editor suggests an edit overlapping the agent's target span.

    Produces the both-marks state of SPEC §4 and the conjunctive/disjunctive
    interaction of §2: neither party can now resolve their own suggestion
    without moving the other's rendering.

    Overlap is only possible *within* a segment, so the other editor's
    suggestion lands in the segment ``params`` names (the default tab's body
    when they name none). Aiming it at the wrong one produces two independent
    cards instead of an overlap -- which the ``pending_after`` count makes
    visible rather than leaving the scenario silently toothless.
    """
    params = interference.params
    text = params.get("text")
    segment = segment_for(doc, params)
    where = {"tab_id": segment.tab_id, "segment_id": segment.segment_id}
    if (
        "at" in params
        or "before_text" in params
        or "after_text" in params
        or params.get("at_end")
    ):
        index = _anchor_index(doc, params, segment)
        if not text:
            raise InterferenceError("overlapping_suggestion insertion needs text")
        sid = doc.insert(index, str(text), interference.editor, segment.key)
        effect = {
            "mode": "insertion",
            "start": index,
            "text": text,
            "suggestion_id": sid,
            **where,
        }
    else:
        start, end = _span(doc, params, segment)
        if text:
            sid = doc.replace(start, end, str(text), interference.editor, segment.key)
            mode = "replacement"
        else:
            sid = doc.delete(start, end, interference.editor, segment.key)
            mode = "deletion"
        effect = {
            "mode": mode,
            "start": start,
            "end": end,
            "text": text,
            "suggestion_id": sid,
            **where,
        }
    effect["pending_after"] = sorted(doc.registry)
    return effect, (True, False)


def _do_merge_absorb(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """A same-author-as-the-agent edit adjacent to the agent's own pending
    suggestion, triggering SPEC §6 merge.

    Confirmed real in production: a merged write returns no
    ``createdSuggestionIds``, and the id the agent is holding stops existing
    because §6 absorbed it into the survivor. Survivor selection is greatest
    ``touched_at``, so an edit made *after* the agent's write wins and the
    agent's remembered id is the one that disappears.

    The editor here is deliberately the authenticated user (a second session
    of the same account -- the phone, the other tab), because §6 refuses to
    merge across authors.

    ``after_suggestion`` resolves the target's OWN segment and edits there:
    §6 never merges across segments or tabs, so an edit placed in the body
    beside a header suggestion's index would produce a second card instead of
    absorbing the first -- a dead scenario that still looks like it fired.
    """
    params = interference.params
    author = str(params.get("author") or backend.me)
    text = str(params.get("text") or "")
    if not text:
        raise InterferenceError("merge_absorb needs text")
    if "after_suggestion" in params:
        sid = str(params["after_suggestion"])
        if sid == "$latest":
            # "Whatever the agent just created", resolved from state rather
            # than hardcoded: the agent's own suggestion id depends on how it
            # chose to phrase the edit, and a hardcoded id would turn a
            # legitimate alternative phrasing into a dead scenario.
            candidates = [s for s in doc.registry.values() if s.author == author]
            if not candidates:
                raise InterferenceError(
                    f"merge_absorb: no pending suggestion by {author!r} to sit "
                    "next to (the agent's own write never landed)"
                )
            sid = max(candidates, key=lambda s: (s.touched_at, s.id)).id
        spans = doc.ranges()
        home = doc.segment_of(sid)
        if sid not in spans or home is None:
            raise InterferenceError(
                f"merge_absorb: suggestion {sid!r} has no marks to sit next to"
            )
        segment = home
        index = spans[sid][1]
        anchor = sid
    else:
        segment = segment_for(doc, params)
        index = _anchor_index(doc, params, segment)
        anchor = None
    watermark = len(doc.merge_log)
    before = set(doc.registry)
    survivor = doc.insert(index, text, author, segment.key)
    merges = doc.merge_log[watermark:]
    effect = {
        "author": author,
        "anchor_suggestion_id": anchor,
        "index": index,
        "text": text,
        "utf16_shift": utf16_len(text),
        "survivor_id": survivor,
        "merged": bool(merges),
        "absorbed_ids": sorted({absorbed for _, absorbed in merges}),
        "vanished_ids": sorted(before - set(doc.registry)),
        "pending_after": sorted(doc.registry),
        "tab_id": segment.tab_id,
        "segment_id": segment.segment_id,
    }
    return effect, (True, False)


def _do_reply_thread(
    backend: Any, doc: MockDoc, interference: Interference
) -> tuple[dict[str, Any], tuple[bool, bool]]:
    """Post a reply to a comment or suggestion thread.

    Not an interference: this is here so a grader can replay the *agent's*
    correct actions through the same algebra as the other editor's, instead
    of hand-writing an expected end state.
    """
    params = interference.params
    content = str(params.get("content") or "")
    if not content:
        raise InterferenceError("reply_thread needs content")
    comment_id = params.get("comment_id")
    suggestion_id = params.get("suggestion_id")
    if (comment_id is None) == (suggestion_id is None):
        raise InterferenceError(
            "reply_thread needs exactly one of comment_id / suggestion_id"
        )
    if suggestion_id is not None:
        if str(suggestion_id) not in doc.registry:
            raise InterferenceError(f"no suggestion {suggestion_id!r} to reply to")
        backend.add_suggestion_reply(
            doc, str(suggestion_id), content, author=interference.editor
        )
        target = {"suggestion_id": str(suggestion_id)}
    else:
        backend.add_comment_reply(
            doc.document_id, str(comment_id), content, author=interference.editor
        )
        target = {"comment_id": str(comment_id)}
    return {**target, "content": content}, (True, True)


_HANDLERS: dict[
    str, Callable[[Any, MockDoc, Interference], tuple[dict, tuple[bool, bool]]]
] = {
    "resolve_under_agent": _do_resolve_under_agent,
    "shift_indexes": _do_shift_indexes,
    "resolve_thread": _do_resolve_thread,
    "overlapping_suggestion": _do_overlapping_suggestion,
    "merge_absorb": _do_merge_absorb,
    "reply_thread": _do_reply_thread,
}


def apply_interference(
    backend: Any, interference: Interference, *, check: bool = True
) -> tuple[dict[str, Any], list[str], str]:
    """Apply one interference; return ``(effect, violations, document_id)``.

    The single dispatch point, shared by the live engine and by
    :func:`replay`, so a grader's notion of "what the other editor did" is the
    same code that did it.
    """
    handler = _HANDLERS.get(interference.kind)
    if handler is None:  # pragma: no cover - parse_script rejects these first
        raise InterferenceError(f"unknown interference kind {interference.kind!r}")
    doc = _document(backend, interference)
    original_before = doc.original_text()
    final_before = doc.final_text()
    effect, (keeps_original, keeps_final) = handler(backend, doc, interference)
    violations: list[str] = []
    if check:
        violations = check_model(
            doc,
            original_before=original_before if keeps_original else None,
            final_before=final_before if keeps_final else None,
        )
    return effect, violations, doc.document_id


def replay(seed: dict[str, Any], ops: Sequence[Any], *, check: bool = True) -> Any:
    """Seed a fresh backend and apply ``ops`` in order; return the backend.

    This is how a grader computes the correct end state *with the other
    editor's operations applied*: seed, then interleave the other editor's
    interferences and the agent's right actions as ops in the same
    vocabulary, then compare projections. Nothing is hand-written, so an
    interference that changes never leaves a stale expectation behind.
    """
    from mockdocs.fake_services import FakeBackend

    backend = FakeBackend(me=str(seed.get("me") or "mockuser"))
    backend.seed(seed)
    for op in ops:
        interference = (
            op if isinstance(op, Interference) else Interference.from_dict(op)
        )
        _, violations, _ = apply_interference(backend, interference, check=check)
        if violations:
            raise InterferenceError(
                f"replay of {interference.name!r} violated the spec: "
                + "; ".join(violations)
            )
    return backend


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > ARG_CLIP:
        return value[:ARG_CLIP] + "…"
    return value


class InterferenceEngine:
    """Fires a script's interferences at points in the agent's call sequence.

    Stateless with respect to time: everything it decides is a function of
    the ordered sequence of agent tool calls it has been shown. Feed it the
    same sequence twice and it fires the same interferences at the same
    points, in the same order.
    """

    def __init__(
        self,
        backend: Any,
        interferences: Iterable[Interference],
        *,
        check: bool = True,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.backend = backend
        self.interferences = list(interferences)
        self.check = check
        self.on_change = on_change
        self.record = ConcurrencyRecord(declared=[i.name for i in self.interferences])
        backend.concurrency = self.record
        self._matches: dict[str, int] = {i.name: 0 for i in self.interferences}
        self._fired: set[str] = set()
        self._calls = 0
        self._tool_counts: dict[str, int] = {}

    # -- agent call bookkeeping ------------------------------------------
    def begin_call(self, tool: str, args: Optional[dict[str, Any]] = None) -> AgentCall:
        args = dict(args or {})
        self._calls += 1
        self._tool_counts[tool] = self._tool_counts.get(tool, 0) + 1
        call = AgentCall(
            ordinal=self._calls,
            tool=tool,
            tool_ordinal=self._tool_counts[tool],
            args={k: _clip(v) for k, v in args.items()},
        )
        self.record.agent_calls.append(call)
        return call

    def end_call(
        self, call: AgentCall, *, ok: bool, error: Optional[str] = None
    ) -> None:
        call.ok = ok
        call.error = _clip(error) if error else None

    # -- firing ----------------------------------------------------------
    def fire(self, phase: str, call: AgentCall) -> list[Fired]:
        """Fire every interference whose trigger this call satisfies in ``phase``."""
        out: list[Fired] = []
        for interference in self.interferences:
            if interference.trigger.when != phase:
                continue
            if interference.name in self._fired:
                continue
            if not interference.trigger.matches(call.tool, call.args):
                continue
            self._matches[interference.name] += 1
            if self._matches[interference.name] != interference.trigger.nth:
                continue
            out.append(self._apply(interference, phase, call))
        if out:
            self._changed()
        return out

    def _apply(self, interference: Interference, phase: str, call: AgentCall) -> Fired:
        self._fired.add(interference.name)
        try:
            effect, violations, doc_id = apply_interference(
                self.backend, interference, check=self.check
            )
        except Exception as exc:  # engine fault: record, never take the run down
            message = (
                f"{interference.name} ({interference.kind}) failed to apply at "
                f"agent call {call.ordinal} ({call.tool}): {type(exc).__name__}: {exc}"
            )
            self.record.errors.append(message)
            entry = Fired(
                name=interference.name,
                kind=interference.kind,
                when=phase,
                editor=interference.editor,
                at_call=call.ordinal,
                at_tool=call.tool,
                document_id=str(interference.document_id or ""),
                effect={"error": message},
                violations=[message],
            )
            self.record.fired.append(entry)
            return entry
        entry = Fired(
            name=interference.name,
            kind=interference.kind,
            when=phase,
            editor=interference.editor,
            at_call=call.ordinal,
            at_tool=call.tool,
            document_id=doc_id,
            effect=effect,
            violations=violations,
        )
        self.record.fired.append(entry)
        return entry

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # pragma: no cover - snapshot failure is not fatal
                pass

    # -- convenience for tests and oracles --------------------------------
    def around(
        self, tool: str, args: Optional[dict[str, Any]], fn: Callable[[], Any]
    ) -> Any:
        """Run ``fn`` as if it were the agent's ``tool`` call.

        Exactly the sequence :class:`InterferenceMiddleware` performs, exposed
        so an oracle (a scripted correct/naive solution) exercises the real
        firing logic rather than a re-implementation of it.
        """
        call = self.begin_call(tool, args)
        self.fire("before", call)
        try:
            result = fn()
        except Exception as exc:
            self.end_call(call, ok=False, error=f"{type(exc).__name__}: {exc}")
            self.fire("after", call)
            self._changed()
            raise
        self.end_call(call, ok=True)
        self.fire("after", call)
        self._changed()
        return result


# ---------------------------------------------------------------------------
# Server-side installation
# ---------------------------------------------------------------------------


def build_middleware(engine: InterferenceEngine) -> Any:
    """A FastMCP middleware that drives ``engine`` from real tool calls.

    ``fastmcp`` is imported lazily so that a grader process -- which reads
    :class:`ConcurrencyRecord` out of a state snapshot and never starts a
    server -- does not have to have it.
    """
    import asyncio

    from fastmcp.server.middleware import Middleware

    class InterferenceMiddleware(Middleware):
        """One lock for the whole tool call: see this module's docstring,
        point 2. A client may issue parallel ``tool_use`` blocks, and without
        serialising them the interference ordinals would depend on the event
        loop's scheduling rather than on the agent's behaviour."""

        def __init__(self) -> None:
            super().__init__()
            self._engine = engine
            self._lock = asyncio.Lock()

        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            message = getattr(context, "message", None)
            tool = str(getattr(message, "name", "") or "unknown")
            raw_args = getattr(message, "arguments", None) or {}
            args = (
                dict(raw_args)
                if isinstance(raw_args, dict)
                else {"_raw": str(raw_args)}
            )
            async with self._lock:
                call = self._engine.begin_call(tool, args)
                self._engine.fire("before", call)
                try:
                    result = await call_next(context)
                except Exception as exc:
                    self._engine.end_call(
                        call, ok=False, error=f"{type(exc).__name__}: {exc}"
                    )
                    self._engine.fire("after", call)
                    self._engine._changed()
                    raise
                failed = bool(
                    getattr(result, "isError", None)
                    or getattr(result, "is_error", None)
                )
                self._engine.end_call(call, ok=not failed)
                self._engine.fire("after", call)
                self._engine._changed()
                return result

    return InterferenceMiddleware()


def install_interference(
    backend: Any,
    interferences: Iterable[Interference],
    *,
    check: bool = True,
    on_change: Optional[Callable[[], None]] = None,
    server: Any = None,
) -> InterferenceEngine:
    """Attach an engine to the running MCP server.

    Registers the middleware on the shared FastMCP server (``core.server``
    unless one is passed), which is the only seam that sees whole agent tool
    calls -- names, arguments and boundaries. The API-call seam
    (``fake_services._Call.execute``) cannot: it sees the several reads one
    tool makes internally, not the call the agent chose to make.
    """
    engine = InterferenceEngine(
        backend, interferences, check=check, on_change=on_change
    )
    if server is None:
        from core.server import server as shared_server

        server = shared_server
    server.add_middleware(build_middleware(engine))
    return engine
