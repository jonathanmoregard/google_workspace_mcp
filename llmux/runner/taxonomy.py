"""Classify what went wrong in a run, from its tool calls and its grade.

The point of the harness is not the pass rate -- it is knowing *which tool
surface* produced the failure, so the taxonomy is deliberately mechanical:
every finding cites the call index or the grader failure it came from, and
nothing is inferred from the agent's prose except the give-up signal (where
prose is the only evidence there is).

Repeats are what turn a finding into a work item, so they are detected
mechanically too: the same class twice in one run
(:func:`classify` sets ``repeated``), or in >= ``REPEAT_RUN_SHARE`` of all
runs (:func:`repeat_report`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from llmux.runner.transcript import ToolCall, Transcript

#: Class -> what it means, and what a fix would look like. Rendered into the
#: report so the taxonomy explains itself to whoever reads it.
CLASSES: dict[str, str] = {
    "wrong_tool_for_intent": (
        "Reached for a tool that does something adjacent to the request "
        "(direct edit instead of a suggestion, unanchored comment instead of "
        "an anchored one). Points at naming/description overlap."
    ),
    "param_shape_error": (
        "A call was rejected for the shape of its arguments (missing or "
        "mutually exclusive parameters, wrong types, invalid enum). Points at "
        "schema ergonomics."
    ),
    "index_error": (
        "A call was rejected or mis-landed on indexes: out of range, "
        "end <= start, or a UTF-16 vs code-point offset mismatch. Points at "
        "the index contract being hard to honour."
    ),
    "stale_state": (
        "Acted on a suggestion/comment/post id that no longer existed -- the "
        "run did not re-read after a mutation changed the ids."
    ),
    "gave_up_early": (
        "Terminated without completing the task: budget/timeout exhausted, or "
        "the agent declared it could not do it."
    ),
    "hallucinated_tool": (
        "Called a tool name that does not exist, or passed a parameter the "
        "schema does not have."
    ),
    "ignored_error": (
        "A tool returned an error and the run moved on without retrying or "
        "repairing that call."
    ),
    "accepted_when_should_reject": (
        "Semantic inversion: resolved a suggestion the wrong way round "
        "(accepted what should have been rejected, or vice versa)."
    ),
    "no_end_state_verification": (
        "Made writes and never read the document back, so the run could not "
        "know whether its edits landed."
    ),
    "harness_mcp_unavailable": (
        "HARNESS FAULT, not an agent mistake: the mock MCP server never "
        "became reachable, so the run had no tools to use."
    ),
}

#: A class showing up in at least this share of runs is a systemic finding.
REPEAT_RUN_SHARE = 0.30

_HALLUCINATED_PATTERNS = (
    re.compile(r"no such tool", re.I),
    re.compile(r"tool .*(?:not found|does not exist|is not available)", re.I),
    re.compile(r"unknown tool", re.I),
    re.compile(r"unexpected keyword argument", re.I),
    re.compile(r"(?:unexpected|unrecognized|unknown) (?:parameter|argument|field)", re.I),
    re.compile(r"got an unexpected", re.I),
)

_INDEX_PATTERNS = (
    re.compile(r"start_index", re.I),
    re.compile(r"end_index", re.I),
    re.compile(r"index .*out of range", re.I),
    re.compile(r"out of range", re.I),
    re.compile(r"must be greater than", re.I),
    re.compile(r"must be >= ?1", re.I),
    re.compile(r"utf-?16", re.I),
    re.compile(r"invalid requests\[\d+\]", re.I),
)

_PARAM_PATTERNS = (
    re.compile(r"input validation error", re.I),
    re.compile(r"validation error", re.I),
    re.compile(r"field required", re.I),
    re.compile(r"missing .*required", re.I),
    re.compile(r"required (?:property|parameter|argument)", re.I),
    re.compile(r"provide exactly one of", re.I),
    re.compile(r"provide text \(insertion\)", re.I),
    re.compile(r"must be one of", re.I),
    re.compile(r"invalid (?:action|view_mode|value|type)", re.I),
    re.compile(r"must be non-empty", re.I),
)

_STALE_PATTERNS = (
    re.compile(r"suggestion id .* is invalid", re.I),
    re.compile(r"suggestion .* (?:was )?not found", re.I),
    re.compile(r"comment .* (?:was )?not found", re.I),
    re.compile(r"post .* (?:was )?not found", re.I),
    re.compile(r"requested entity was not found", re.I),
)

_GAVE_UP_PATTERNS = (
    re.compile(r"\b(?:i (?:was )?(?:could not|couldn't|cannot|can't|am unable|was unable))", re.I),
    re.compile(r"unable to (?:complete|finish|do this|proceed)", re.I),
    re.compile(r"no (?:suitable )?tool (?:is )?available", re.I),
    re.compile(r"i don'?t have (?:a|the|any) tool", re.I),
    re.compile(r"i (?:will )?stop(?:ped)? here", re.I),
)

_SEMANTIC_INVERSION_PATTERNS = (
    re.compile(r"should have been (?:accepted|rejected)", re.I),
    re.compile(r"accepted when it should", re.I),
    re.compile(r"rejected when it should", re.I),
)

_WRONG_TOOL_GRADE_PATTERNS = (
    re.compile(r"applied directly instead of being suggested", re.I),
    re.compile(r"unanchored", re.I),
    re.compile(r"does not satisfy the brief", re.I),
    re.compile(r"the document text was modified", re.I),
)

_STALE_GRADE_PATTERNS = (re.compile(r"still pending", re.I),)

#: Scenario tags that mean "the work must land as a pending suggestion".
SUGGESTION_TAGS = frozenset({"suggestions", "suggestion", "suggest-edit", "review"})
#: Scenario tags that mean "the comment has to be anchored to a range".
ANCHORED_TAGS = frozenset({"anchored", "anchor"})
#: Drive-surface comment tool: creates document-level (unanchored) comments.
UNANCHORED_COMMENT_TOOL = "manage_document_comment"


@dataclass(frozen=True)
class ScenarioFacts:
    """The slice of a scenario the taxonomy reasons about.

    A plain value object so classification can be unit-tested with synthetic
    transcripts and no corpus on disk.
    """

    id: str = "scenario"
    difficulty: str = "unknown"
    tags: tuple[str, ...] = ()
    expected: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_scenario(cls, scenario: Any) -> "ScenarioFacts":
        return cls(
            id=scenario.id,
            difficulty=scenario.difficulty,
            tags=tuple(scenario.tags),
            expected=dict(scenario.expected),
        )

    @property
    def wants_suggestion(self) -> bool:
        if SUGGESTION_TAGS & set(self.tags):
            return True
        try:
            return int(self.expected.get("pending_suggestions") or 0) > 0
        except (TypeError, ValueError):
            return False

    @property
    def wants_anchor(self) -> bool:
        return bool(ANCHORED_TAGS & set(self.tags)) or bool(
            self.expected.get("anchor_quote_contains")
        )


@dataclass(frozen=True)
class Finding:
    """One classified mistake, always with the evidence that produced it."""

    code: str
    detail: str
    source: str = "tool_call"
    tool: Optional[str] = None
    call_index: Optional[int] = None
    repeated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "source": self.source,
            "tool": self.tool,
            "call_index": self.call_index,
            "repeated": self.repeated,
        }


def _first_match(text: str, patterns: Iterable[re.Pattern[str]]) -> Optional[str]:
    for pattern in patterns:
        found = pattern.search(text or "")
        if found:
            return found.group(0)
    return None


def classify_error(call: ToolCall, prior_write: bool) -> Finding:
    """Map one failed call to its class (most specific rule wins)."""
    text = call.result_text or ""
    hit = _first_match(text, _HALLUCINATED_PATTERNS)
    if hit:
        return Finding(
            "hallucinated_tool",
            f"{call.name} rejected: {hit!r}",
            tool=call.tool,
            call_index=call.index,
        )
    hit = _first_match(text, _STALE_PATTERNS)
    if hit:
        code = "stale_state" if prior_write else "param_shape_error"
        detail = f"{call.tool} referenced a missing id: {hit!r}"
        if not prior_write:
            detail += " (no prior mutation: the id was never valid)"
        return Finding(code, detail, tool=call.tool, call_index=call.index)
    hit = _first_match(text, _INDEX_PATTERNS)
    if hit:
        return Finding(
            "index_error",
            f"{call.tool} rejected on indexes: {hit!r}",
            tool=call.tool,
            call_index=call.index,
        )
    hit = _first_match(text, _PARAM_PATTERNS)
    if hit:
        return Finding(
            "param_shape_error",
            f"{call.tool} rejected on argument shape: {hit!r}",
            tool=call.tool,
            call_index=call.index,
        )
    excerpt = " ".join(text.split())[:160]
    return Finding(
        "param_shape_error",
        f"{call.tool} returned an unclassified error: {excerpt!r}",
        tool=call.tool,
        call_index=call.index,
    )


def _unknown_tool_findings(transcript: Transcript) -> list[Finding]:
    """Names the agent invented, judged only against a complete tool list.

    ``system/init`` frequently fires while the MCP server is still connecting
    and advertises no MCP tools at all. Treating that empty list as ground
    truth would brand every legitimate call a hallucination -- so the rule
    only applies once the list actually contains MCP tools.
    """
    known = set(transcript.available_tools)
    if not transcript.advertised_mcp_tools:
        return []
    return [
        Finding(
            "hallucinated_tool",
            f"called {call.name!r}, which was not in the session's tool list",
            tool=call.tool,
            call_index=call.index,
        )
        for call in transcript.agent_tool_calls
        if call.name not in known
    ]


def _ignored_error_findings(calls: Sequence[ToolCall], succeeded: bool) -> list[Finding]:
    out: list[Finding] = []
    for position, call in enumerate(calls):
        if not call.failed:
            continue
        later = calls[position + 1 :]
        retried = any(other.tool == call.tool for other in later)
        if retried:
            continue
        if later or succeeded:
            out.append(
                Finding(
                    "ignored_error",
                    f"{call.tool} failed at call {call.index} and was never "
                    "retried or repaired",
                    tool=call.tool,
                    call_index=call.index,
                )
            )
    return out


def _wrong_tool_findings(
    facts: ScenarioFacts, calls: Sequence[ToolCall]
) -> list[Finding]:
    out: list[Finding] = []
    if facts.wants_suggestion:
        for call in calls:
            if call.is_direct_edit and not call.failed:
                out.append(
                    Finding(
                        "wrong_tool_for_intent",
                        f"{call.tool} applies the edit straight to the "
                        "document; the task asked for a pending suggestion "
                        "(suggest_doc_edit)",
                        tool=call.tool,
                        call_index=call.index,
                    )
                )
    if facts.wants_anchor:
        for call in calls:
            if call.tool == UNANCHORED_COMMENT_TOOL and not call.failed:
                action = str(call.args.get("action") or "").lower()
                if action in ("", "create"):
                    out.append(
                        Finding(
                            "wrong_tool_for_intent",
                            f"{call.tool} creates a document-level comment; the "
                            "task needed create_anchored_doc_comment",
                            tool=call.tool,
                            call_index=call.index,
                        )
                    )
    return out


def _inversion_findings(
    facts: ScenarioFacts, calls: Sequence[ToolCall]
) -> list[Finding]:
    """Accept/reject applied to the id the ground truth assigned the other way.

    ``expected.json`` is free to omit ``accept``/``reject``; the rule simply
    does not fire then, and the grade-failure rule still catches inversions
    the grader describes.
    """
    expected_accept = {str(x) for x in (facts.expected.get("accept") or [])}
    expected_reject = {str(x) for x in (facts.expected.get("reject") or [])}
    if not expected_accept and not expected_reject:
        return []
    out: list[Finding] = []
    for call in calls:
        if call.tool != "manage_document_suggestion" or call.failed:
            continue
        action = str(call.args.get("action") or "").lower()
        sid = str(call.args.get("suggestion_id") or "")
        if action == "accept" and sid in expected_reject:
            out.append(
                Finding(
                    "accepted_when_should_reject",
                    f"accepted {sid}, which the ground truth rejects",
                    tool=call.tool,
                    call_index=call.index,
                )
            )
        elif action == "reject" and sid in expected_accept:
            out.append(
                Finding(
                    "accepted_when_should_reject",
                    f"rejected {sid}, which the ground truth accepts",
                    tool=call.tool,
                    call_index=call.index,
                )
            )
    return out


def _grade_findings(failures: Sequence[str]) -> list[Finding]:
    out: list[Finding] = []
    for failure in failures:
        if _first_match(failure, _SEMANTIC_INVERSION_PATTERNS):
            code = "accepted_when_should_reject"
        elif _first_match(failure, _WRONG_TOOL_GRADE_PATTERNS):
            code = "wrong_tool_for_intent"
        elif _first_match(failure, _STALE_GRADE_PATTERNS):
            code = "gave_up_early"
        elif _first_match(failure, _INDEX_PATTERNS) or "index" in failure.lower():
            code = "index_error"
        else:
            continue
        out.append(
            Finding(code, f"grader: {' '.join(failure.split())[:200]}", source="grade")
        )
    return out


def _mark_repeats(findings: list[Finding]) -> list[Finding]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return [
        Finding(
            f.code,
            f.detail,
            f.source,
            f.tool,
            f.call_index,
            repeated=counts.get(f.code, 0) >= 2,
        )
        for f in findings
    ]


def classify(
    facts: ScenarioFacts,
    transcript: Transcript,
    *,
    passed: bool,
    failures: Sequence[str] = (),
    timed_out: bool = False,
) -> list[Finding]:
    """All findings for one run, with in-run repeats flagged."""
    calls = transcript.agent_tool_calls
    findings: list[Finding] = []

    if not transcript.mcp_connected:
        findings.append(
            Finding(
                "harness_mcp_unavailable",
                "no mock MCP tool was ever reachable in this run "
                f"(servers: {transcript.mcp_servers or 'none reported'})",
                source="transcript",
            )
        )

    findings.extend(_unknown_tool_findings(transcript))

    seen_successful_write = False
    for call in calls:
        if call.failed:
            findings.append(classify_error(call, prior_write=seen_successful_write))
        elif call.is_write:
            seen_successful_write = True

    findings.extend(_ignored_error_findings(calls, succeeded=passed))
    findings.extend(_wrong_tool_findings(facts, calls))
    findings.extend(_inversion_findings(facts, calls))
    findings.extend(_grade_findings(failures))

    writes = [c for c in calls if c.is_write and not c.failed]
    if writes:
        last_write = writes[-1].index
        if not any(c.is_read and c.index > last_write and not c.failed for c in calls):
            findings.append(
                Finding(
                    "no_end_state_verification",
                    "the run never read the document back after its last write",
                    source="transcript",
                    tool=writes[-1].tool,
                    call_index=last_write,
                )
            )

    gave_up_reason: Optional[str] = None
    if timed_out:
        gave_up_reason = "the run hit the harness wall-clock timeout"
    elif transcript.subtype and transcript.subtype != "success":
        gave_up_reason = f"terminated with subtype {transcript.subtype!r}"
    elif transcript.terminal_reason and transcript.terminal_reason not in (
        "completed",
        "",
    ):
        gave_up_reason = f"terminal_reason {transcript.terminal_reason!r}"
    else:
        hit = _first_match(transcript.final_text, _GAVE_UP_PATTERNS)
        if hit and not passed:
            gave_up_reason = f"final message says {hit!r}"
    if gave_up_reason is None and not passed and not writes and transcript.mcp_connected:
        gave_up_reason = "failed without making a single successful write call"
    if gave_up_reason:
        findings.append(Finding("gave_up_early", gave_up_reason, source="transcript"))

    return _mark_repeats(findings)


def repeat_report(
    per_run_findings: Sequence[Sequence[Finding]],
) -> dict[str, dict[str, Any]]:
    """Aggregate findings across runs and flag the systemic ones.

    A class is systemic when it repeats inside a single run or shows up in at
    least :data:`REPEAT_RUN_SHARE` of the batch -- the two mechanical signals
    that separate "one agent had a bad day" from "the tool surface is wrong".
    """
    total_runs = len(per_run_findings)
    stats: dict[str, dict[str, Any]] = {}
    for findings in per_run_findings:
        codes = [f.code for f in findings]
        for code in set(codes):
            entry = stats.setdefault(
                code,
                {
                    "occurrences": 0,
                    "runs": 0,
                    "repeated_within_run": 0,
                    "description": CLASSES.get(code, ""),
                    "examples": [],
                },
            )
            entry["runs"] += 1
            entry["occurrences"] += codes.count(code)
            if codes.count(code) >= 2:
                entry["repeated_within_run"] += 1
            for finding in findings:
                if finding.code == code and len(entry["examples"]) < 3:
                    entry["examples"].append(finding.detail)
    for code, entry in stats.items():
        share = entry["runs"] / total_runs if total_runs else 0.0
        entry["run_share"] = round(share, 3)
        entry["systemic"] = bool(
            entry["repeated_within_run"] or share >= REPEAT_RUN_SHARE
        )
    return dict(
        sorted(stats.items(), key=lambda kv: (-kv[1]["runs"], -kv[1]["occurrences"]))
    )
