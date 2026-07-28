"""Synthetic Google Docs Document JSON fixtures for curated-layer tests.

All fixtures emulate ``documents.get`` payloads in SUGGESTIONS_INLINE view
mode: suggested insertions are present in the text (tagged with
``suggestedInsertionIds``) and suggested deletions are still present (tagged
with ``suggestedDeletionIds``).

Index discipline: the Docs API counts indexes in UTF-16 code units, NOT
Python code points. The builders below compute startIndex/endIndex with
``utf16_len`` so fixtures containing astral-plane characters (emoji) have
API-faithful indexes that differ from Python ``len()`` arithmetic.
"""

from __future__ import annotations

from typing import Any, Optional


def utf16_len(s: str) -> int:
    """Length of ``s`` in UTF-16 code units (the Docs API index unit)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def run(
    text: str,
    ins: Optional[list[str]] = None,
    dels: Optional[list[str]] = None,
    styles: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Spec for a single TextRun inside a paragraph."""
    return {"text": text, "ins": ins or [], "dels": dels or [], "styles": styles or []}


def paragraph(*runs: dict[str, Any], bullet: bool = False) -> dict[str, Any]:
    """Spec for a paragraph. The caller includes the trailing newline in the
    final run's text, mirroring real Docs payloads."""
    return {"kind": "paragraph", "runs": list(runs), "bullet": bullet}


def table(rows: list[list[list[dict[str, Any]]]]) -> dict[str, Any]:
    """Spec for a table: rows -> cells -> list of paragraph specs."""
    return {"kind": "table", "rows": rows}


def _build_text_run(spec: dict[str, Any], index: int) -> tuple[dict[str, Any], int]:
    end = index + utf16_len(spec["text"])
    text_run: dict[str, Any] = {"content": spec["text"], "textStyle": {}}
    if spec["ins"]:
        text_run["suggestedInsertionIds"] = list(spec["ins"])
    if spec["dels"]:
        text_run["suggestedDeletionIds"] = list(spec["dels"])
    if spec["styles"]:
        text_run["suggestedTextStyleChanges"] = {
            sid: {"textStyle": {"bold": True}} for sid in spec["styles"]
        }
    element = {"startIndex": index, "endIndex": end, "textRun": text_run}
    return element, end


def _build_paragraph(spec: dict[str, Any], index: int) -> tuple[dict[str, Any], int]:
    elements = []
    for run_spec in spec["runs"]:
        element, index = _build_text_run(run_spec, index)
        elements.append(element)
    para: dict[str, Any] = {
        "elements": elements,
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
    }
    if spec.get("bullet"):
        para["bullet"] = {"listId": "kix.list0", "nestingLevel": 0}
    start = elements[0]["startIndex"] if elements else index
    structural = {
        "startIndex": start,
        "endIndex": index,
        "paragraph": para,
    }
    return structural, index


def _build_table(spec: dict[str, Any], index: int) -> tuple[dict[str, Any], int]:
    table_start = index
    index += 1  # table start marker
    built_rows = []
    for row in spec["rows"]:
        index += 1  # row start marker
        built_cells = []
        for cell in row:
            cell_start = index
            index += 1  # cell start marker
            content = []
            for para_spec in cell:
                structural, index = _build_paragraph(para_spec, index)
                content.append(structural)
            built_cells.append(
                {"startIndex": cell_start, "endIndex": index, "content": content}
            )
        built_rows.append({"tableCells": built_cells})
    structural = {
        "startIndex": table_start,
        "endIndex": index,
        "table": {
            "rows": len(built_rows),
            "columns": max((len(r) for r in spec["rows"]), default=0),
            "tableRows": built_rows,
        },
    }
    return structural, index


def build_segment(
    specs: list[dict[str, Any]], start: int = 1, section_break: bool = True
) -> list[dict[str, Any]]:
    """Build a segment's ``content`` array from paragraph/table specs."""
    content: list[dict[str, Any]] = []
    index = start
    if section_break:
        # Real body payloads start with a sectionBreak that has NO startIndex.
        content.append({"endIndex": start, "sectionBreak": {"sectionStyle": {}}})
    for spec in specs:
        if spec["kind"] == "paragraph":
            structural, index = _build_paragraph(spec, index)
        elif spec["kind"] == "table":
            structural, index = _build_table(spec, index)
        else:  # pragma: no cover - fixture authoring error
            raise ValueError(f"unknown spec kind: {spec['kind']}")
        content.append(structural)
    return content


def build_doc(
    body_specs: list[dict[str, Any]],
    headers: Optional[dict[str, list[dict[str, Any]]]] = None,
    footers: Optional[dict[str, list[dict[str, Any]]]] = None,
    footnotes: Optional[dict[str, list[dict[str, Any]]]] = None,
    suggestion_threads: Optional[list[dict[str, Any]]] = None,
    title: str = "Fixture Doc",
    document_id: str = "doc-fixture-1",
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "documentId": document_id,
        "title": title,
        "body": {"content": build_segment(body_specs)},
    }
    for field, segments in (
        ("headers", headers),
        ("footers", footers),
        ("footnotes", footnotes),
    ):
        if segments:
            doc[field] = {
                seg_id: {"content": build_segment(specs, start=0, section_break=False)}
                for seg_id, specs in segments.items()
            }
    if suggestion_threads is not None:
        doc["suggestionThreads"] = suggestion_threads
    return doc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# 1. Plain insertion: "Hello world.\n" with " brave" suggested-inserted.
DOC_PLAIN_INSERTION = build_doc(
    [
        paragraph(
            run("Hello"),
            run(" brave", ins=["suggest.ins1"]),
            run(" world.\n"),
        )
    ]
)

# 2. Plain deletion: "Hello cruel world.\n" with " cruel" suggested-deleted.
DOC_PLAIN_DELETION = build_doc(
    [
        paragraph(
            run("Hello"),
            run(" cruel", dels=["suggest.del1"]),
            run(" world.\n"),
        )
    ]
)

# 3. Replacement: one suggestion ID carrying both a deletion and an insertion
#    ("morning" -> "evening").
DOC_REPLACEMENT = build_doc(
    [
        paragraph(
            run("Good "),
            run("morning", dels=["suggest.rep1"]),
            run("evening", ins=["suggest.rep1"]),
            run("\n"),
        )
    ]
)

# 4. Multi-run suggestion: one ID spanning non-adjacent runs with an
#    untouched base run sandwiched between them.
DOC_MULTI_RUN = build_doc(
    [
        paragraph(
            run("Start "),
            run("alpha", ins=["suggest.multi1"]),
            run("-mid-"),
            run("omega", ins=["suggest.multi1"]),
            run(" end.\n"),
        )
    ]
)

# 5. Table-nested suggestion.
DOC_TABLE = build_doc(
    [
        paragraph(run("Intro.\n")),
        table(
            [
                [
                    [paragraph(run("Cell A\n"))],
                    [
                        paragraph(
                            run("Cell "),
                            run("B-extra", ins=["suggest.tab1"]),
                            run("\n"),
                        )
                    ],
                ]
            ]
        ),
    ]
)

# 6. Empty document: section break + single empty paragraph, no suggestions.
DOC_EMPTY = build_doc([paragraph(run("\n"))])

# 7. Emoji / UTF-16: astral-plane chars before and inside the suggestion.
#    "\U0001F600\U0001F600 " is 5 UTF-16 units; para starts at index 1, so
#    the insertion run must start at index 6 (not 4 as code-point math says).
DOC_EMOJI = build_doc(
    [
        paragraph(
            run("\U0001f600\U0001f600 "),
            run("\U0001f389 party ", ins=["suggest.emoji1"]),
            run("time.\n"),
        )
    ]
)
EMOJI_SUGGESTION_START = 6
EMOJI_SUGGESTION_END = 6 + utf16_len("\U0001f389 party ")  # 6 + 9 = 15

# 8. Suggestion at the very start of the body.
DOC_AT_START = build_doc(
    [
        paragraph(
            run("New: ", ins=["suggest.start1"]),
            run("content.\n"),
        )
    ]
)

# 9. Suggestion at the very end of the body (deletion just before the final
#    newline of the last paragraph).
DOC_AT_END = build_doc(
    [
        paragraph(
            run("Keep this"),
            run(" not this", dels=["suggest.end1"]),
            run("\n"),
        )
    ]
)

# 10. Two nearby suggestions: contexts are computed on BASE text (all
#     insertions stripped), so sug A's context must not leak sug B's
#     inserted text.
DOC_NEIGHBOURS = build_doc(
    [
        paragraph(
            run("one "),
            run("INSERTED-B ", ins=["suggest.nb"]),
            run("two "),
            run("three", dels=["suggest.na"]),
            run(" four.\n"),
        )
    ]
)

# 11. Suggestion inside a header segment.
DOC_HEADER = build_doc(
    [paragraph(run("Body text.\n"))],
    headers={
        "kix.h1": [
            paragraph(
                run("Header"),
                run(" updated", ins=["suggest.hdr1"]),
                run("\n"),
            )
        ]
    },
)

# 12. Style-only suggestion.
DOC_STYLE = build_doc(
    [
        paragraph(
            run("Plain "),
            run("styled", styles=["suggest.sty1"]),
            run(" text.\n"),
        )
    ]
)

# 13. Mixed suggestion: same ID both inserts text and restyles another run.
DOC_MIXED = build_doc(
    [
        paragraph(
            run("Alpha "),
            run("beta", ins=["suggest.mix1"]),
            run(" gamma", styles=["suggest.mix1"]),
            run("\n"),
        )
    ]
)

# 14. Suggestion threads present (preview surface): author is exposed via
#     SuggestionThread.headPost.author (PostAuthor).
THREADS = [
    {
        "suggestionId": "suggest.ins1",
        "status": "OPEN",
        "headPost": {
            "postId": "post1",
            "author": {
                "displayName": "Alice Reviewer",
                "me": False,
                "user": "users/123",
            },
        },
        "replies": [],
    }
]
DOC_INSERTION_WITH_THREADS = build_doc(
    [
        paragraph(
            run("Hello"),
            run(" brave", ins=["suggest.ins1"]),
            run(" world.\n"),
        )
    ],
    suggestion_threads=THREADS,
)
