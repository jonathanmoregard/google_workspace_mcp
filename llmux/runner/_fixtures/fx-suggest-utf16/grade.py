"""Grade fx-suggest-utf16: one pending suggestion that fixes 'colour'.

The document leads with an astral-plane emoji, so a run that computes indexes
from Python code points instead of the UTF-16 indexes the tools hand back
lands its edit one position early -- which shows up here as a wrong final
text, not as a tool error.
"""

from __future__ import annotations

from typing import Any

DOCUMENT_ID = "fx-doc-utf16"
ORIGINAL = "Release 🎉 notes: the colour picker now remembers your last choice.\n"
EXPECTED_FINAL = "Release 🎉 notes: the color picker now remembers your last choice.\n"


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
    if doc.original_text() != ORIGINAL:
        failures.append(
            "the change was applied directly instead of being suggested: "
            f"base text is now {doc.original_text()!r}"
        )
    else:
        passed += 1

    checks += 1
    if len(doc.registry) != 1:
        failures.append(
            f"expected exactly 1 pending suggestion, found {len(doc.registry)}: "
            + ", ".join(sorted(doc.registry))
        )
    else:
        passed += 1

    checks += 1
    if doc.final_text() != EXPECTED_FINAL:
        failures.append(
            f"accepting the suggestions would give {doc.final_text()!r}, "
            f"expected {EXPECTED_FINAL!r} (check the UTF-16 index offsets)"
        )
    else:
        passed += 1

    checks += 1
    authors = {s.author for s in doc.registry.values()}
    if authors and authors != {backend.me}:
        failures.append(f"suggestion authored by {sorted(authors)}, expected {backend.me}")
    else:
        passed += 1

    return {
        "pass": not failures,
        "score": passed / checks,
        "failures": failures,
    }
