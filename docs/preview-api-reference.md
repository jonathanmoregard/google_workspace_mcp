# Google Docs API — Developer Preview reference (comments & suggestions)

Human-readable reference for the Google Docs API **Developer Preview** surface
launched 2026-07-07: comment threads, suggestion accept/reject/delete, and the
`SUGGEST` write mode.

**Provenance**: transcribed from the official reference pages
(`developers.google.com/workspace/docs/api/reference/rest/v1/documents/request`,
`.../batchUpdate`, `.../response`, the documents resource page, and
`.../how-tos/suggestions`; all "Last updated 2026-07-07 UTC", fetched
2026-07-13). The preview surface is absent from the public discovery document.
Statements marked **VERIFIED 2026-07-30** were observed against the live API
with an enrolled Workspace Developer Preview account
(a single enrolled Workspace account); statements marked **UNCERTAIN** are still gaps in
the official docs, preserved verbatim from the overlay this file replaces
(`codegen/overlay/docs_preview_overlay.json` +
`codegen/overlay/generator_config.json`, removed in the 2026-07-29 redirect).

All preview operations are members of the `Request` union in
`documents.batchUpdate`. Comments are authored as the authenticated user
(OAuth user flow; no service accounts).

---

## Preview request types (batchUpdate `Request` union members)

### insertComment → `InsertCommentRequest`

Inserts a `CommentThread` into the document.

| Field | Type | Semantics |
|---|---|---|
| `content` | string | Comment text, plain text. Must be non-empty; max 2048 UTF-8 code units. |
| `assigneeEmailAddress` | string | Optional. Assignee email; empty = non-assigned comment. Max 2048 UTF-8 code units. |
| `range` | `Range` | The document range the comment is anchored to. Member of the `anchor` union — currently the only member. |

- **UNCERTAIN**: behavior when `range` is omitted (unanchored comment) is not
  explicitly documented.
- Response: `InsertCommentResponse` (see Response signals below).

### addCommentReply → `AddCommentReplyRequest`

Inserts a reply `Post` into a `CommentThread` **or** `SuggestionThread`.

| Field | Type | Semantics |
|---|---|---|
| `post` | `Post` | The reply. Writable fields: `content`, `assigneeEmail`, `commentAction`, `suggestionAction`; everything else output-only. |
| `commentId` | string | Target comment thread (`thread_id` union member). |
| `suggestionId` | string | Target suggestion thread (`thread_id` union member). |

- Exactly one of `commentId` / `suggestionId` must be set.
- A reply can *act* on the thread: `Post.commentAction` RESOLVE/REOPEN for
  comment threads, `Post.suggestionAction` ACCEPT/REJECT for suggestion
  threads. `Post.content` may be empty only when `commentAction` is
  RESOLVE or REOPEN.
- Response: `AddCommentReplyResponse`.

### updateCommentPost → `UpdateCommentPostRequest`

Updates a `Post` in a comment or suggestion thread.

| Field | Type | Semantics |
|---|---|---|
| `postId` | string | The post being updated. |
| `content` | string | New text, plain text. Non-empty; max 2048 UTF-8 code units. |
| `commentId` | string | Thread the post belongs to (`thread_id` union member). |
| `suggestionId` | string | Thread the post belongs to (`thread_id` union member). |

- Exactly one of `commentId` / `suggestionId` must be set.
- 400 if: the post is the `headPost` of a **SuggestionThread**, the requester
  is not the post's author, or the same batch contains multiple
  `UpdateCommentPostRequest`s for the same `postId`.

### deleteComment → `DeleteCommentRequest`

Deletes an entire `CommentThread`.

| Field | Type | Semantics |
|---|---|---|
| `commentId` | string | The thread to delete. |

- 400 if the requester is not the author of the thread's `headPost`.

### deleteCommentReply → `DeleteCommentReplyRequest`

Deletes a reply `Post` from a comment or suggestion thread.

| Field | Type | Semantics |
|---|---|---|
| `postId` | string | The reply to delete. |
| `commentId` | string | Thread the post belongs to (`thread_id` union member). |
| `suggestionId` | string | Thread the post belongs to (`thread_id` union member). |

- Exactly one of `commentId` / `suggestionId` must be set.
- 400 if: the requester is not the post's author, the reply contains an
  action, or the reply contains an assignee.

### acceptSuggestion → `AcceptSuggestionRequest`

| Field | Type | Semantics |
|---|---|---|
| `suggestionId` | string | The suggestion to accept. |

- 403 if the requester lacks **edit access** to the document.

### rejectSuggestion → `RejectSuggestionRequest`

| Field | Type | Semantics |
|---|---|---|
| `suggestionId` | string | The suggestion to reject. |

- 403 if the requester lacks edit access **and** is not the suggestion's
  author (either suffices).

### deleteSuggestion → `DeleteSuggestionRequest`

| Field | Type | Semantics |
|---|---|---|
| `suggestionId` | string | The suggestion to delete. |

- 403 if the requester is not the suggestion's author.

### Permission rules, summarized

| Operation | Requires |
|---|---|
| acceptSuggestion | edit access (403 otherwise) |
| rejectSuggestion | edit access OR being the suggestion author |
| deleteSuggestion | being the suggestion author |
| deleteComment | being the author of the thread's headPost |
| updateCommentPost | being the author of the post |
| deleteCommentReply | being the author of the post |

---

## `writeControl.writeMode = SUGGEST` mechanics

`BatchUpdateDocumentRequest.writeControl.writeMode` (Developer Preview enum
member) controls how the batch's updates are applied:

- `WRITE_MODE_UNSPECIFIED` — defaults to EDIT behavior.
- `EDIT` — apply all updates as normal edits.
- `SUGGEST` — apply all updates **as suggestions** (Developer Preview). New
  suggestion ids come back via `suggestionResponses` (below). Suggestion
  threads cannot be created directly — they exist only as a byproduct of
  SUGGEST-mode edits.

### Official "Unsupported requests in suggest mode" list

From `https://developers.google.com/workspace/docs/api/how-tos/suggestions`
(fetched 2026-07-13) — these 8 request types must NOT be sent in a
SUGGEST-mode batch:

- `addDocumentTab`
- `createNamedRange`
- `deleteFooter`
- `deleteHeader`
- `deleteNamedRange`
- `deleteTab`
- `updateDocumentTabProperties`
- `updateTableColumnProperties`

### Additional exclusions — REFUTED (2026-08-02, live API)

The 8 preview comment/suggestion **thread operations** documented above
(`insertComment`, `addCommentReply`, `updateCommentPost`, `deleteComment`,
`deleteCommentReply`, `acceptSuggestion`, `rejectSuggestion`,
`deleteSuggestion`) were also treated as SUGGEST-incompatible: they act on
threads directly and are not content edits, so SUGGEST write mode does not
apply to them. This was a codegen-overlay design decision, never verified
against the live preview API — **and it is wrong.** All eight return HTTP 200
with `commentUpdateState: ALL_SAVED` inside a `writeMode: SUGGEST` batch, and
they take effect. Only the 8 *officially* unsupported request types listed
above are refused. `mockdocs` was rejecting batches prod accepts and has been
fixed; see item 5 under "Open UNCERTAIN items" and
[`suggest-semantics.md`](findings/suggest-semantics.md).

### Partial support

- `updateDocumentStyle`: SUGGEST mode does not support the `documentFormat`,
  `useEvenPageHeaderFooter`, or `useFirstPageHeaderFooter` style fields.

All other GA batchUpdate request types (32 of the 40 in public discovery)
accept SUGGEST write mode.

---

## Response signals

### `BatchUpdateDocumentResponse` (preview additions)

| Field | Type | Semantics |
|---|---|---|
| `suggestionResponses` | `SuggestionResponse[]` | Suggestions affected by each update; maps **1:1 with the request list**. |
| `commentUpdateState` | enum | Whether comment/thread updates in the batch were applied. |

`commentUpdateState` values:

- `COMMENT_UPDATE_STATE_UNSPECIFIED`
- `NO_UPDATES_REQUESTED` — no comment updates in the batch.
- `ALL_SAVED` — all requested comment updates applied.
- `ALL_FAILED_UNKNOWN_REASON` — all requested comment updates failed.

**Critical semantics**: comment/suggestion thread updates can fail to save
even when text mutations in the same batch commit (partial failure). Always
check `commentUpdateState` after batches containing thread operations.

### `SuggestionResponse`

The suggestions affected by a given update. All fields are `string[]` of
suggestion ids: `createdSuggestionIds`, `updatedSummarySuggestionIds`,
`deletedSuggestionIds`, `acceptedSuggestionIds`, `rejectedSuggestionIds`.

### Per-request response members — **VERIFIED 2026-07-30**

The `Response` union does gain the members, under exactly these names, and
they carry the **author** of the object just created — so a write path never
needs a follow-up read to report authorship:

- `replies[i].insertComment.commentThread` → the whole `CommentThread`:

  ```json
  {"insertComment": {"commentThread": {
    "commentId": "AAACEfTspmk",
    "anchorId": "kix.5jicnobkgd9j",
    "headPost": {
      "postId": "AAACEfTspmk", "content": "probe comment",
      "contentHtml": "probe comment",
      "author": {"displayName": "…", "me": true, "user": "users/1085…"},
      "createTime": "2026-07-30T18:51:48.198Z",
      "updateTime": "2026-07-30T18:51:48.198Z",
      "commentAction": "NO_COMMENT_ACTION_CHANGE"},
    "status": "OPEN", "plainTextQuote": "Say"}}}
  ```

  Note `headPost.postId == commentId` for the thread head.

- `replies[i].addCommentReply.post` → the new reply `Post`, with `author`,
  `contentHtml`, `createTime`/`updateTime`, and `commentAction`
  (comment threads) or `suggestionAction` (suggestion threads).
- Requests that produce no response member (e.g. `insertText`) occupy their
  index with an empty object `{}`, so `replies` still maps 1:1.
- A SUGGEST replacement (`deleteContentRange` + `insertText`) yields ONE
  suggestion id: request 0 reports it under `createdSuggestionIds`, request 1
  reports the same id under `updatedSummarySuggestionIds`.

---

## Reading threads: `documents.get` with tabs + comments — **VERIFIED 2026-07-30**

Threads (and therefore **authors**) are absent from a plain `documents.get`.
They appear only when the read asks for them:

```
GET https://docs.googleapis.com/v1/documents/{documentId}
      ?suggestionsViewMode=SUGGESTIONS_INLINE
      &commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED
      &includeTabsContent=true
```

- `includeTabsContent=true` is **required** alongside `commentsViewMode`.
  Without it: `400 "Comments view mode may only be specified if tabs content
  is also requested."`
- `commentsViewMode` is **absent from the public discovery document**, so the
  googleapiclient Resource refuses it before any request is sent
  (`TypeError: Got an unexpected keyword argument commentsViewMode`;
  `documents().get` accepts only `documentId`, `suggestionsViewMode`,
  `includeTabsContent` and the standard system parameters). The repo issues
  this read with a `google.auth.transport.requests.AuthorizedSession` built
  from the credentials the injected Resource already holds
  (`service._http.credentials`) — see `gdocs_preview/preview_read.py`.
- `commentsViewMode` is `google.apps.docs.v1.CommentsViewMode`. Accepted:
  `COMMENTS_VIEW_MODE_UNSPECIFIED`, `COMMENTS_VIEW_MODE_INCLUDED`,
  `COMMENTS_VIEW_MODE_OMITTED`; anything else is a 400 naming the enum type.
  Responses always echo it — `COMMENTS_VIEW_MODE_OMITTED` for a plain or
  tabs-only read.

### Response layout

| read | top-level keys |
|---|---|
| plain `documents.get` | `body`, `documentStyle`, `namedStyles`, `title`, `documentId`, `revisionId`, `suggestionsViewMode`, `commentsViewMode` |
| `includeTabsContent=true` only | `tabs`, `title`, `documentId`, `revisionId`, `suggestionsViewMode`, `commentsViewMode` (no threads) |
| tabs + `COMMENTS_VIEW_MODE_INCLUDED` | the above **plus** `suggestions` and `comments` |

Asking for tabs content **removes the top-level `body`**: content moves to
`tabs[i].documentTab`. Each tab is `{tabProperties, documentTab}` (plus
`childTabs` for nested tabs); `tabProperties` is `{tabId, title, index}`
(`t.0` for the first tab); `documentTab` holds `body`, `documentStyle`,
`namedStyles`, and `headers`/`footers`/`footnotes`/`commentAnchors` when
present. `tabs[0].documentTab.body` is byte-identical to the GA read's
`body` for a single-tab document — **indexes are unchanged**, so indexes
taken from either read stay valid for `batchUpdate`.

Empty repeated fields are omitted proto3-style: a document with suggestions
but no comments comes back with no `comments` key at all.

### `suggestions[]` (SuggestionThread)

```json
{"suggestionId": "suggest.ymc8iork4nln",
 "headPost": {"postId": "AAACEfTspmU",
              "author": {"displayName": "…", "me": true, "user": "users/1085…"},
              "createTime": "…", "updateTime": "…",
              "suggestionAction": "NO_SUGGESTION_ACTION_CHANGE"},
 "replies": [ Post, … ],
 "status": "OPEN",
 "summaryText": "Replace: “brave” with “bold”",
 "summaryHtml": "<div …>"}
```

A suggestion `headPost` has **no `content`** (unlike a comment head post) —
the human-readable summary is `summaryText`/`summaryHtml` on the thread.

### `comments[]` (CommentThread)

Same shape as the `insertComment` response member above: `commentId`,
`anchorId`, `headPost` (with `content`, `contentHtml`, `author`, times,
`commentAction`), `replies[]`, `status`, `plainTextQuote`.

Richer than the Drive v3 comment surface, which exposes no anchor id, no
per-post ids and no People resource names. Comment ids DO interoperate:
a thread created with `insertComment` is visible to Drive `comments.list`
and can be updated/resolved/deleted through it (verified 2026-07-30).

### `summaryText` grammar

Google's own label for a suggestion, in **typographic** quotes (U+201C /
U+201D), whitespace-collapsed and trimmed:

| edit | `summaryText` |
|---|---|
| insertion | `Add: “Say”` |
| deletion | `Delete: “brave”` |
| replacement | `Replace: “brave” with “bold”` |

This is the oracle for the mock's SPEC §8 `label()`
(`docs/plans/2026-07-30-suggestion-mock-spec.md`), whose ASCII quotes were a
guess; `mockdocs/model.py` now matches prod, and
`e2e/test_preview_surface.py::test_summary_text_grammar_matches_the_mock_labels`
re-checks the two against each other on every enrolled run.

---

## Supporting object schemas

### `Post`

One post in a comment or suggestion thread. Writable when creating a reply:
`content`, `assigneeEmail`, `commentAction`, `suggestionAction`. Output-only:
`postId`, `contentHtml`, `author` (`PostAuthor`), `createTime`, `updateTime`,
`deleted` (if true, content/author are empty), `fromImportedDocument`,
`fromCopiedDocument`, `fromDocumentComparison`.

- `commentAction` enum: `COMMENT_ACTION_TYPE_UNSPECIFIED`,
  `NO_COMMENT_ACTION_CHANGE`, `RESOLVE` (resolves the thread), `REOPEN`
  (reopens the thread).
- `suggestionAction` enum: `SUGGESTION_ACTION_TYPE_UNSPECIFIED`,
  `NO_SUGGESTION_ACTION_CHANGE`, `ACCEPT`, `REJECT`.
- `content` max 2048 UTF-8 code units.

### `PostAuthor`

All fields output-only: `displayName` (may be absent if anonymous), `me`
(whether the author is the authenticated caller), `anonymous`, `user`
(People API resource name `users/{user}`; not populated if anonymous or from
an imported document). This is how suggestion/comment **authorship** is
exposed — `SuggestionThread.headPost.author` resolves the suggestion author
(the MVP plan's "suggestion author" unknown).

**VERIFIED 2026-07-30**: a real author block is
`{"displayName": "Jonathan Moregård", "me": true, "user": "users/1085…"}` —
`anonymous` is omitted entirely when false, so consumers must treat a missing
`anonymous` as unknown rather than defaulting it.

### `CommentThread`

`commentId`, `anchorId` (the `CommentAnchor` in the document; multiple
threads may share one anchor), `headPost` (`Post`), `replies` (`Post[]`),
`status`, `plainTextQuote` (quoted document text at creation time; member of
the `quote` union).

- `status` enum: `STATUS_UNSPECIFIED`, `OPEN`, `RESOLVED`.
- **UNCERTAIN**: the enum values are inlined here; the real discovery schema
  name for this enum type is unknown.
- Where to read them: the tabs + `commentsViewMode` read, top-level
  `comments` (see above).

### `SuggestionThread`

`suggestionId`, `headPost` (`Post`), `replies` (`Post[]`), `status`,
`summaryText` / `summaryHtml` (plain/HTML summary of the suggested
differences; may be empty). Created only as a byproduct of SUGGEST-mode
saves — never directly.

- `status` enum: `STATUS_UNSPECIFIED`, `OPEN`, `ACCEPTED`, `REJECTED`.
  Observed value for a pending suggestion: `OPEN`.
- **UNCERTAIN**: same inlined-enum caveat as `CommentThread.status`.
- Where to read them: the tabs + `commentsViewMode` read, top-level
  `suggestions` (see above).

---

## Resolved (2026-07-30, enrolled account)

- ~~Whether (and where) `documents.get` exposes `CommentThread` /
  `SuggestionThread` objects~~ → top-level `suggestions` / `comments`, only
  in the `commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED` +
  `includeTabsContent=true` read. See "Reading threads" above.
- ~~Whether the batchUpdate `Response` union gains `insertComment` /
  `addCommentReply` members~~ → it does, under those names, carrying the
  full `CommentThread` / `Post` including `author`.
- ~~How authorship is exposed~~ → `headPost.author` (`PostAuthor`) on both
  thread kinds and on every reply.
- ~~Error shape for a bogus suggestion id~~ → HTTP **404** "Suggestion with
  ID … does not exist." (not 400); `preview_status.classify_preview_error`
  handles it.

## Open UNCERTAIN items

Four of the five original items were settled against the live API on
2026-08-02. Verbatim requests, responses and error strings are in
[`docs/findings/`](findings/); HANDOVER §7 is the index.

1. ~~`InsertCommentRequest` with `range` omitted~~ — **RESOLVED. The API
   refuses it**: `400 Invalid requests[0].insertComment: Insert comment
   requests must specify a range to anchor to.` An empty range and a tab-only
   range are refused too. The tool's mandatory range is Google's restriction,
   not a self-imposed one. A Drive-created unanchored comment does read back
   here as a full `CommentThread` with `anchorId` and `plainTextQuote` absent —
   that absence is what "document-level" looks like on this surface.
   ([`errors-and-discovery.md`](findings/errors-and-discovery.md))
2. **STILL OPEN, and unreachable: the discovery type NAMES** of the
   `CommentThread.status` / `SuggestionThread.status` enums. Every labelled
   variant (`DEVELOPER_PREVIEW`, `PREVIEW`, `TRUSTED_TESTER`,
   `LIMITED_AVAILABILITY`) returns the byte-identical public document;
   `v1preview`/`v1beta`/`v1alpha` are 404; enrolled credentials change nothing.
   The transcoder names preview proto types in a value error, but `status` is
   output-only on both threads, so no request can carry one and no error can
   name one. The **values** are confirmed live: `CommentThread.status`
   OPEN→RESOLVED→OPEN, `SuggestionThread.status` OPEN→ACCEPTED/REJECTED.
   ([`errors-and-discovery.md`](findings/errors-and-discovery.md))
3. **STILL OPEN: per-project or per-account enrollment.** Two questions under
   one label, with one experiment each. Neither has been run.

   **A — vary the project, hold the account fixed.** A second, non-enrolled
   GCP project with its own OAuth client and an interactive consent grant for
   the same Google account: a human in a browser. Specified step by step in
   `pending_for_human.md`. `available` ⇒ the project is not the gate;
   `unavailable` ⇒ it is.

   **B — vary the account, hold the project fixed.** One OAuth client, one GCP
   project, two Google accounts — one in an enrolled Workspace org, one not,
   both authenticated into the same credential store and both test users on
   the consent screen. Run `check_docs_review_capabilities(probe=true,
   document_id=…)` under each and compare. Decision rule, fixed in advance:
   **both `available` ⇒ the gate is per-project and the account is irrelevant;
   one `available` and one `unavailable` ⇒ there is a per-account component.**
   Any other pair settles nothing; an `unknown` from either probe means it
   never reached the question and must be re-run, not read.

   **What the public documentation says** (retrieved 2026-08-25): the
   Classroom preview page gates on the project — "The calling Google Cloud
   project must be enrolled in the Google Workspace Developer Preview Program
   and allow listed by Google"
   (<https://developers.google.com/workspace/classroom/reference/preview>) —
   while the program page gates on the individual: "If your email address
   cannot be added to the Google Group, you won't be able to access the
   dedicated client library, and you won't get access to some of the features"
   (<https://developers.google.com/workspace/preview>). Neither addresses the
   two-accounts-one-client case; the Docs-specific preview page was not
   retrieved. The documentation supports both readings and settles neither, so
   the repo keys its verdict by `user_google_email` and reports `unknown`
   until observed — correct under either answer (HANDOVER §3.6).

   **Confound.** The preview request types are absent from the public
   discovery document and no label restores them (`labels=DEVELOPER_PREVIEW` /
   `PREVIEW` / `TRUSTED_TESTER` / `LIMITED_AVAILABILITY` all return the
   byte-identical public document), so a failure can mean not-enrolled *or* a
   client/payload problem: `insertcomment` is an unknown name, not a
   case-insensitive match, and a non-enrolled caller and a typo are
   indistinguishable from the message alone. Probe with a payload already
   observed to work under an enrolled account.

   Related but distinct, and now answered: the classifier's marker strings
   were validated against real proto-parse errors, and a second grammar
   (`Invalid value at 'P' (TYPE), "V"`) that carried none of them was found
   falling through to `available` and is now classified `("unknown",
   "request_not_parsed")`. That validates the markers; it does not establish
   what a non-enrolled project returns for a recognised-but-ungated request
   type.
   ([`errors-and-discovery.md`](findings/errors-and-discovery.md))
4. ~~Tab attribution on the thread arrays~~ — **RESOLVED. No thread object
   carries any tab field.** `suggestions[]` keys are exactly `{headPost,
   status, suggestionId, summaryHtml, summaryText}`; `comments[]` are
   `{anchorId, commentId, headPost, plainTextQuote, status}`. Attributing a
   suggestion to the tab whose body carries its id is correct and cannot be
   ambiguous — a range cannot span tabs, and one SUGGEST batch writing into
   two tabs mints two distinct ids. **Comments are different**: no tab body
   carries a comment id, but each tab has a disjoint
   `documentTab.commentAnchors` map, so `anchorId` is the join key.
   ([`tabs.md`](findings/tabs.md))
5. ~~SUGGEST-incompatibility of the thread operations~~ — **RESOLVED, and the
   overlay was half wrong.** The eight officially-unsupported request types
   really are refused: `400 Invalid requests[0].X: Request does not support
   application as suggestion.`, with each accepted in EDIT mode on the next
   call. The **preview thread ops are NOT refused** — `insertComment`,
   `addCommentReply`, `acceptSuggestion`, `rejectSuggestion` and the three
   deletes all return HTTP 200 with `commentUpdateState: ALL_SAVED` inside a
   `writeMode: SUGGEST` batch, and take effect. See "Additional exclusions",
   which has been corrected.
   ([`suggest-semantics.md`](findings/suggest-semantics.md))

Two facts found while answering the above, each confirmed independently by two
separate investigations:

- **The thread array is not the pending set.** A resolved suggestion does not
  leave `suggestions[]`; it is restatused and gains a `suggestionAction` reply.
  Any code deriving "still pending" from `len(suggestions)` is wrong — the
  pending set comes from the body's marks.
- **Both `PREVIEW_*` view modes always degrade to the GA read.**
  `documents.get` refuses `commentsViewMode` alongside them: `400 "Comments
  may not be requested when previewing suggestions."`
