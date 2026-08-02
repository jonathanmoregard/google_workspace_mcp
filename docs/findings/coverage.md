# Coverage: what `analysis.py` can and cannot see

Measured against the **live Google Docs API** (enrolled Developer Preview
project, account `jonathan@klaffat.com`) on **2026-08-02**, branch
`probe/coverage`. Every payload fragment below is verbatim from a
`documents.get` response, not transcribed from documentation.

The question: `HANDOVER.md` §7 recorded, as a design decision rather than a
measured one,

> Not modelled by design (out of scope, `analysis.py`): row/column-level table
> structure suggestions, and paragraph-level style suggestions. Text
> suggestions inside table cells, and text-run style suggestions, *are*
> reported.

Nobody had checked what the API returns for these, or what the tools do when
they appear.

**Verdict up front: the "by design" claim did not survive contact with the
API.** One half of it was factually wrong in the tools' favour, the other half
was a real silent omission — a `suggestion_count: 0` returned about a document
with an open suggestion in it. That is the exact failure this package exists to
prevent, and calling it a scope decision made it invisible for a year.

---

## Q1. Paragraph-level style suggestions — **RESOLVED**

### What the API does

`updateParagraphStyle` in `writeControl.writeMode: SUGGEST` **succeeds** and
creates a real pending suggestion. HTTP 200, an `OPEN` thread, a card in the
Docs sidebar.

Whether `analysis.py` sees it depends entirely on whether the change happens to
touch a text run:

| request | payload containers | modelled? |
|---|---|---|
| `updateParagraphStyle` alignment `CENTER` | `suggestedParagraphStyleChanges` | **NO** |
| `updateParagraphStyle` `lineSpacing: 200` | `suggestedParagraphStyleChanges` | **NO** |
| `updateParagraphStyle` `indentStart: 36pt` | `suggestedParagraphStyleChanges` | **NO** |
| `updateParagraphStyle` `namedStyleType: HEADING_2` | `suggestedParagraphStyleChanges` **+** `suggestedTextStyleChanges` | yes — by accident |
| `createParagraphBullets` | `suggestedBulletChanges`, `suggestedParagraphStyleChanges` | **NO** |
| `updateTableRowStyle` `minRowHeight` | `suggestedTableRowStyleChanges` | **NO** |
| `updateTableCellStyle` `backgroundColor` | `suggestedTableCellStyleChanges` | **NO** |

`HEADING_2` came through only because applying a heading also changes the run's
font — the thing `analysis.py` actually reads is
`textRun.suggestedTextStyleChanges`, not the paragraph style. The note in
`HANDOVER.md` and in `analysis.py` was therefore right about the mechanism and
wrong about the consequence: this is not a bounded, understood exclusion, it is
"whatever Google happens to co-write onto a run".

### Raw evidence

`updateParagraphStyle` with `{"alignment": "CENTER"}`, `fields: "alignment"` —
the **paragraph** carries the change and **every run is untouched**:

```json
{
  "startIndex": 1,
  "endIndex": 17,
  "paragraph": {
    "suggestedParagraphStyleChanges": {
      "suggest.tcoca0i50pwq": {
        "paragraphStyle": { "alignment": "CENTER", "...": "..." },
        "paragraphStyleSuggestionState": { "alignmentSuggested": true, "...": "..." }
      }
    },
    "elements": [ { "textRun": { "content": "Alpha line one.\n", "textStyle": {} } } ]
  }
}
```

The same document's top-level `suggestions[]` — the card exists, it is `OPEN`,
and `summaryText` is the only place its kind is named anywhere in the payload:

```json
{
  "suggestionId": "suggest.tcoca0i50pwq",
  "headPost": {
    "postId": "AAACEt8dea4",
    "author": { "displayName": "Jonathan Moregård", "me": true, "user": "users/108544169371250993163" },
    "createTime": "2026-08-01T22:14:33.103Z"
  },
  "status": "OPEN",
  "summaryText": "Format: alignment"
}
```

`createParagraphBullets` (`summaryText: "Format list: add to list\nFormat:
indent first line, indent left (2 times)"`):

```json
"suggestedBulletChanges": {
  "suggest.eki7g29p99n3": {
    "bullet": { "listId": "kix.d30slwxvsowj", "textStyle": { "underline": false } },
    "bulletSuggestionState": { "listIdSuggested": true, "nestingLevelSuggested": true, "...": "..." }
  }
}
```

`updateTableRowStyle` (`summaryText: "Format row: minimum height"`) — on the
**`tableRow`**, which `analysis.py` does not walk at all:

```json
"suggestedTableRowStyleChanges": {
  "suggest.5z5ur56fmkyh": {
    "tableRowStyle": { "minRowHeight": { "magnitude": 40, "unit": "PT" } },
    "tableRowStyleSuggestionState": { "minRowHeightSuggested": true }
  }
}
```

`updateTableCellStyle` (`summaryText: "Format cell: background color (4
times)"`) — on the **`tableCell`**:

```json
"suggestedTableCellStyleChanges": {
  "suggest.tq1x4iil80es": {
    "tableCellStyle": { "backgroundColor": { "color": { "rgbColor": { "red": 0.9019608, "green": 0.9019608, "blue": 0.2 } } }, "...": "..." },
    "tableCellStyleSuggestionState": { "backgroundColorSuggested": true, "...": "..." }
  }
}
```

### What the tools did

Measured on a document whose ONLY pending suggestion was an alignment card:

- `list_document_suggestions` → `suggestion_count: 0`, `matched_count: 0`,
  `returned_count: 0`, `suggestions: []`. No notice, no flag, nothing.
- `get_doc_review_view` → `suggestion_ids: []` and a `body_text` containing no
  `{+…+}` / `{-…-}` marker anywhere.

On the realistic mixed document (one text insertion **and** one alignment
card), `suggestion_count` was `1`. Two pending suggestions, one reported, and
the response's own accounting block agreed with itself.

**Yes, it was dropped silently.** `suggestion_count` / `matched_count` /
`returned_count` are documented as the way an agent tells whether it has seen
everything, and all three were internally consistent and externally wrong. An
agent that had resolved the one card it was shown would have reported the
review complete.

### Bonus finding: the write path failed open on the same gap

`_PostWriteRead.pending_state` answered `id in self.records` — the **modelled**
set. Measured directly against a live payload with an OPEN alignment thread:

```
BEFORE any resolution: pending_state('suggest.4a4a7lhdexle') = False
  ^ the suggestion IS pending (API lists its thread, status 'OPEN')
```

So a `reject` of such a card that did **not** take effect would have been
verified as `still_pending: false` / `matches_expectation: true` — a confident
success claim on the one destructive path this package has, derived from a
COMPLETE read that was listing the contradicting evidence in the same payload.
This is the class `_ResolutionVerdict` was built to retire; it survived because
its input was silently narrower than the thing it was reasoning about.

**And it survived one more level up.** Widening `pending_state` left
`verification.pending_suggestion_count` / `pending_suggestion_ids` still built
from `read.records`, so `still_pending: true` could print beside
`pending_suggestion_ids: []` on a complete read. Fixed in the close-out round
— [`closeout-fixes.md`](closeout-fixes.md) §1.

---

## Q2. Table structure suggestions — **RESOLVED, and the claim was backwards**

Row and column insert/delete in SUGGEST mode **are already reported**, as
ordinary records with a real address and `in_table: true`. There was never
anything to be out of scope about.

`insertTableRow` puts `suggestedInsertionIds` on the **`tableRow`** — which the
note predicted, and which `analysis.py` indeed does not read:

```json
{
  "startIndex": 60,
  "endIndex": 65,
  "suggestedInsertionIds": ["suggest.pzgvy3qfzt3"],
  "tableRowStyle": { "minRowHeight": { "unit": "PT" } }
}
```

But it *also* puts the same id on the new row's cell text runs, which
`analysis.py` does read:

```json
{
  "startIndex": 62,
  "endIndex": 63,
  "textRun": {
    "content": "\n",
    "suggestedInsertionIds": ["suggest.pzgvy3qfzt3"],
    "suggestedTextStyleChanges": { "suggest.pzgvy3qfzt3": { "...": "..." } }
  }
}
```

`deleteTableRow` and `deleteTableColumn` likewise mark the cells:

```json
{ "startIndex": 57, "endIndex": 58,
  "textRun": { "content": "\n", "suggestedDeletionIds": ["suggest.gxo0v4z18uok"] } }
```

Measured tool output (`fields="full"`):

| request | `summaryText` | record `type` | `in_table` | counted? |
|---|---|---|---|---|
| `insertTableRow` | `Add row` | `mixed` | `true` | yes |
| `insertTableColumn` | `Add column` | `mixed` | `true` | yes |
| `deleteTableRow` | `Delete row` | `deletion` | `true` | yes |
| `deleteTableColumn` | `Delete column` | `deletion` | `true` | yes |

`mixed` rather than `insertion` because prod writes a
`suggestedTextStyleChanges` (baselineOffset) onto the new cells' runs alongside
the insertion mark. That is asserted in the e2e test rather than smoothed over.

Also observed: an insert and a delete on the same row, by the same author, in
separate batches, **merge** into one card with
`summaryText: "Delete row\nAdd row"` — consistent with the same-author merge
already recorded in `HANDOVER.md` §4.5.

---

## Q3. The honest verdict — **RESOLVED**

**"Out of scope, by design" does not survive.** It was two claims and both were
wrong:

1. *Table structure is not modelled* — false. It always was.
2. *Paragraph style is not modelled* — true, and not a scope decision but a
   silent omission, which additionally extends to bullets and to table row and
   cell style, neither of which the note mentions.

A scope decision that is stated in the response is a scope decision. One that
is stated only in a source docstring, while the response returns
`suggestion_count: 0`, is a bug. The repo's central claim is that no response
asserts more than its evidence supports and that nothing is silently truncated;
a card that exists in the document and appears in **no** count breaks that
claim outright.

### The fix, and why this one

The cheapest correct fix is to **count what is not modelled and say so**, not
to model it. Modelling a paragraph-style delta or a row insert properly means
an address, a pre/post projection and a resolution check per kind — a large
feature, and the wrong one, because almost nothing a reviewer decides needs the
delta. What the reviewer needs is to know the card is there.

The API already says so, in a field this package already parses: the preview
read's top-level `suggestions[]` is the API's OWN inventory of the document's
suggestions. So the honest answer costs a set subtraction.

Both read tools — and, since the close-out round
([`closeout-fixes.md`](closeout-fixes.md) §1), both write tools' `verification`
blocks — now report:

```json
"unreported_suggestion_count": 1,
"unreported_suggestions": [
  { "suggestion_id": "suggest.tcoca0i50pwq",
    "summary_text": "Format: alignment",
    "author": "Jonathan Moregård",
    "status": "OPEN" }
],
"notice_unreported": "1 pending suggestion(s) in this document are NOT counted by any other suggestion count in this response … Do NOT report a review as complete on this response's other suggestion counts alone while `unreported_suggestion_count` is non-zero."
```

The ids are **actionable**: `manage_document_suggestion` accepts or rejects by
id regardless of whether this package can describe the content. Only the text,
address and before/after are unavailable, and the notice says exactly that.

`suggestion_count` keeps its old meaning (the cards this tool models), because
narrowing/pagination arithmetic is defined against it and redefining it would
silently change every existing caller's reading. The new number sits beside it
and the docstrings now say a review is finished when **both** are zero.

### The load-bearing new API fact this rests on

**The thread array is not the pending set.** Rejecting a suggestion leaves its
thread in `suggestions[]` with `status: "REJECTED"` while every content mark
disappears:

```
[after suggest]   threads={'suggest.h3j8vjj2yao6': ('Format: alignment', 'OPEN')}
                  paragraph suggestedParagraphStyleChanges: [(1, ['suggest.h3j8vjj2yao6'])]
[after reject #1] threads={'suggest.h3j8vjj2yao6': ('Format: alignment', 'REJECTED')}
                  paragraph suggestedParagraphStyleChanges: []
```

So the subtraction must filter resolved threads, or the count would grow
monotonically with the document's review history and never return to zero. The
filter is **negative** (`ACCEPTED` / `REJECTED` are resolved; anything else,
including a status this code does not recognise, is reported) because
over-reporting is recoverable — every record carries its raw `status` — and
under-reporting is the failure being fixed.

### Where the count is refused rather than answered

`0` is an absence claim, and the thread array exists only on the Developer
Preview read. On any degraded read the response carries
`unreported_suggestion_count: null` +
`unreported_suggestions_unavailable: "read_degraded"` + a notice, following the
same rule as `author`, `status`, `tab_id` and `still_pending`.

That includes a case worth recording on its own: **both `PREVIEW_*` view modes
always degrade.** `documents.get` with
`suggestionsViewMode=PREVIEW_SUGGESTIONS_ACCEPTED` (or
`PREVIEW_WITHOUT_SUGGESTIONS`) **and** `commentsViewMode` is a 400:

```json
{ "error": { "code": 400,
             "message": "Comments may not be requested when previewing suggestions.",
             "status": "INVALID_ARGUMENT" } }
```

`preview_read` always sets `commentsViewMode`, so
`get_doc_review_view(view_mode="PREVIEW_SUGGESTIONS_ACCEPTED")` has always
fallen back to the GA read and reported `read_source: "ga_documents_get"` with
its degraded notice. That is honest but was undocumented; it is now asserted in
e2e and named in the tool docstring.

---

## What changed in the repo

| file | change |
|---|---|
| `gdocs_preview/preview_read.py` | `RESOLVED_THREAD_STATUSES`, `thread_is_pending()`, `pending_thread_ids()` — the API's own pending inventory, with the negative status test and its rationale |
| `gdocs_preview/review_page.py` | `unreported_suggestions()`, `unreported_notice()`, `attach_unreported()` + the `read_degraded` unavailable path |
| `gdocs_preview/curated_tools.py` | both read tools attach the block (against the WHOLE modelled set, never the page or the window); docstrings corrected |
| `gdocs_preview/write_tools.py` | `_PostWriteRead.pending_thread_ids`; `pending_state` asks both sets, closing the reject fail-open |
| `gdocs_preview/analysis.py` | the "known limitations" docstring replaced with what was measured |
| `tests/gdocs_preview/fixtures.py` | `paragraph(para_styles=…)`, `DOC_PARAGRAPH_STYLE_ONLY`, `DOC_TEXT_PLUS_PARAGRAPH_STYLE`, `PARAGRAPH_STYLE_THREAD{,_REJECTED}` |
| `tests/gdocs_preview/test_preview_read.py` | `TestPendingThreadIds` (6) |
| `tests/gdocs_preview/test_review_page.py` | `TestUnreportedSuggestions`, `TestAttachUnreported` (7) |
| `tests/gdocs_preview/test_curated_tools.py` | `TestSuggestionsThisLayerDoesNotModel` (10) |
| `tests/gdocs_preview/test_write_tools.py` | `TestASuggestionWithNoContentMarkIsStillPending` (5) |
| `e2e/test_suggestion_coverage.py` | **new**, 8 tests, marker `e2e_preview` |

`uv run pytest tests/ -q` → 2421 passed, 3 skipped (baseline 2391/3).
`uv run ruff check .` and `uv run ruff format --check .` clean.

The e2e file builds its fixture state with a harness-side Docs client, for a
reason that is itself a finding: **no tool on this MCP surface can create a
paragraph-style suggestion.** `suggest_doc_edit` writes text only, and
upstream's `batch_update_doc` / `update_paragraph_style` write in EDIT mode with
no `writeControl`. An agent using this server can now *see* and *resolve* these
cards but cannot *make* one.

---

## Still unknown, and why

- **Whether `ACCEPTED` is the accepted-thread status string.** `REJECTED` was
  measured directly; `ACCEPTED` is inferred by symmetry and is in
  `RESOLVED_THREAD_STATUSES` on that basis. If Google uses another spelling the
  count over-reports (an accepted card would keep being listed) rather than
  under-reports, which is the safe direction, and the record's raw `status`
  makes it diagnosable. Cheap to close: accept a suggestion and re-read.
- **Whether a thread can be `OPEN` while its content marks were
  garbage-collected** (`HANDOVER.md` §4.5 says the thread is collected with the
  suggestion). If it can, that card would be counted as unreported forever. Not
  observed in any probe here.
- **Multi-tab attribution.** The `suggestions[]` array carries no tab id
  (`docs/preview-api-reference.md` open item 4), so `unreported_suggestion_count`
  is document-wide and cannot be narrowed by `tab_id`. It is deliberately not
  narrowed by any filter or window, so this costs nothing today — but it means
  the count cannot become per-tab without new API information.
- **Whether more unmodelled kinds exist.** Seven request types were measured.
  `updateTableColumnProperties` is already recorded as SUGGEST-unsupported;
  positioned objects, inline object properties, section styles and named ranges
  were not probed. The fix does not depend on enumerating them — the
  subtraction catches any kind, present or future — but the docstring's list of
  examples is not a closed set and says so.
- **The full e2e suite cannot currently be run in one go.** `uv run pytest e2e`
  exhausts the Docs API's 60-writes-per-minute-per-user quota and fails with
  429s. This is **pre-existing**: a baseline run with
  `--ignore=e2e/test_suggestion_coverage.py` failed the same way (4 failed, 7
  errors), and the account was simultaneously in use by other agents on sibling
  worktrees. `e2e/test_suggestion_coverage.py` passes 8/8 in isolation (~38 s)
  and was consolidated onto module-scoped shared documents (5 docs, ~19 writes)
  to add as little pressure as possible. Nothing here diagnoses or fixes the
  suite-wide quota problem; it is flagged for whoever owns the harness.
