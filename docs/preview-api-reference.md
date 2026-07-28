# Google Docs API — Developer Preview reference (comments & suggestions)

Human-readable reference for the Google Docs API **Developer Preview** surface
launched 2026-07-07: comment threads, suggestion accept/reject/delete, and the
`SUGGEST` write mode.

**Provenance**: transcribed from the official reference pages
(`developers.google.com/workspace/docs/api/reference/rest/v1/documents/request`,
`.../batchUpdate`, `.../response`, the documents resource page, and
`.../how-tos/suggestions`; all "Last updated 2026-07-07 UTC", fetched
2026-07-13). The preview surface is absent from the public discovery document,
so **nothing here has been verified against the live API** — verification is
pending Workspace Developer Preview Program enrollment. Statements marked
**UNCERTAIN** are known gaps in the official docs, preserved verbatim from the
overlay this file replaces (`codegen/overlay/docs_preview_overlay.json` +
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

### Additional exclusions (overlay decision, unverified)

The 8 preview comment/suggestion **thread operations** documented above
(`insertComment`, `addCommentReply`, `updateCommentPost`, `deleteComment`,
`deleteCommentReply`, `acceptSuggestion`, `rejectSuggestion`,
`deleteSuggestion`) were also treated as SUGGEST-incompatible: they act on
threads directly and are not content edits, so SUGGEST write mode does not
apply to them. This was a codegen-overlay design decision, **not verified
against the live preview API**.

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

### Per-request response members

- `InsertCommentResponse` — `{ commentThread: CommentThread }` (the
  newly-inserted thread).
- `AddCommentReplyResponse` — `{ post: Post }` (the newly-inserted reply).
- **UNCERTAIN**: whether the batchUpdate `Response` union actually gains
  `insertComment` / `addCommentReply` members (and their exact member names)
  was not transcribed; the response schemas exist per the batchUpdate
  reference, but their union wiring is unconfirmed.

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

### `CommentThread`

`commentId`, `anchorId` (the `CommentAnchor` in the document; multiple
threads may share one anchor), `headPost` (`Post`), `replies` (`Post[]`),
`status`, `plainTextQuote` (quoted document text at creation time; member of
the `quote` union).

- `status` enum: `STATUS_UNSPECIFIED`, `OPEN`, `RESOLVED`.
- **UNCERTAIN**: the enum values are inlined here; the real discovery schema
  name for this enum type is unknown.

### `SuggestionThread`

`suggestionId`, `headPost` (`Post`), `replies` (`Post[]`), `status`,
`summaryText` / `summaryHtml` (plain/HTML summary of the suggested
differences; may be empty). Created only as a byproduct of SUGGEST-mode
saves — never directly.

- `status` enum: `STATUS_UNSPECIFIED`, `OPEN`, `ACCEPTED`, `REJECTED`.
- **UNCERTAIN**: same inlined-enum caveat as `CommentThread.status`.

---

## Open UNCERTAIN items (resolve empirically post-enrollment)

1. `InsertCommentRequest` with `range` omitted — unanchored-comment behavior
   undocumented.
2. Real discovery names of the `CommentThread.status` /
   `SuggestionThread.status` enum types.
3. Whether the batchUpdate `Response` union gains `insertComment` /
   `addCommentReply` members, and their exact member names.
4. Whether (and where) `documents.get` exposes `CommentThread` /
   `SuggestionThread` objects in the `Document` payload — could not be
   confirmed from the reference pages.
5. Whether preview enrollment propagates per-project or per-account, and the
   error shapes for non-enrolled callers (the capabilities probe in
   `gdocs_preview/curated_tools.py` classifies these heuristically).
