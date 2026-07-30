"""Grade fx-accept-reject: accept the typo fix, reject the date deletion."""

from __future__ import annotations

from typing import Any

DOCUMENT_ID = "fx-doc-typo"
EXPECTED_FINAL = "Our team shipped the feature on Friday.\nEveryone was relieved.\n"


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
    if doc.registry:
        failures.append(
            "pending suggestions remain, expected none: "
            + ", ".join(sorted(doc.registry))
        )
    else:
        passed += 1

    # The text checks only mean anything once nothing is pending: final_text()
    # is the accept-everything projection, so judging it mid-review would
    # report resolutions the run never made.
    resolved = not doc.registry
    text = doc.final_text()

    checks += 1
    if not resolved:
        failures.append("cannot judge the end text: suggestions are still pending")
    elif text != EXPECTED_FINAL:
        failures.append(f"final text is {text!r}, expected {EXPECTED_FINAL!r}")
    else:
        passed += 1

    checks += 1
    if resolved and "on Friday" not in text:
        failures.append(
            "the ship date was removed: the deletion suggestion was accepted "
            "when it should have been rejected"
        )
    elif resolved:
        passed += 1

    checks += 1
    if resolved and "teem" in text:
        failures.append(
            "the misspelling 'teem' survives: the spelling fix was rejected "
            "when it should have been accepted"
        )
    elif resolved:
        passed += 1

    return {
        "pass": not failures,
        "score": passed / checks,
        "failures": failures,
    }
