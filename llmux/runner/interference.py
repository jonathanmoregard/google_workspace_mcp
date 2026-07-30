"""Runner-side wiring for :mod:`mockdocs.concurrency`.

Two halves, both deliberately additive so that a scenario without
interferences behaves exactly as it did before this module existed and
batches stay comparable with ``reports/20260730-211540.md``:

**Declaration.** A scenario declares its interferences in ``meta.json`` under
``interferences``. Nothing new appears on disk -- the corpus contract stays
``seed/brief/expected/grade/meta`` -- so the runner can consume an
interference scenario unchanged. :func:`materialise` writes the validated
script into the run directory and returns the env var that points the mock
server at it.

**Interpretation.** After the run, the server's :class:`ConcurrencyRecord`
comes back inside the state snapshot. :class:`InterferenceReport` turns it
into the questions a grader and the taxonomy actually ask:

- did every declared interference fire, and where in the agent's call
  sequence (a scenario whose interference never fired proves nothing);
- which ids did the other editor make vanish;
- by how many UTF-16 units did it move the indexes, and at which call;
- did the agent *look again* after the change, or act on what it had cached;
- and -- the harness's own honesty check -- did the interleaving itself break
  a spec invariant, in which case the run is a harness fault and the agent
  must not be blamed for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from mockdocs.concurrency import (
    SCHEMA_VERSION,
    AgentCall,
    ConcurrencyRecord,
    Fired,
    Interference,
    InterferenceError,
    parse_script,
)
from llmux.runner.transcript import READ_TOOL_SUFFIXES

#: ``meta.json`` key carrying the scripted second editor.
META_KEY = "interferences"

#: Env var the mock server reads the script from (``mockdocs/serve.py``).
ENV_VAR = "MOCKDOCS_INTERFERENCE"

#: Tool arguments that name a suggestion / comment / post id. Used to decide
#: whether a call acted on something the other editor had already removed.
ID_ARGS = ("suggestion_id", "comment_id", "post_id", "reply_id", "thread_id")

#: Tool arguments that carry an index the agent computed from an earlier
#: read. A write carrying one of these after an unobserved shift is the
#: silent-corruption case.
INDEX_ARGS = ("start_index", "end_index", "index")


def declared_interferences(scenario: Any) -> list[Interference]:
    """Parse a scenario's declared interferences (empty when it has none)."""
    raw = (scenario.meta or {}).get(META_KEY)
    if not raw:
        return []
    return parse_script(raw)


def build_script(interferences: Sequence[Interference]) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "interferences": [i.as_dict() for i in interferences],
    }


def materialise(scenario: Any, run_dir: Path) -> dict[str, str]:
    """Write the scenario's script into ``run_dir``; return env for the server.

    Returns an empty dict when the scenario declares no interference, so the
    caller can merge it unconditionally.
    """
    interferences = declared_interferences(scenario)
    if not interferences:
        return {}
    path = Path(run_dir) / "interference.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_script(interferences), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {ENV_VAR: str(path.resolve())}


# ---------------------------------------------------------------------------
# Reading the interleaving back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterferenceReport:
    """What the other editor did, and what the agent did about it."""

    record: ConcurrencyRecord

    @classmethod
    def from_backend(cls, backend: Any) -> Optional["InterferenceReport"]:
        record = getattr(backend, "concurrency", None)
        if record is None:
            return None
        return cls(record=record)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Optional["InterferenceReport"]:
        if "concurrency" not in (state or {}):
            return None
        return cls(record=ConcurrencyRecord.from_dict(state["concurrency"]))

    # -- basics ----------------------------------------------------------
    @property
    def declared(self) -> list[str]:
        return list(self.record.declared)

    @property
    def fired(self) -> list[Fired]:
        return list(self.record.fired)

    @property
    def fired_names(self) -> list[str]:
        return self.record.fired_names

    @property
    def unfired(self) -> list[str]:
        return self.record.unfired

    @property
    def calls(self) -> list[AgentCall]:
        return list(self.record.agent_calls)

    @property
    def violations(self) -> list[str]:
        """Non-empty means the harness broke, not the agent. Every grader
        checks this first and reports it as a harness fault."""
        return self.record.violations

    @property
    def ineffective(self) -> list[tuple[str, str]]:
        """Interferences that fired but changed nothing.

        A silent no-op is the subtlest way an interference scenario can rot:
        it looks like it ran, the report says it fired, and the agent sailed
        through a document nobody else ever touched. The engine deliberately
        does not raise on a missing target -- a script must stay safe to
        apply to any state -- so the emptiness is caught here instead, and
        treated as a harness fault rather than a well-behaved agent.
        """
        out: list[tuple[str, str]] = []
        for entry in self.record.fired:
            effect = entry.effect or {}
            if "error" in effect:
                out.append((entry.name, str(effect["error"])))
            elif effect.get("existed") is False:
                target = (
                    effect.get("suggestion_id")
                    or effect.get("comment_id")
                    or "its target"
                )
                out.append(
                    (entry.name, f"{target} was not in the document, so it did nothing")
                )
            elif entry.kind == "merge_absorb" and effect.get("merged") is False:
                out.append(
                    (
                        entry.name,
                        "the adjacent same-author edit did not merge, so no id "
                        "was absorbed",
                    )
                )
        return out

    @property
    def first_fire(self) -> Optional[int]:
        ordinals = [f.at_call for f in self.record.fired]
        return min(ordinals) if ordinals else None

    def fire(self, name: str) -> Optional[Fired]:
        for entry in self.record.fired:
            if entry.name == name:
                return entry
        return None

    # -- derived signals --------------------------------------------------
    @property
    def vanished_ids(self) -> dict[str, int]:
        """Id -> the agent-call ordinal at which the other editor removed it.

        Covers every route an id can stop existing: a resolution (plus the
        suggestions garbage-collected with it, SPEC §11.1 I2), a §6 merge
        absorbing it, and a deleted comment thread.
        """
        out: dict[str, int] = {}
        for entry in self.record.fired:
            effect = entry.effect or {}
            gone: list[str] = []
            if entry.kind == "resolve_under_agent" and effect.get("existed"):
                gone.append(str(effect.get("suggestion_id")))
                gone.extend(str(x) for x in effect.get("also_removed") or [])
            elif entry.kind == "merge_absorb":
                gone.extend(str(x) for x in effect.get("absorbed_ids") or [])
                gone.extend(str(x) for x in effect.get("vanished_ids") or [])
            elif entry.kind == "resolve_thread" and effect.get("action") == "delete":
                if effect.get("existed"):
                    gone.append(str(effect.get("comment_id")))
            for sid in gone:
                if sid and sid != "None":
                    out.setdefault(sid, entry.at_call)
        return out

    @property
    def shifts(self) -> list[tuple[int, int]]:
        """``(agent-call ordinal, UTF-16 shift)`` for every index-moving event."""
        out = []
        for entry in self.record.fired:
            shift = int((entry.effect or {}).get("utf16_shift") or 0)
            if shift:
                out.append((entry.at_call, shift))
        return out

    def read_calls_after(self, ordinal: int) -> list[AgentCall]:
        """Successful read tool calls strictly after ``ordinal``.

        This is the mechanical definition of "the agent looked again". It
        counts read TOOL CALLS only -- a write tool's own internal
        verification read does not count, because it happens *after* the
        request carrying the stale argument was already sent.
        """
        return [
            call
            for call in self.record.agent_calls
            if call.ordinal > ordinal
            and call.tool in READ_TOOL_SUFFIXES
            and call.ok is not False
        ]

    def reread_after_change(self) -> bool:
        first = self.first_fire
        return bool(first is not None and self.read_calls_after(first))

    def calls_referencing(self, identifier: str) -> list[AgentCall]:
        return [
            call
            for call in self.record.agent_calls
            if any(str(call.args.get(key) or "") == identifier for key in ID_ARGS)
        ]

    def index_writes_after(self, ordinal: int) -> list[AgentCall]:
        """Successful calls after ``ordinal`` that carried a computed index."""
        return [
            call
            for call in self.record.agent_calls
            if call.ordinal > ordinal
            and call.ok
            and any(key in call.args for key in INDEX_ARGS)
        ]

    def blind_retries(self) -> list[tuple[AgentCall, AgentCall]]:
        """A failed call on a vanished id, repeated with no read in between.

        The behaviour the grader must NOT credit: the agent noticed the error
        and simply tried again instead of finding out what changed.
        """
        out: list[tuple[AgentCall, AgentCall]] = []
        for identifier in self.vanished_ids:
            touching = self.calls_referencing(identifier)
            for first, second in zip(touching, touching[1:]):
                if first.ok is False and not self._read_between(
                    first.ordinal, second.ordinal
                ):
                    out.append((first, second))
        return out

    def _read_between(self, low: int, high: int) -> bool:
        return any(
            low < call.ordinal < high
            and call.tool in READ_TOOL_SUFFIXES
            and call.ok is not False
            for call in self.record.agent_calls
        )

    def stale_index_writes(self) -> list[tuple[AgentCall, int]]:
        """Successful index-bearing writes made after an unobserved shift.

        The write did not fail -- that is the whole point. It landed
        ``shift`` UTF-16 units away from the text the agent read.
        """
        out: list[tuple[AgentCall, int]] = []
        for ordinal, shift in self.shifts:
            for call in self.index_writes_after(ordinal):
                if self._read_between(ordinal, call.ordinal):
                    continue
                out.append((call, shift))
        return out

    def as_dict(self) -> dict[str, Any]:
        """The slice a report renders: what fired, where, and how it landed."""
        return {
            "declared": self.declared,
            "fired": [
                {
                    "name": f.name,
                    "kind": f.kind,
                    "when": f.when,
                    "at_call": f.at_call,
                    "at_tool": f.at_tool,
                    "editor": f.editor,
                }
                for f in self.record.fired
            ],
            "unfired": self.unfired,
            "ineffective": [f"{name}: {why}" for name, why in self.ineffective],
            "agent_calls": len(self.record.agent_calls),
            "vanished_ids": self.vanished_ids,
            "reread_after_change": self.reread_after_change(),
            "blind_retries": len(self.blind_retries()),
            "stale_index_writes": len(self.stale_index_writes()),
            "violations": self.violations,
        }


def report_from_state_file(path: Path) -> Optional[InterferenceReport]:
    """Read a state dump and pull the interleaving out of it, if any."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    return InterferenceReport.from_state(state)


__all__ = [
    "ENV_VAR",
    "META_KEY",
    "InterferenceError",
    "InterferenceReport",
    "build_script",
    "declared_interferences",
    "materialise",
    "report_from_state_file",
]
