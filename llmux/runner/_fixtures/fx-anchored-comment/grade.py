"""Grade fx-anchored-comment: one comment anchored to the growth figure."""

from __future__ import annotations

from typing import Any

DOCUMENT_ID = "fx-doc-comment"
EXPECTED_FINAL = "Q3 revenue grew 40% year over year.\nWe expect similar growth in Q4.\n"
ANCHOR = "40%"


def grade(backend: Any) -> dict[str, Any]:
    failures: list[str] = []
    checks = 0
    passed = 0

    doc = backend.documents.get(DOCUMENT_ID)
    checks += 1
    if doc is None:
        failures.append(f"document {DOCUMENT_ID} is gone from the backend")
        return {"pass": False, "score": 0.0, "failures": failures}
    passed += 1

    checks += 1
    if doc.display_text() != EXPECTED_FINAL or doc.registry:
        failures.append(
            "the document text was modified; the brief asked for a comment only"
        )
    else:
        passed += 1

    threads = backend.comments.get(DOCUMENT_ID) or []
    checks += 1
    if len(threads) != 1:
        failures.append(f"expected exactly 1 comment thread, found {len(threads)}")
    else:
        passed += 1

    checks += 1
    anchored = [t for t in threads if ANCHOR in (t.get("plainTextQuote") or "")]
    if not anchored:
        quotes = [t.get("plainTextQuote") or "<unanchored>" for t in threads]
        failures.append(
            f"no comment is anchored to {ANCHOR!r} (quotes seen: {quotes}); an "
            "unanchored document-level comment does not satisfy the brief"
        )
    else:
        passed += 1

    checks += 1
    if anchored and not (anchored[0]["headPost"]["content"] or "").strip():
        failures.append("the anchored comment has empty content")
    else:
        passed += 1

    return {
        "pass": not failures,
        "score": passed / checks,
        "failures": failures,
    }
