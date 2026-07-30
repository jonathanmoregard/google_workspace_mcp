"""End-state grading shared by every generated ``grade.py``.

Grades the **document, not the transcript**. Many tool-call paths reach the
same correct end state -- accept in any order (L3), read once or ten times,
resolve before or after commenting -- and an LLM-UX measurement that scored
the path would be measuring the harness author's taste instead of the tool
surface. So every check here is a question about the post-run
:class:`~mockdocs.fake_services.FakeBackend`.

Partial credit is per sub-goal: text, survivors, each resolution, each
thread expectation, each invariant. An agent that resolved three of five
suggestions correctly scores meaningfully above one that resolved none, which
is what makes the corpus usable as a *gradient* rather than a pass gate.

One thing deliberately not graded: whether a resolved suggestion was
accepted or rejected *per se*. §7 deletes the registry entry either way and
the mock keeps no resolution log, so the distinction is observable only
through its effect on the text -- which the text checks already cover.
``resolved`` therefore grades "this suggestion is no longer pending", and
the accept/reject distinction is carried by ``final_text``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from mockdocs.model import MockDoc

#: Sub-goal weights. Text and survivors dominate because they are the
#: end state; the rest are per-item and add up on their own.
WEIGHT_TEXT = 3.0
WEIGHT_SURVIVORS = 2.0
WEIGHT_ITEM = 1.0

Check = tuple[str, float, Callable[[], Optional[str]]]


def load_expected(scenario_dir: str | Path) -> dict[str, Any]:
    path = Path(scenario_dir) / "expected.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# thread expectations
# ---------------------------------------------------------------------------


def _suggestion_reply_failure(doc: MockDoc, exp: dict[str, Any]) -> Optional[str]:
    sid = exp["suggestion_id"]
    sug = doc.registry.get(sid)
    if sug is None:
        return (
            f"suggestion {sid} is no longer pending, so its thread (and the "
            f"reply that should be on it) is gone"
        )
    posts = list(sug.thread)
    author = exp.get("author")
    if author is not None:
        posts = [p for p in posts if p.author == author]
    regex = exp.get("content_regex")
    if regex:
        posts = [p for p in posts if re.search(regex, p.content)]
    contains = exp.get("content_contains")
    if contains:
        posts = [p for p in posts if contains in p.content]
    minimum = exp.get("min_count", 1)
    if len(posts) < minimum:
        detail = f" matching /{regex}/" if regex else ""
        return (
            f"suggestion {sid}: expected at least {minimum} reply{detail} by "
            f"{author or 'anyone'}, found {len(posts)}"
        )
    return None


def _no_suggestion_reply_failure(doc: MockDoc, exp: dict[str, Any]) -> Optional[str]:
    sid = exp["suggestion_id"]
    sug = doc.registry.get(sid)
    if sug is None:
        return f"suggestion {sid} should still be pending but is not"
    author = exp.get("author")
    posts = [p for p in sug.thread if author is None or p.author == author]
    if posts:
        return f"suggestion {sid} should have no reply, found {len(posts)}"
    return None


def _comment_thread_failure(
    threads: list[dict[str, Any]], exp: dict[str, Any]
) -> Optional[str]:
    candidates = list(threads)
    if "quote" in exp:
        candidates = [t for t in candidates if t.get("plainTextQuote") == exp["quote"]]
    if "quote_contains" in exp:
        candidates = [
            t
            for t in candidates
            if exp["quote_contains"] in (t.get("plainTextQuote") or "")
        ]
    if "content_contains" in exp:
        candidates = [
            t
            for t in candidates
            if exp["content_contains"]
            in ((t.get("headPost") or {}).get("content") or "")
        ]
    if "content_regex" in exp:
        candidates = [
            t
            for t in candidates
            if re.search(
                exp["content_regex"], ((t.get("headPost") or {}).get("content") or "")
            )
        ]
    if "reply_contains" in exp:
        candidates = [
            t
            for t in candidates
            if any(
                exp["reply_contains"] in (p.get("content") or "")
                for p in (t.get("replies") or [])
            )
        ]
    minimum = exp.get("min_count", 1)
    maximum = exp.get("max_count")
    if len(candidates) < minimum:
        quotes = sorted({(t.get("plainTextQuote") or "") for t in threads})
        return (
            f"expected at least {minimum} comment thread matching {exp!r}, found "
            f"{len(candidates)} (quotes present: {quotes})"
        )
    if maximum is not None and len(candidates) > maximum:
        return (
            f"expected at most {maximum} comment thread matching {exp!r}, found "
            f"{len(candidates)}"
        )
    return None


def _thread_expectation_failure(
    doc: MockDoc, threads: list[dict[str, Any]], exp: dict[str, Any]
) -> Optional[str]:
    kind = exp.get("kind")
    if kind == "suggestion_reply":
        return _suggestion_reply_failure(doc, exp)
    if kind == "no_suggestion_reply":
        return _no_suggestion_reply_failure(doc, exp)
    if kind == "comment_thread":
        return _comment_thread_failure(threads, exp)
    if kind == "comment_count":
        if len(threads) != exp["equals"]:
            return (
                f"expected {exp['equals']} comment thread(s) on the document, "
                f"found {len(threads)}"
            )
        return None
    return f"unknown thread expectation kind {kind!r}"


# ---------------------------------------------------------------------------
# invariant checks
# ---------------------------------------------------------------------------


def _projection_text(doc: MockDoc, projection: str) -> str:
    return {
        "display": doc.display_text,
        "original": doc.original_text,
        "final": doc.final_text,
    }[projection]()


def _invariant_failure(
    doc: MockDoc, threads: list[dict[str, Any]], check: dict[str, Any]
) -> Optional[str]:
    kind = check.get("check")
    if kind == "projection_text":
        projection = check.get("projection", "display")
        actual = _projection_text(doc, projection)
        if actual != check["equals"]:
            return (
                f"{projection} projection is {actual!r}, expected {check['equals']!r}"
            )
        return None
    if kind == "text_present":
        actual = _projection_text(doc, check.get("projection", "display"))
        if check["text"] not in actual:
            return f"expected {check['text']!r} to be present in the document"
        return None
    if kind == "text_absent":
        actual = _projection_text(doc, check.get("projection", "display"))
        if check["text"] in actual:
            return f"expected {check['text']!r} to be gone from the document"
        return None
    if kind == "suggestion_count":
        if len(doc.registry) != check["equals"]:
            return (
                f"expected {check['equals']} pending suggestion(s), found "
                f"{len(doc.registry)}: {sorted(doc.registry)}"
            )
        return None
    if kind == "authored_suggestion_count":
        author = check["author"]
        owned = [s for s in doc.registry.values() if s.author == author]
        if len(owned) != check["equals"]:
            return (
                f"expected {check['equals']} pending suggestion(s) authored by "
                f"{author}, found {len(owned)}"
            )
        return None
    if kind == "comment_count":
        if len(threads) != check["equals"]:
            return f"expected {check['equals']} comment thread(s), found {len(threads)}"
        return None
    if kind == "model_invariants":
        try:
            doc.check_invariants()
        except AssertionError as exc:
            return f"model invariant violated in the end state: {exc}"
        return None
    return f"unknown invariant check {kind!r}"


# ---------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------


def grade_scenario(backend: Any, scenario_dir: str | Path) -> dict[str, Any]:
    """Grade ``backend`` against ``<scenario_dir>/expected.json``."""
    expected = load_expected(scenario_dir)
    return grade_against(backend, expected)


def grade_against(backend: Any, expected: dict[str, Any]) -> dict[str, Any]:
    document_id = expected["document_id"]
    doc = getattr(backend, "documents", {}).get(document_id)
    if doc is None:
        return {
            "pass": False,
            "score": 0.0,
            "failures": [
                f"document {document_id} is not in the backend "
                f"(have: {sorted(getattr(backend, 'documents', {}))})"
            ],
        }
    threads = list((getattr(backend, "comments", {}) or {}).get(document_id) or [])

    checks: list[Check] = []

    checks.append(
        (
            "final_text",
            WEIGHT_TEXT,
            lambda: (
                None
                if doc.display_text() == expected["final_text"]
                else (
                    f"document text is {doc.display_text()!r}, expected "
                    f"{expected['final_text']!r}"
                )
            ),
        )
    )

    def survivors_failure() -> Optional[str]:
        actual = sorted(doc.registry)
        wanted = sorted(expected.get("surviving_suggestion_ids") or [])
        if actual == wanted:
            return None
        extra = [s for s in actual if s not in wanted]
        missing = [s for s in wanted if s not in actual]
        parts = []
        if extra:
            parts.append(f"still pending but should be resolved: {extra}")
        if missing:
            parts.append(f"resolved but should still be pending: {missing}")
        return "; ".join(parts)

    checks.append(("surviving_suggestions", WEIGHT_SURVIVORS, survivors_failure))

    for sid, action in sorted((expected.get("resolved") or {}).items()):

        def resolution_failure(sid: str = sid, action: str = action) -> Optional[str]:
            if sid in doc.registry:
                return f"{sid} should have been {action} but is still pending"
            return None

        checks.append((f"resolved:{sid}", WEIGHT_ITEM, resolution_failure))

    for i, exp in enumerate(expected.get("thread_expectations") or []):

        def thread_failure(exp: dict[str, Any] = exp) -> Optional[str]:
            return _thread_expectation_failure(doc, threads, exp)

        checks.append((f"thread[{i}]:{exp.get('kind')}", WEIGHT_ITEM, thread_failure))

    for i, check in enumerate(expected.get("invariant_checks") or []):

        def invariant_failure(check: dict[str, Any] = check) -> Optional[str]:
            return _invariant_failure(doc, threads, check)

        checks.append(
            (f"invariant[{i}]:{check.get('check')}", WEIGHT_ITEM, invariant_failure)
        )

    failures: list[str] = []
    earned = 0.0
    total = 0.0
    for name, weight, run in checks:
        total += weight
        try:
            failure = run()
        except Exception as exc:  # a malformed expectation must not crash grading
            failure = f"check raised {type(exc).__name__}: {exc}"
        if failure is None:
            earned += weight
        else:
            failures.append(f"[{name}] {failure}")

    return {
        "pass": not failures,
        "score": round(earned / total, 6) if total else 0.0,
        "failures": failures,
    }
