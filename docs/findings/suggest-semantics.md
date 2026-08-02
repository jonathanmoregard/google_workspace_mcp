# SUGGEST-mode `batchUpdate` semantics — measured against the live API

Two questions this fork carried as **transcribed, never verified** since the
overlay was written. Both are now answered by the API rather than by a
documentation page.

| question | verdict |
|---|---|
| **Q1.** Are the 8 thread operations really SUGGEST-incompatible? | **RESOLVED** — split answer: the 8 *officially* unsupported request types are refused; the 8 *preview thread* operations are **not**, and excluding them was a bug. |
| **Q2.** Do SUGGEST-mode batches resolve indexes against the pre-batch document? | **RESOLVED** — **no.** They resolve *progressively*, exactly like EDIT mode. The premise of the old comment (that EDIT resolves against the pre-batch document) was itself wrong. |

**Provenance.** Measured 2026-08-01 against `docs.googleapis.com/v1` with the
enrolled Workspace Developer Preview credentials this repo already uses,
through throwaway probe scripts issuing raw `documents.batchUpdate` calls.
Every scratch document created was trashed. Payloads below are verbatim; only
document ids, revision ids and author identity are redacted (this is a public
repository) — request/response *shapes*, ids of the objects under test, and
error strings are untouched.

The findings are pinned as permanent tests in
[`e2e/test_suggest_semantics.py`](../../e2e/test_suggest_semantics.py)
(6 tests, marker `e2e_preview`) and
`tests/mockdocs/test_adapter_properties.py`.

> **Note on quota.** The Docs write quota is
> `WriteRequestsPerMinutePerUser` = **60**, and it counts *failed* writes.
> The first probe run hit it after 7 of 8 cases. Anything write-dense against
> this API needs backoff; `e2e/test_suggest_semantics.py::_execute` has it.

---

## Q1 — what a SUGGEST-mode batch refuses

### Q1a. Google's published 8: refused. RESOLVED.

All eight are refused, each with the **same specific message**, and each one
is accepted in EDIT mode *on the same document, in the same state, in the very
next call* — which is what makes this evidence rather than noise.

Request (one per row, `writeControl: {"writeMode": "SUGGEST"}`) → HTTP 400,
`status: INVALID_ARGUMENT`:

| request sent | `error.message`, verbatim |
|---|---|
| `{"addDocumentTab": {"tabProperties": {"title": "SuggestProbeTab"}}}` | `Invalid requests[0].addDocumentTab: Request does not support application as suggestion.` |
| `{"createNamedRange": {"name": "probe_suggest_nr", "range": {"startIndex": 1, "endIndex": 6}}}` | `Invalid requests[0].createNamedRange: Request does not support application as suggestion.` |
| `{"deleteHeader": {"headerId": "kix.3spaldcrklio"}}` | `Invalid requests[0].deleteHeader: Request does not support application as suggestion.` |
| `{"deleteFooter": {"footerId": "kix.813pzrlh0y1e"}}` | `Invalid requests[0].deleteFooter: Request does not support application as suggestion.` |
| `{"deleteNamedRange": {"namedRangeId": "kix.8sp1vgg2odq5"}}` | `Invalid requests[0].deleteNamedRange: Request does not support application as suggestion.` |
| `{"deleteTab": {"tabId": "t.axe5rvusofmt"}}` | `Invalid requests[0].deleteTab: Request does not support application as suggestion.` |
| `{"updateDocumentTabProperties": {"tabProperties": {"tabId": "t.0", "title": "RenamedBySuggest"}, "fields": "title"}}` | `Invalid requests[0].updateDocumentTabProperties: Request does not support application as suggestion.` |
| `{"updateTableColumnProperties": {"tableStartLocation": {"index": 2}, "columnIndices": [0], "tableColumnProperties": {"widthType": "FIXED_WIDTH", "width": {"magnitude": 120, "unit": "PT"}}, "fields": "width,widthType"}}` | `Invalid requests[0].updateTableColumnProperties: Request does not support application as suggestion.` |

One full exchange, unabridged:

```jsonc
// POST https://docs.googleapis.com/v1/documents/<doc>:batchUpdate
{
  "requests": [{"deleteHeader": {"headerId": "kix.3spaldcrklio"}}],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```jsonc
// HTTP 400
{
  "error": {
    "code": 400,
    "message": "Invalid requests[0].deleteHeader: Request does not support application as suggestion.",
    "status": "INVALID_ARGUMENT"
  }
}
```

The same request, same document, same header, `"writeMode": "EDIT"`:

```jsonc
// HTTP 200
{
  "replies": [{}],
  "writeControl": {"requiredRevisionId": "<rev>"},
  "documentId": "<doc>",
  "suggestionResponses": [{}],
  "commentUpdateState": "NO_UPDATES_REQUESTED"
}
```

A **positive control** ran on each document immediately before the refusal —
`insertText` in `SUGGEST` mode, HTTP 200 with a `createdSuggestionIds` — so
"this type is refused" is distinguishable from "SUGGEST is unavailable here".

#### The refusal is checked *before* the request's own arguments

This fell out of a probe bug and is worth keeping. A
`updateTableColumnProperties` whose `tableStartLocation` was stale (computed
before a suggested insertion shifted it — see Q2) answered:

```
SUGGEST → 400 "Invalid requests[0].updateTableColumnProperties: Request does not support application as suggestion."
EDIT    → 400 "Invalid requests[0].updateTableColumnProperties: The provided table start location is invalid."
```

So the SUGGEST refusal is emitted for a request that is *also* malformed, and
a test asserting only "SUGGEST gives 400" would pass on a request that was
never valid. Hence the EDIT leg in the e2e test, and hence the assertion on
the message text rather than on the status alone. The corrected control
(`tableStartLocation` read after the suggested insertion) gives EDIT → 200,
which is the row in the table above.

### Q1b. The 8 preview thread operations: **not** refused. RESOLVED — the exclusion was wrong.

`mockdocs/adapter.py` and `docs/preview-api-reference.md` treated
`insertComment`, `addCommentReply`, `updateCommentPost`, `deleteComment`,
`deleteCommentReply`, `acceptSuggestion`, `rejectSuggestion` and
`deleteSuggestion` as SUGGEST-incompatible, "they act on threads, not
content, so SUGGEST write mode does not apply to them". **The API disagrees.
All eight run inside a `writeMode: SUGGEST` batch, return HTTP 200, and take
effect.**

`insertComment` in a SUGGEST batch produces an ordinary, fully-formed comment
thread — not a suggested one:

```jsonc
// POST .../documents/<doc>:batchUpdate
{
  "requests": [{"insertComment": {
      "content": "suggest-mode comment probe",
      "range": {"startIndex": 3, "endIndex": 7}}}],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```jsonc
// HTTP 200
{
  "replies": [{"insertComment": {"commentThread": {
      "commentId": "AAACBre27d0",
      "anchorId": "kix.tdmy05iw790b",
      "headPost": {
        "postId": "AAACBre27d0",
        "content": "suggest-mode comment probe",
        "contentHtml": "suggest-mode comment probe",
        "author": {"displayName": "<author>", "me": true, "user": "<user>"},
        "createTime": "2026-08-01T22:16:54.798Z",
        "updateTime": "2026-08-01T22:16:54.798Z",
        "commentAction": "NO_COMMENT_ACTION_CHANGE"
      },
      "status": "OPEN",
      "plainTextQuote": "0123"}}}],
  "documentId": "<doc>",
  "suggestionResponses": [{}],
  "commentUpdateState": "ALL_SAVED"
}
```

The rest, all in `writeMode: SUGGEST`, all HTTP 200:

| request | evidence it took effect |
|---|---|
| `{"addCommentReply": {"commentId": "AAACBre27dk", "post": {"content": "suggest reply"}}}` | `replies[0].addCommentReply.post` with `postId`, `author`, `createTime`; `commentUpdateState: ALL_SAVED` |
| `{"updateCommentPost": {"commentId": …, "postId": …, "content": "edited head post"}}` | `commentUpdateState: ALL_SAVED` |
| `{"acceptSuggestion": {"suggestionId": "suggest.7audcsx2lxew"}}` | `suggestionResponses: [{"acceptedSuggestionIds": ["suggest.7audcsx2lxew"]}]`; the suggestion's text is afterwards in the `PREVIEW_WITHOUT_SUGGESTIONS` base text |
| `{"rejectSuggestion": {"suggestionId": "suggest.puz9z1ncmxol"}}` | `suggestionResponses: [{"rejectedSuggestionIds": ["suggest.puz9z1ncmxol"]}]` |
| `{"deleteSuggestion": {"suggestionId": "suggest.hy24hmtxk0ou"}}` | `suggestionResponses: [{"deletedSuggestionIds": ["suggest.hy24hmtxk0ou"]}]`, and the follow-up in **EDIT** mode → `404 "Suggestion with ID suggest.hy24hmtxk0ou does not exist."` |
| `{"deleteCommentReply": {"commentId": …, "postId": "AAACBre27eE"}}` | `ALL_SAVED`, and the follow-up in EDIT mode → `404 "Reply with ID AAACBre27eE does not exist."` |
| `{"deleteComment": {"commentId": "AAACBre27dk"}}` | `ALL_SAVED`, and the follow-up in EDIT mode → `404 "Comment with ID AAACBre27dk does not exist."` |

The three 404s are the load-bearing part: they prove the SUGGEST-mode call
*removed the object*, not merely that it returned 200.

And the mixed batch — a content edit and a thread operation together, which
the old model made unrepresentable:

```jsonc
{
  "requests": [
    {"insertText": {"location": {"index": 1}, "text": "MIX"}},
    {"insertComment": {"content": "mixed-batch comment",
                       "range": {"startIndex": 2, "endIndex": 5}}}
  ],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```jsonc
// HTTP 200 — 1:1 with the requests, both halves reported
{
  "replies": [{}, {"insertComment": {"commentThread": {"commentId": "AAACBre27fA", …}}}],
  "suggestionResponses": [{"createdSuggestionIds": ["suggest.y2th7z6nmkh5"]}, {}],
  "commentUpdateState": "ALL_SAVED"
}
```

The comment anchors to `plainTextQuote: "IXA"` — i.e. its range was resolved
*after* the suggested `MIX` insertion in the same batch, which is Q2's answer
showing up in the anchor.

### What changed in the repo (Q1)

- **`mockdocs/adapter.py`** — `SUGGEST_UNSUPPORTED` is now
  `SUGGEST_UNSUPPORTED_OFFICIAL` alone; the `| PREVIEW_REQUEST_TYPES` term is
  gone, with the measurement recorded at the constant. **This was a real
  bug**: the mock rejected batches production accepts, so no `llmux` scenario
  and no mock-backed test could ever exercise a comment written alongside a
  suggested edit. It failed closed (a capability made unreachable) rather than
  open, which is why nothing caught it.
- **`mockdocs/adapter.py`** — the refusal message is now prod's verbatim
  string (`SUGGEST_UNSUPPORTED_MESSAGE`) instead of a paraphrase
  (*"request type X is not supported in SUGGEST write mode"*). The mock's
  errors feed `preview_status.classify_preview_error`, so the wording is
  contract.
- **`tests/mockdocs/test_adapter_properties.py`** —
  `test_suggest_mode_rejects_unsupported_request_types` asserted the wrong
  set; it now covers only the official 8 and asserts the message. Two tests
  added: the thread ops running inside a SUGGEST batch, and Q2's index
  resolution.
- **`e2e/test_suggest_semantics.py`** — new; 3 tests for Q1.

**Not changed, deliberately** (`HANDOVER.md` and
`docs/preview-api-reference.md` are consolidated by the orchestrator). These
now-false statements need retiring:

- `docs/preview-api-reference.md:163` — the whole *"Additional exclusions
  (overlay decision, unverified)"* section is wrong and should say so.
- `docs/preview-api-reference.md:427` — open UNCERTAIN item **5** is
  resolved; the official 8 stand, the additional 8 do not.
- `HANDOVER.md:816` and `HANDOVER.md:833` — same item.
- `HANDOVER.md:565` — "Eight request types are unsupported in SUGGEST mode"
  is correct as written (it lists the official 8 only); no change needed.

### What is still unknown (Q1), and why

- **Whether `updateDocumentStyle`'s partial support is as documented** —
  `docs/preview-api-reference.md` records that SUGGEST mode does not support
  its `documentFormat`, `useEvenPageHeaderFooter` and
  `useFirstPageHeaderFooter` fields. Not probed: out of scope for the two
  questions asked, and no tool in this repo emits `updateDocumentStyle`.
- **Whether the refusal wording is stable across the other ~24 GA request
  types.** Eight were measured, all identical. The message is presumably
  generated from one code path, but that is inference, not measurement.
- **Whether a `deleteComment` by a non-author behaves differently in SUGGEST
  mode.** Every probe ran as the sole author of everything it touched, so all
  the permission rules in `docs/preview-api-reference.md` §"Permission rules"
  are still untested in either mode. A second account is needed.

---

## Q2 — how a SUGGEST-mode batch resolves indexes

### Verdict: progressive, in the `SUGGESTIONS_INLINE` space. RESOLVED.

The old comment in `write_tools.py` said:

> EDIT-mode batches resolve indexes against the pre-batch document; whether
> SUGGEST-mode shares that semantics is transcribed-not-verified.

**Both halves are wrong.** EDIT-mode batches do *not* resolve against the
pre-batch document, and SUGGEST-mode batches behave the same way EDIT ones
do. Each request is addressed against the document as the preceding requests
in the same batch left it.

#### The discriminating batch

Seed body `"0123456789"` (index 1 = `'0'`). Two requests, one batch:

```jsonc
{
  "requests": [
    {"insertText": {"location": {"index": 1}, "text": "AAAA"}},
    {"insertText": {"location": {"index": 5}, "text": "B"}}
  ],
  "writeControl": {"writeMode": "SUGGEST"}   // and again with "EDIT"
}
```

The two interpretations predict different documents:

| interpretation | index 5 means | resulting text |
|---|---|---|
| **progressive** — request 1 sees `AAAA0123456789` | the `'0'` just after `AAAA` | `AAAAB0123456789` |
| **pre-batch** — request 1 sees the seed | the seed's `'4'` | `AAAA0123B456789` |

Measured (`documents.get`, `suggestionsViewMode=SUGGESTIONS_INLINE`):

| mode | `SUGGESTIONS_INLINE` | `PREVIEW_WITHOUT_SUGGESTIONS` |
|---|---|---|
| `SUGGEST` | `"AAAAB0123456789\n"` | `"0123456789\n"` |
| `EDIT` | `"AAAAB0123456789\n"` | `"AAAAB0123456789\n"` |

**Progressive, and the two modes agree.** The batch response also shows the
two insertions merging into one suggestion, as §4.5 of `HANDOVER.md`
describes:

```jsonc
"suggestionResponses": [
  {"createdSuggestionIds": ["suggest.e30zwe2wff2u"]},
  {"updatedSummarySuggestionIds": ["suggest.e30zwe2wff2u"]}
]
```

#### Where the modes part company — and it is not about *when*

A *suggested* deletion removes nothing from the inline space; the characters
stay, marked. So it shifts nothing, while an EDIT deletion shifts everything
after it. Same batch, same seed:

```jsonc
{
  "requests": [
    {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}},
    {"insertText": {"location": {"index": 6}, "text": "Z"}}
  ]
}
```

| mode | `SUGGESTIONS_INLINE` | why |
|---|---|---|
| `SUGGEST` | `"01234Z56789\n"` | `0123` is still there (struck), so index 6 is still `'5'` |
| `EDIT` | `"45678Z9\n"` | `0123` is gone, so index 6 is five characters earlier, in `456789` |

Both are *progressive*. They differ only in what request 0 did to the
document, which is the whole distinction between the two write modes.

### Consequence for `suggest_doc_edit`

`suggest_doc_edit`'s replacement path sends
`deleteContentRange[s, e)` then `insertText@s` in one SUGGEST batch. **It is
correct** — and the assumption it was justified by was not the reason.

```jsonc
// seed "0123456789", writeMode SUGGEST
[{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}},
 {"insertText": {"location": {"index": 1}, "text": "X"}}]
```
```
SUGGESTIONS_INLINE           → "X0123456789\n"
PREVIEW_WITHOUT_SUGGESTIONS  → "0123456789\n"
PREVIEW_SUGGESTIONS_ACCEPTED → "X456789\n"

suggestionResponses: [{"createdSuggestionIds": ["suggest.61pvxod7o0oh"]},
                      {"updatedSummarySuggestionIds": ["suggest.61pvxod7o0oh"]}]
```

The insertion lands at `start_index` because the suggested deletion did not
move it — not because indexes are resolved against a pre-batch snapshot.
Under the (false) pre-batch reading the outcome happens to be identical,
which is exactly why the wrong justification survived: it never produced a
wrong answer for the one batch shape the tool sends.

**Had the assumption been load-bearing, it would have been wrong.** The
concrete risk it was hiding: any *third* request appended to that batch, or
any future tool that sends two suggested insertions in one batch, must
compute its index against the inline document *including the earlier
insertion*. A caller reading "indexes resolve against the pre-batch document"
and building a multi-edit SUGGEST batch from one listing's indexes writes
every request after the first into the wrong place — silently, since every
index but 0 is valid somewhere.

### What changed in the repo (Q2)

- **`gdocs_preview/write_tools.py`** — the `UNCERTAIN (pending enrollment)`
  comment on the replacement path is replaced with the measured rule, the
  reason the shape is correct, and the corollary about appending a third
  request. No behaviour change: the code was already right.
- **`e2e/test_suggest_semantics.py`** — 3 tests: the two-insert
  discriminator in both modes, the deletion-shift difference between the
  modes, and the replacement shape's three view-mode texts.
- **`tests/mockdocs/test_adapter_properties.py`** — pins the same two
  scenarios against `mockdocs`, which already behaved this way (it dispatches
  in order against the mutating model, and a suggested delete only marks).
  Mock and prod now provably agree on this.

**Not changed** (orchestrator-owned): `HANDOVER.md:820`, the bullet
"Whether SUGGEST-mode batches resolve indexes against the pre-batch document
… transcribed, not verified", is resolved — and should also correct the claim
about *EDIT* mode embedded in it.

### What is still unknown (Q2), and why

- **Multi-segment and multi-tab batches.** Every Q2 probe ran in the body of
  a single-tab document. Whether a request naming a header/footnote segment
  is shifted by an earlier request in a *different* segment was not measured
  (it should not be — segments are numbered independently, §4.1 — but that is
  reasoning, not evidence).
- **Whether `requiredRevisionId` / `targetRevisionId` change the answer.**
  No probe set either. The repo never sends them.
- **The interaction with same-author merging.** The two-insert batch merged
  into one suggestion, and the merge happened *after* both indexes had
  resolved. Whether a merge can occur mid-batch and shift a later request's
  index is not established; the observed batch cannot distinguish it, because
  a merge of adjacent runs does not change the text or its length.
- **Ordering under `replaceAllText`**, whose "index" is a search, not a
  number. Not probed.

---

## Reproducing

```bash
uv run pytest e2e/test_suggest_semantics.py -q      # 6 passed, ~70 s
```

Requires the enrolled credentials (`e2e/README.md`); skips loudly with the
capabilities probe's classification otherwise.

**Run this module on its own, and give the quota a minute either side.** It
issues ~60 Docs write requests, and `WriteRequestsPerMinutePerUser` is 60.
`_execute` backs off on 429 (5 s doubling to 40 s, 6 attempts), which is
enough when the module runs alone and not enough when the rest of the suite
is saturating the same bucket.

### A pre-existing problem this work surfaced, and did not cause

`uv run pytest e2e -m e2e_preview` **already fails on write quota without
this module**, on the credentials in use. Measured 2026-08-02, back to back,
same session:

| invocation | result |
|---|---|
| `pytest e2e -m e2e_preview --ignore=e2e/test_suggest_semantics.py` | **12 passed, 10 failed** (22 tests, 138 s) |
| the same, after a 3-minute cool-down | **3 passed, 19 failed/errored** (62 s) |
| `pytest e2e -m e2e_preview` (with this module) | 13 passed, 15 failed (28 tests) |
| `pytest e2e/test_suggest_semantics.py` alone | **6 passed** (70 s) |

Every failure is the same shape — `HttpError 429 … 'quota_limit':
'WriteRequestsPerMinutePerUser', 'quota_limit_value': '60'` — usually
surfacing through `create_doc` in a fixture, so it lands as a fixture
**error** rather than a test failure and reads like a broken test.

Two things follow, neither of them in scope here and both worth a decision:

1. The e2e preview suite has no quota handling anywhere (`e2e/conftest.py`'s
   `create_doc_via_mcp`, `ServerSession.call_tool`, and the tools themselves
   all propagate 429 immediately). A green `-m e2e_preview` run is currently
   a matter of how recently the quota was used.
2. Adding this module makes a saturated run worse, because it is write-dense
   by nature: proving a request type is refused costs two writes, and there
   are eight of them. If the suite is ever made quota-safe, the cheap lever
   here is a shared session-scoped fixture doc for the Q2 scenarios.
