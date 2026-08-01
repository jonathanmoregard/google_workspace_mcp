# Findings: unanchored comments, the non-enrolled classifier, discovery

Three long-open questions, probed against the **real** Google Docs API on
2026-08-01 with the enrolled account `jonathan@klaffat.com` (GCP project
`498052759130`). Everything below is copied from live responses; nothing is
paraphrased, and where a claim is inference rather than observation it says so
in that sentence.

| # | question | verdict |
|---|---|---|
| Q1 | `insertComment` with `range` omitted | **RESOLVED** |
| Q2 | the non-enrolled error classifier, validated without a second project | **PARTIAL** |
| Q3 | discovery type names for the two `status` enums | **PARTIAL** (names BLOCKED, value sets RESOLVED) |

Encoded as permanent tests in
[`e2e/test_comment_and_error_shapes.py`](../../e2e/test_comment_and_error_shapes.py)
(18 tests: 10 `e2e_ga`, 8 `e2e_preview`) and
`tests/gdocs_preview/test_curated_tools.py::TestClassifyAgainstLiveErrorStrings`
(21 unit tests, no network).

---

## Q1 — `InsertCommentRequest` with `range` omitted — **RESOLVED**

### Verdict

The API **refuses** it. `create_anchored_doc_comment`'s mandatory
`start_index` / `end_index` are not a self-imposed restriction:
**do not relax the signature.** The docstring's redirect to
`manage_document_comment` action `create` (Drive v3) is the correct and only
unanchored path, and it costs the caller nothing on the read side.

### Raw evidence

Sent to `POST https://docs.googleapis.com/v1/documents/{id}:batchUpdate`,
document seeded with `"Say the brave word today.\n"`:

```jsonc
// request
{"requests": [{"insertComment": {"content": "UNANCHORED-A"}}]}
// HTTP 400
{"error": {"code": 400,
           "message": "Invalid requests[0].insertComment: Insert comment requests must specify a range to anchor to.",
           "status": "INVALID_ARGUMENT"}}
```

```jsonc
// request
{"requests": [{"insertComment": {"content": "EMPTYRANGE-B", "range": {}}}]}
// HTTP 400
{"error": {"code": 400,
           "message": "Invalid requests[0].insertComment: Invalid range: must contain a start and end index",
           "status": "INVALID_ARGUMENT"}}
```

```jsonc
// request -- a range that names a tab but no indexes
{"requests": [{"insertComment": {"content": "TABONLY-C", "range": {"tabId": "t.0"}}}]}
// HTTP 400 -- same message as the empty range
{"error": {"code": 400,
           "message": "Invalid requests[0].insertComment: Invalid range: must contain a start and end index",
           "status": "INVALID_ARGUMENT"}}
```

The anchored control in the same document succeeded and returned the
`CommentThread` documented in `docs/preview-api-reference.md`
(`commentId`, `anchorId: "kix.n1u28bctd2j1"`, `plainTextQuote: "Say"`,
`status: "OPEN"`, `commentUpdateState: "ALL_SAVED"`).

All three refusals are **semantic**, not parse failures — the message names
the camelCase request and follows with prose — so
`classify_preview_error(400, …)` reads them as
`("available", "preview_request_type_recognized")`, which is correct: the
preview request type was recognised.

### The Drive path, verified end to end

`drive.comments().create(fileId=…, body={"content": "DRIVE-UNANCHORED"})`
returns a comment with **no `anchor` key at all**, and the Docs preview read
then reports it as a first-class `CommentThread`:

```jsonc
// GET .../documents/{id}?suggestionsViewMode=SUGGESTIONS_INLINE
//        &commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED&includeTabsContent=true
// -> comments[0]
{"commentId": "AAACElr5hlc",
 "headPost": {"postId": "AAACElr5hlc",
              "content": "DRIVE-UNANCHORED",
              "contentHtml": "DRIVE-UNANCHORED",
              "author": {"displayName": "…", "me": true, "user": "users/1085…"},
              "createTime": "2026-08-01T22:19:38.968Z",
              "updateTime": "2026-08-01T22:19:38.968Z",
              "commentAction": "NO_COMMENT_ACTION_CHANGE"},
 "status": "OPEN"}
```

`anchorId` and `plainTextQuote` are **absent** (proto3 omits them), and
`tabs[0].documentTab.commentAnchors` is absent too — that absence *is* the
representation of "document-level". Every other field a review record needs —
id, head post, content, author, `status` — is present and identical in shape
to an anchored thread's.

### Recommendation

Keep the signature. Relaxing it would move a 400 from the tool's own
validation (which answers instantly and names the parameter) to a round trip
that answers with `Invalid requests[0].insertComment: …` and no mention of
which tool parameter to fix. The one thing worth doing — already true — is
that the docstring names the Drive alternative; this document now backs that
sentence with the evidence that the alternative produces a thread
`get_doc_review_view` can see.

### Still unknown

Nothing material. `insertComment` was not tested with a range whose
`startIndex == endIndex`; the tool rejects that before the API sees it.

---

## Q2 — the non-enrolled error classifier — **PARTIAL**

### What was and was not proved

**Proved.** `_UNKNOWN_FIELD_MARKERS` matches, verbatim and with room to
spare, every unknown-name rejection the live Docs API produces. The marker
strings are correct about the thing they describe: proto-parse failures.

**Not proved, and not provable here.** What a **non-enrolled project**
receives when it sends a *recognised-but-ungated* request type. That needs a
second GCP project, which is not available. If Google enforces enrollment
*semantically* — the field parses for everyone and the handler then refuses —
the message is unknown to this repo and the marker list may not match it.
This proxy validates the marker **strings** against real proto-parse errors.
It says nothing about the enrollment mechanism.

**The bridge between the two, which is inference and is labelled as such**
in `preview_status`'s docstring: every preview element this package puts on
the wire is a *field name*, and every one is absent from the public discovery
document (§Q3). If enrollment gates field **visibility** — the mechanism the
repo has always assumed — each lands in the covered grammar. Discovery
generation and the JSON transcoder are normally driven by the same visibility
labels, but that is how Google's API infrastructure generally works, not
something measured here.

### Raw evidence — the unknown-name grammar

Every one of these is HTTP 400 and every one carries **all three** markers.
Messages verbatim, `error.message` field:

| what was sent | `error.message` |
|---|---|
| `{"thisRequestTypeDoesNotExist": {}}` | `Invalid JSON payload received. Unknown name "thisRequestTypeDoesNotExist" at 'requests[0]': Cannot find field.` |
| `{"insertCommentThread": {"content": "x"}}` | `Invalid JSON payload received. Unknown name "insertCommentThread" at 'requests[0]': Cannot find field.` |
| `{"insertcomment": {"content": "x"}}` (wrong case) | `Invalid JSON payload received. Unknown name "insertcomment" at 'requests[0]': Cannot find field.` |
| `{"insertComment": {"content": "x", "bogusSubFieldXyz": "nope"}}` | `Invalid JSON payload received. Unknown name "bogusSubFieldXyz" at 'requests[0].insert_comment': Cannot find field.` |
| `{"insertComment": {…, "range": {…, "bogusRangeFieldXyz": 3}}}` | `Invalid JSON payload received. Unknown name "bogusRangeFieldXyz" at 'requests[0].insert_comment.range': Cannot find field.` |
| `{"insertText": {…, "bogusFieldXyz": 1}}` | `Invalid JSON payload received. Unknown name "bogusFieldXyz" at 'requests[0].insert_text': Cannot find field.` |
| body-level `{"bogusTopLevelFieldXyz": 1}` | `Invalid JSON payload received. Unknown name "bogusTopLevelFieldXyz": Cannot find field.` |
| `{"writeControl": {"bogusXyz": 1}}` | `Invalid JSON payload received. Unknown name "bogusXyz" at 'write_control': Cannot find field.` |

Notes worth keeping:

- **The path is snake_case, the name is echoed as sent.** `insertComment`
  appears in the path as `insert_comment` but the unknown name is quoted
  exactly as the caller wrote it.
- **Case matters.** `insertcomment` is an unknown name, not a case-insensitive
  match — so a non-enrolled caller and a typo are indistinguishable from the
  message alone.
- **The top-level variant has no `at '…'` clause.** Any marker requiring
  `at '` would miss it. The current markers do not.

One variant lives on the **read** path and drops the third marker:

```
GET .../documents/{id}?bogusQueryParamXyz=1
-> 400 Invalid JSON payload received. Unknown name "bogusQueryParamXyz":
   Cannot bind query parameter. Field 'bogusQueryParamXyz' could not be
   found in request message.
```

No `Cannot find field.` — caught by the other two. **The redundancy in the
marker list is load-bearing, not belt-and-braces.**

### The gap: a proto-parse family the markers MISS

There is a second parse-failure grammar. The field name resolves; the *value*
will not parse into its type. It carries **none** of the three markers:

| what was sent | `error.message` |
|---|---|
| `{"insertComment": "not-an-object"}` | `Invalid value at 'requests[0].insert_comment' (type.googleapis.com/google.apps.docs.v1.InsertCommentRequest), "not-an-object"` |
| `{"insertText": {"location": {"index": "abc"}, …}}` | `Invalid value at 'requests[0].insert_text.location.index' (TYPE_INT32), "abc"` |
| `writeControl.writeMode = "TOTALLY_BOGUS_WRITE_MODE"` | `Invalid value at 'write_control.write_mode' (type.googleapis.com/google.apps.docs.v1.WriteControl.WriteMode), "TOTALLY_BOGUS_WRITE_MODE"` |
| `paragraphStyle.namedStyleType = "BOGUS_STYLE_XYZ"` | `Invalid value at 'requests[0].update_paragraph_style.paragraph_style.named_style_type' (type.googleapis.com/google.apps.docs.v1.NamedStyleType), "BOGUS_STYLE_XYZ"` |
| two members of the request oneof | `Invalid value at 'requests[0]' (oneof), oneof field 'request' is already set. Cannot set 'deleteContentRange'` |

Before this round every one of them fell through the classifier's
`if status == 400: … return "available"` branch. **A request the API never
parsed was recorded as evidence that the preview surface is reachable** —
exactly the fail-open direction.

Is it reachable by a *non-enrolled* caller of this package? On the evidence,
**no**: every preview element the package sends is a field name (see the
table in §Q3), so field-visibility gating puts all of them in the
unknown-name grammar. But "on the evidence, no" is not "never", and the
honest verdict for an unparsed request is that it proves nothing either way —
an *enrolled* caller with a malformed value produces exactly these strings.

### The third grammar, and why the first two can be trusted

Semantic rejections — the request type parsed, its arguments were wrong — use
a grammar of their own: the **camelCase** request name, a colon, then prose.
Observed:

```
Invalid requests[0].insertComment: Insert comment requests must specify a range to anchor to.
Invalid requests[0].insertComment: Invalid range: must contain a start and end index
Invalid requests[0].insertComment: Index 900000 must be less than the end index of the referenced segment, 24.
Invalid requests[0]: No request set.
Invalid requests[0].updateParagraphStyle: Invalid field: bogus_mask_field_xyz
Comments view mode may only be specified if tabs content is also requested.
Suggestion with ID probe.nonexistent does not exist.          (HTTP 404)
```

Three disjoint grammars, and no semantic message contains `Invalid value at`
or any unknown-name marker. That disjointness is what the classifier rests
on, and it is now asserted over the recorded corpus rather than described in
prose (`test_the_two_parse_grammars_do_not_overlap_the_semantic_one`).

### What changed in the repo

`gdocs_preview/preview_status.py`:

- `_UNKNOWN_FIELD_MARKERS` is unchanged as a value; its comment now carries
  the verbatim observed strings, the query-parameter variant that justifies
  the redundancy, and an explicit statement of what the proxy does and does
  not prove.
- **New `_PARSE_FAILURE_MARKERS = ("invalid value at",)`**, checked on 400
  after the unknown-name markers, returning
  `("unknown", "request_not_parsed")`.

The verdict is `unknown`, not `unavailable`. `unavailable` would tell an
enrolled caller with a malformed value to go and enrol — a wrong instruction
delivered confidently — while `available` was the fail-open bug. `unknown` is
the verdict this module already gives a bare 404 and a 403, for the same
reason, and its docstring already says so: *"the honest answer, and the one
that costs a probe rather than a wrong belief."*

**Behaviour of the write path is unchanged.** `_execute_preview_batch_update`
special-cases only `("unavailable", "not_enrolled")`; `unknown` re-raises the
`HttpError` exactly as `available` did. Only the recorded verdict changes,
from a wrong "available" to an honest "unknown". The capabilities probe is
unaffected: its bogus-id `acceptSuggestion` answers 404 with
`Suggestion with ID … does not exist.`, re-observed on 2026-08-01.

One pre-existing unit test needed its fixture string swapped:
`test_one_callers_probe_is_not_another_callers_verdict` used
`"Invalid value at requests[0]"` as a stand-in for "some 400 that means
available". The string was always incidental to what that test asserts
(cross-tenant leakage); it is now a real semantic 400 from the live API, with
a comment saying why it changed.

### Still unknown, and why

1. **What a non-enrolled project actually receives.** Requires a second GCP
   project. Until then `("unavailable", "not_enrolled")` is a claim supported
   by the grammar plus the discovery-visibility inference, not by observation.
2. **Whether enrollment is per-project or per-account** (open UNCERTAIN item
   3). Untouched by this round.
3. **Whether a non-enrolled caller gets 403 rather than 400.** A 403 would
   classify `("unknown", "permission_or_scope")` — safe, but it would mean the
   `not_enrolled` branch never fires in production and nobody would notice.

---

## Q3 — discovery type names for the `status` enums — **PARTIAL**

### Verdict

The two type names are **BLOCKED** — not merely unfound. No reachable
discovery document contains the preview schemas at all, and the one channel
that *does* name preview proto types is structurally closed for these two
enums. The **value sets** are now RESOLVED by observation.

### Every discovery variant that was tried

All fetched twice, anonymously and with the enrolled account's OAuth token;
**the two were byte-identical in every case** (same `Content-Length`), so
enrollment buys nothing here.

| URL | result |
|---|---|
| `https://docs.googleapis.com/$discovery/rest?version=v1` | 200, 236,737 bytes, `revision: "20260727"`, 170 schemas |
| `https://www.googleapis.com/discovery/v1/apis/docs/v1/rest` | 200, byte-identical to the above |
| `…$discovery/rest?version=v1&labels=DEVELOPER_PREVIEW` | 200, **identical** — the label is ignored, not honoured |
| `…&labels=PREVIEW` | 200, identical |
| `…&labels=TRUSTED_TESTER` | 200, identical |
| `…&labels=LIMITED_AVAILABILITY` | 200, identical |
| `…$discovery/rest?version=v1preview` | 404 `Discovery document not found for API service: docs.googleapis.com format: rest version: v1preview` |
| `…?version=v1beta` | 404, same shape |
| `…?version=v1alpha` | 404, same shape |
| `https://www.googleapis.com/discovery/v1/apis?name=docs` | 200, 686 bytes, lists only `docs v1` |
| `https://type.googleapis.com/google.apps.docs.v1.CommentThread` | 404 (an HTML Google 404 — not a type resolver) |

In the 236,737-byte public document, occurrences of every preview token —
`insertComment`, `InsertCommentRequest`, `CommentThread`, `SuggestionThread`,
`commentsViewMode`, `CommentsViewMode`, `acceptSuggestion`,
`addCommentReply`, `PostAuthor`, `plainTextQuote`, `writeMode`,
`commentUpdateState` — is **zero**.

Concretely, the absences that matter (each asserted by
`test_every_preview_element_is_absent_from_the_public_discovery_document`):

| preview element | where it would be | public document has |
|---|---|---|
| `insertComment`, `acceptSuggestion`, `rejectSuggestion`, `addCommentReply` | `schemas.Request.properties` | 40 members, none of them these |
| `writeControl.writeMode` | `schemas.WriteControl.properties` | exactly `requiredRevisionId`, `targetRevisionId` |
| `commentsViewMode` | `resources.documents.methods.get.parameters` | exactly `documentId`, `includeTabsContent`, `suggestionsViewMode` |
| `CommentThread`, `SuggestionThread`, `Post`, `PostAuthor` | `schemas` | absent |

**This is the fact Q2's inference rests on**: every preview element is a
*field name*, never a mere enum value on an otherwise-public field. It also
independently confirms `HANDOVER.md` §4.3's account of why
`googleapiclient` rejects `commentsViewMode` before any request is sent.

### The type names the API does give up

The JSON transcoder names the fully-qualified proto type whenever a value
will not parse into a field. Mined live, verbatim from `error.message`:

| request | named type |
|---|---|
| `{"insertComment": "s"}` | `google.apps.docs.v1.InsertCommentRequest` |
| `{"acceptSuggestion": "s"}` | `google.apps.docs.v1.AcceptSuggestionRequest` |
| `{"rejectSuggestion": "s"}` | `google.apps.docs.v1.RejectSuggestionRequest` |
| `{"addCommentReply": "s"}` | `google.apps.docs.v1.AddCommentReplyRequest` |
| `{"addCommentReply": {"post": "s", …}}` | `google.apps.docs.v1.Post` |
| `post.commentAction = "BOGUS_XYZ"` | `google.apps.docs.v1.Post.CommentActionType` |
| `post.suggestionAction = "BOGUS_XYZ"` | `google.apps.docs.v1.Post.SuggestionActionType` |
| `insertComment.range = "s"` | `google.apps.docs.v1.Range` |
| `writeControl = "s"` | `google.apps.docs.v1.WriteControl` |
| `writeControl.writeMode = "BOGUS_XYZ"` | `google.apps.docs.v1.WriteControl.WriteMode` |
| `?commentsViewMode=BOGUS_XYZ` | `google.apps.docs.v1.CommentsViewMode` |
| `?suggestionsViewMode=BOGUS_XYZ` | `google.apps.docs.v1.SuggestionsViewMode` |

Note the two shapes: an enum owned by one message is **nested** under it
(`Post.CommentActionType`, `WriteControl.WriteMode`) while a standalone
request-parameter enum is **top-level** (`CommentsViewMode`,
`SuggestionsViewMode`). Both conventions are in use, so the status enums'
names **cannot be inferred** from the pattern — `CommentThread.Status`,
`CommentThread.StatusType` and a top-level `ThreadStatus` are all consistent
with what is observed. No guess is recorded anywhere.

Also worth noting, since `docs/preview-api-reference.md` calls the two enums
`commentAction` / `suggestionAction`: the *field* names are those, but the
*type* names carry a `Type` suffix — `CommentActionType`,
`SuggestionActionType`.

### Why the channel is closed for `status`

`status` is output-only on both thread kinds, so no request can carry one and
no error can name its type. Confirmed at all three positions it could
plausibly occupy — each answers as an **unknown name**, never as an invalid
value:

```
{"insertComment": {"content": "x", "status": "OPEN"}}
-> Invalid JSON payload received. Unknown name "status" at 'requests[0].insert_comment': Cannot find field.

{"addCommentReply": {"commentId": "c", "status": "RESOLVED"}}
-> Invalid JSON payload received. Unknown name "status" at 'requests[0].add_comment_reply': Cannot find field.

{"addCommentReply": {"commentId": "c", "post": {"content": "x", "status": "RESOLVED"}}}
-> Invalid JSON payload received. Unknown name "status" at 'requests[0].add_comment_reply.post': Cannot find field.
```

`test_no_request_carries_a_thread_status_so_no_error_can_name_its_type` pins
this, so if a future preview revision makes `status` writable anywhere, the
suite says so and the answer becomes reachable.

The field-mask channel was tried too and yields no type names — only whether
a path exists:

```
?fields=comments/status              -> 200 {"comments": [{"status": "OPEN"}]}
?fields=suggestions/status           -> 200 {"suggestions": [{"status": "REJECTED"}, {"status": "ACCEPTED"}]}
?fields=comments/status/bogusXyz     -> 400 Error expanding 'fields' parameter. Cannot find matching fields for path 'comments.status.bogusXyz'.
?fields=comments/bogusXyz            -> 400 Error expanding 'fields' parameter. Cannot find matching fields for path 'comments.bogusXyz'.
```

(Also learned there: `fields=comments/…` with `commentsViewMode` but without
`suggestionsViewMode` is refused — `Comments may only be explicitly included
if inline suggestions are also explicitly requested.`)

### The value sets — RESOLVED by observation

One document, driven through every reachable transition, reading the
thread-bearing GET after each step:

| step | `comments[0].status` | `suggestions[*].status` |
|---|---|---|
| after `insertComment` | `OPEN` | – |
| after `addCommentReply` `commentAction: RESOLVE` | **`RESOLVED`** | – |
| after `addCommentReply` `commentAction: REOPEN` | `OPEN` | – |
| after two SUGGEST-mode `insertText` | `OPEN` | `OPEN`, `OPEN` |
| after `acceptSuggestion` | `OPEN` | `OPEN`, **`ACCEPTED`** |
| after `rejectSuggestion` | `OPEN` | **`REJECTED`**, `ACCEPTED` |

So `docs/preview-api-reference.md`'s hand-inlined value sets are correct:
`CommentThread.status` ∈ {`OPEN`, `RESOLVED`} and `SuggestionThread.status` ∈
{`OPEN`, `ACCEPTED`, `REJECTED`}, plus `STATUS_UNSPECIFIED` in each. The
unspecified value is the proto3 zero and is therefore **omitted from JSON
rather than emitted** — unobservable by construction, not merely unobserved.

### A load-bearing side finding

**A resolved suggestion thread does not leave `suggestions[]`.** It stays,
restatused, and gains a reply carrying `suggestionAction: ACCEPT` / `REJECT`:

```jsonc
{"suggestionId": "suggest.6fjhenyky4c5",
 "status": "ACCEPTED",
 "summaryText": "Add: “ACC”",
 "replies": [{"postId": "AAACBsQJfuI", "suggestionAction": "ACCEPT"}]}
```

This matters because `HANDOVER.md` §4.4's rule — *"an accepted or rejected
suggestion is gone from the post-write pending set"* — would be false if the
pending set were `len(suggestions)`. It is not: `gdocs_preview.analysis`
derives the pending set from the body's suggestion marks, and the thread list
is only where authors and statuses come from. The rule holds, but for a
reason that is easy to get backwards; the test states it out loud.

### What changed in the repo

Nothing in `gdocs_preview/`. Q3 is documentation: the enum values are
confirmed rather than assumed, the type names stay unknown and are now
*proved* unknown, and the reference's UNCERTAIN item 2 can be narrowed from
"the names are unknown" to "the names are unreachable through any channel the
API exposes, for a stated structural reason".

### Still unknown

The two type names. The only remaining channels are outside this repo's reach
(Google's internal proto definitions, or a published `googleapis` proto for
the preview surface). Not worth chasing: nothing in the code depends on the
type *name*, only on the values, and those are now observed.

---

## Incidental: the e2e suite is bounded by a write quota

Found while validating, reported because it is load-bearing for anyone who
tries to reproduce the above.

`docs.googleapis.com` allows **60 write requests per minute per user**
(`WriteRequestsPerMinutePerUser`, project `498052759130`). Running
`uv run pytest e2e -m e2e_preview` **exhausts it and fails** — measured on a
clean five-minute cooldown, **with the new test module excluded**:
`6 failed, 11 passed, 5 errors`, every failure a 429:

```
Quota exceeded for quota metric 'Quota group for write operations' and limit
'Quota group for write operations per minute per user' of service
'docs.googleapis.com' for consumer 'project_number:498052759130'.
```

So this is **pre-existing**, not introduced here, and it means
`HANDOVER.md` §6.2's "22 tests" figure is not currently reachable in one
invocation. `uv run pytest e2e -m e2e_ga` passes (24 passed, 1 skipped), and
the new module passes standalone (18 passed in ~55 s). The quota recovers
within seconds, so the fix is pacing — a 429-aware retry in `e2e/util.py` or
in `ServerSession`, or splitting the preview marker across two runs. Left
undone deliberately: it is a change to shared harness beyond this round's
scope. Two scratch documents orphaned by a 429 that killed a teardown were
trashed by hand; `test_zz_teardown_audit.py` is the thing that would have
caught them had the session survived.

The new module was written with this ceiling in mind: it shares one scratch
document across every test that only provokes failures, merges the two
suggestion-status transitions into one document, and asserts four of the
eight mined type names rather than all eight — the other four are transcribed
above instead. Every one of those trade-offs is stated in the docstring that
makes it.
