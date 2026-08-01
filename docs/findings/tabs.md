# Findings: nested tabs, and tab attribution of threads

Two long-open empirical questions, resolved against the **real** Google Docs
API on **2026-08-01** with the enrolled Developer Preview credentials
(`jonathan@klaffat.com`, `WORKSPACE_MCP_CREDENTIALS_DIR` default store).

This file replaces two "unverified" claims:

- HANDOVER §7 *Untested paths* → "Nested tabs. … no e2e test has ever run
  against a real document with nested tabs."
- `docs/preview-api-reference.md` open UNCERTAIN item **4** → "Whether a
  multi-tab document's `suggestions`/`comments` arrays carry any tab
  attribution."

Every payload below is copied verbatim from a probe run (scratch documents,
all trashed). The permanent tests are `e2e/test_tabs_surface.py` (6 tests:
1 × `e2e_ga`, 5 × `e2e_preview`) plus new unit tests in
`tests/gdocs_preview/test_preview_read.py`.

---

## Q1. Nested tabs — **RESOLVED**

### Can nested tabs be created through the API?

**Yes.** `addDocumentTab` with `tabProperties.parentTabId` creates a child
tab. No Docs UI needed, and it is **not** preview-gated — the request is on
the GA surface, and upstream's `manage_doc_tab(action="create",
parent_tab_id=…)` already emits it (`gdocs/docs_helpers.py:1069`
`create_insert_doc_tab_request`).

Request (raw `POST documents/{id}:batchUpdate`):

```json
{"requests": [{"addDocumentTab": {"tabProperties": {
    "title": "Child of root", "index": 0, "parentTabId": "t.0"}}}]}
```

Response — **HTTP 200**:

```json
{"replies": [{"addDocumentTab": {"tabProperties": {
      "tabId": "t.g23ug7ysbuqh",
      "title": "Child of root",
      "parentTabId": "t.0",
      "index": 0,
      "nestingLevel": 1}}}],
 "documentId": "1wuyaV9d…",
 "suggestionResponses": [{}],
 "commentUpdateState": "NO_UPDATES_REQUESTED"}
```

`parentTabId` belongs **inside** `tabProperties`. As a sibling of it the
request is rejected before anything is applied:

```json
{"requests": [{"addDocumentTab": {
    "tabProperties": {"title": "Child alt", "index": 0},
    "parentTabId": "t.0"}}]}
```

→ **HTTP 400**

```
Invalid JSON payload received. Unknown name "parentTabId" at
'requests[0].add_document_tab': Cannot find field.
```

(That error text is worth noting: it is one of
`preview_status._UNKNOWN_FIELD_MARKERS`. A malformed *shape* of a GA request
produces the same "cannot find field" wording the classifier reads as
"not enrolled". The classifier only ever sees the probe's own fixed request,
so this is not a live bug — but it is evidence that the marker is about JSON
parsing, not about enrollment.)

A tab created this way is **not** also reported at top level; it appears
only under its parent's `childTabs`. Read back
(`includeTabsContent=true&commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED`):

```
- tabId='t.0'            title='Tab 1'         index=0 parentTabId=None  childTabs=1
  - tabId='t.g23ug7ysb…' title='Child of root' index=0 parentTabId='t.0' childTabs=0
- tabId='t.ju539y2khz…' title='Second'         index=1 parentTabId=None  childTabs=0
```

Nesting is arbitrarily deep in principle; two levels were exercised live and
three in unit tests.

**`addDocumentTab` is refused in SUGGEST mode** — the first live confirmation
of any of the eight request types HANDOVER §4.7 lists as SUGGEST-incompatible
(open UNCERTAIN item 5, previously a codegen-overlay design decision only):

```json
{"requests": [{"addDocumentTab": {"tabProperties": {"title": "Suggested", "index": 2}}}],
 "writeControl": {"writeMode": "SUGGEST"}}
```

→ **HTTP 400** `Invalid requests[0].addDocumentTab: Request does not support
application as suggestion.` This resolves item 5 for `addDocumentTab` only;
the other seven types remain untested.

### Does `tab_documents()` flatten it correctly?

**Yes for content, and it used to lose the tree.**

*Correct:* the depth-first walk returns `[root, child, top2]`, each tab's
body/headers/footers/footnotes reshaped into a GA-shaped `Document`, indexes
untouched. Child-tab suggestions and comments are found: a suggestion made in
the nested tab is listed with `tab_id` = the child's id, is filterable by it,
and an index window resolved in the child answers about the child alone
(`e2e/test_tabs_surface.py::test_a_child_tab_is_a_full_coordinate_space_of_its_own`).

*Indexes are per-tab, as §4.1 claims.* Three tabs of deliberately different
lengths in one document, each numbered from its own start:

```
- t.0            'Tab 1'       body endIndex=40
  - t.ogt71ysx…  'NestedChild' body endIndex=46
- t.1rrzv4h7…    'TopTwo'      body endIndex=76
```

A suggestion inserted at local index 1 in the root and at local index 1 in
the child come back as two cards, both `start_index: 1`, with different
`context_after` — the child's window starts with the child's prose.

*What was wrong:* **`index` is a tab's position among its SIBLINGS.** The
document above answers with **two tabs at `index: 0`** — the root and the
root's child. The flattened inventory the tools emit
(`read.tab_metadata`, surfaced as `tabs` on every read tool) carried only
`{tab_id, title, index}`, so a nested tab was presented as a second
top-level tab whose `index` collided with the first, with nothing in the
response saying otherwise.

**Repo change** (`gdocs_preview/preview_read.py`): `TabDocument` and its
`metadata` gained `parent_tab_id` and `nesting_level`, taken from the walk
rather than from `tabProperties` — proto3 omits `nestingLevel: 0` and a
top-level tab carries no `parentTabId` at all, so the fields are absent
exactly where they would otherwise have to be read as defaults. The GA
fallback's single implicit tab reports `None` for both, because that read
cannot see the tab tree at all.

### What the GA fallback sees of a nested document

`documents.get` **without** `includeTabsContent` returns the root tab's body
and nothing else — measured on a document with content and a pending
suggestion in all three tabs:

```
top-level keys: ['body', 'commentsViewMode', 'documentId', 'documentStyle',
                 'namedStyles', 'revisionId', 'suggestionsViewMode', 'title']
body endIndex: 25
suggestion ids visible: ['suggest.ykmnth8dwuq3']     # 3 exist in the document
body text: 'AA Base text for root. \n'
```

So §4.6's `complete=False` premise holds for nested tabs specifically: an id
absent from the GA read may be sitting in a child tab that read structurally
cannot see. Pinned by
`test_the_ga_fallback_cannot_see_a_nested_tab_and_never_claims_to`, which
drives the real degraded-read server and asserts `tabs: []`,
`suggestion_count: 1`, and that a `tab_id` filter is **refused** rather than
answered `matched_count: 0`.

### Still unknown

- **Nesting depth limit.** Two levels were created live; three are exercised
  in fixtures. No depth was probed to failure, so whether the API caps
  nesting is unknown. Not pursued because the tools treat depth uniformly
  (a recursive walk), so a cap would change nothing here.
- **`updateDocumentTabProperties` re-parenting.** Whether an existing tab can
  be moved under a new parent was not probed; nothing in `docs_preview` calls
  it.

---

## Q2. Multi-tab tab attribution — **RESOLVED**

Setup: one document, three tabs (`t.0` root, `t.ogt71ysxrifq` nested under
it, `t.1rrzv4h7cesm` a second top-level tab), each with its own prose, **one
pending suggestion and one anchored comment in each**.

### Do the thread objects carry any tab field?

**No. None of them, anywhere in the object.**

`suggestions[]` — every key on every thread, verbatim from the read:

```
suggestionId=suggest.yuwdbd3nws29 keys=['headPost', 'status', 'suggestionId', 'summaryHtml', 'summaryText']
  headPost keys=['author', 'createTime', 'postId', 'suggestionAction', 'updateTime']
  summaryText='Add: “[sugg-CHILD]”'
  serialized thread contains the substring 'tab'? False
```

(identically for the other two). No tab id appears anywhere in a thread's
JSON — the probe searched each thread's serialized form for all three tab ids
and found none.

`comments[]`:

```
commentId=AAACBr0-pDU keys=['anchorId', 'commentId', 'headPost', 'plainTextQuote', 'status']
  headPost keys=['author', 'commentAction', 'content', 'contentHtml', 'createTime', 'postId', 'updateTime']
  anchorId='kix.q273r6ow6auz' quote='[sug' content='comment in CHILD'
```

Same result: no tab field, and no tab id anywhere in the serialized thread.

### Is attribution-by-id-location correct for suggestions?

**Yes.** Walking each tab's `documentTab` for `suggestedInsertionIds` /
`suggestedDeletionIds` places every id in exactly one tab, and the placement
matches the tab each `suggest_doc_edit` named:

```
t.0            ('Tab 1'):       ['suggest.8ib0tvw494kk']   # created in ROOT
t.ogt71ysxrifq ('NestedChild'): ['suggest.yuwdbd3nws29']   # created in CHILD
t.1rrzv4h7cesm ('TopTwo'):      ['suggest.onxt7h5kjcy2']   # created in TOP2
```

### Can two tabs produce ambiguity?

**No mechanism was found, and the one that looked most likely does not
produce it.** A suggestion id is minted per tab even when a *single*
batchUpdate touches two tabs — one `SUGGEST` batch with an `insertText` in
each of two tabs:

```json
"suggestionResponses": [
  {"createdSuggestionIds": ["suggest.ykmnth8dwuq3"]},
  {"createdSuggestionIds": ["suggest.m4x62gnx02ss"]}
]
```

Two requests, two distinct ids, one per tab. A second batch pairing a
`deleteContentRange` in the first tab with an `insertText` in the second
merged the delete into the *first tab's existing* suggestion and created a
new id in the second — the merge stayed inside its tab:

```json
"suggestionResponses": [
  {"updatedSummarySuggestionIds": ["suggest.ykmnth8dwuq3"]},
  {"createdSuggestionIds": ["suggest.4085t0pk6v9p"]}
]
```

After both batches: 3 threads in the top-level `suggestions[]`, 3 distinct
ids found across the tab bodies, **0 ids in more than one tab**. A Docs range
cannot span tabs, so there is no request shape that could put one suggestion
in two of them.

This is asserted permanently in
`test_no_suggestion_id_is_claimed_by_more_than_one_tab`, which also checks
that the three per-tab listings **partition** the document: union equals the
whole id set, and the sizes sum to the total with nothing counted twice.

### Do comments carry tab attribution?

**They differ from suggestions, and the repo was reporting nothing.**

A comment thread has no ids inside a tab body to place it by — the tab body
carries no comment ids at all. What it carries is a **`commentAnchors` map**,
per tab, keyed by anchor id, and the maps are **disjoint**:

```
t.0:            {"kix.x55quw35soca": {"anchorId": "kix.x55quw35soca",
                  "ranges": [{"startIndex": 1, "endIndex": 5}]}}
t.ogt71ysxrifq: {"kix.q273r6ow6auz": {"anchorId": "kix.q273r6ow6auz",
                  "ranges": [{"startIndex": 1, "endIndex": 5,
                              "tabId": "t.ogt71ysxrifq"}]}}
t.1rrzv4h7cesm: {"kix.7v4p216tus1i": {"anchorId": "kix.7v4p216tus1i",
                  "ranges": [{"startIndex": 1, "endIndex": 5,
                              "tabId": "t.1rrzv4h7cesm"}]}}
```

Each thread's `anchorId` is therefore the join key onto its tab. (Note the
inner `ranges[].tabId` is **omitted for `t.0`** and present for the others,
even though the creating request named `"tabId": "t.0"` explicitly — do not
read the range's `tabId`; read the map the range lives in.)

Before this work, `preview_read.comment_threads()` emitted
`{comment_id, anchor_id, status, quoted_text, content, author, post_id,
create_time, update_time, replies}` — **no address of any kind**. On a
multi-tab document `get_doc_review_view` therefore returned every comment in
the document with nothing saying which tab it was in, on the tool an agent
actually reads a document with.

**Repo change** (`gdocs_preview/preview_read.py`): a new `anchor_tab_ids()`
builds the `anchorId → tabId` map by walking the tabs (children included),
and `comment_threads()` joins on it, adding **`tab_id`** to every comment
record. `None` means the read could not place the thread — an unanchored
comment, an anchor in no tab's map, or a payload with no tabs — and is never
silently the default tab. An anchor id appearing in two tabs (never observed;
an anchor range cannot span tabs) maps to `None` rather than to whichever tab
was walked first.

Pinned live by `test_a_comment_is_placed_in_the_tab_it_was_anchored_in`,
which creates three comments with the **identical** anchor range `[1, 5)` in
three different tabs — indistinguishable without the join — and asserts the
exact `comment_id → tab_id` map.

### Still unknown

- **Unanchored comments.** `create_anchored_doc_comment` requires a range, so
  a thread with no `anchorId` was not produced here; open UNCERTAIN item 1 is
  untouched. The code path for it is covered by unit test
  `test_an_unplaceable_comment_is_null_never_the_default_tab`, which asserts
  `tab_id: None` rather than a guess — but a live unanchored thread has never
  been read.
- **`get_doc_review_view` does not FILTER comments by tab.** `comments` is
  still the whole document's list even when the view is windowed into one
  tab. That is now visible rather than hidden (every record says which tab it
  is in), and changing the filtering semantics is a behaviour change beyond
  this investigation — `scope_note` currently documents that
  `segment_id`/`tab_id` narrow nothing without an index window. Flagged, not
  changed.
- **Drive-side comment listing** (`list_document_comments`) exposes no anchor
  id at all, so it cannot place a comment in a tab. Unchanged, and the reason
  `get_doc_review_view` is the review surface.

---

## Repo changes made

| file | change |
|---|---|
| `gdocs_preview/preview_read.py` | `TabDocument`/`metadata` gain `parent_tab_id` + `nesting_level`, derived from the depth-first walk; new `anchor_tab_ids()`; `comment_threads()` records `tab_id`. |
| `e2e/test_tabs_surface.py` | **new** — 6 tests (1 `e2e_ga`, 5 `e2e_preview`) covering everything above. |
| `tests/gdocs_preview/test_preview_read.py` | new `TestCommentTabAttribution` class + 3 nesting tests; the `metadata` equality assertion updated. |
| `tests/gdocs_preview/test_curated_tools.py` | the `result["tabs"]` equality assertion updated. |

Verification at the time of writing: `uv run pytest tests/ -q` →
**2397 passed, 3 skipped** (baseline was 2391/3); `uv run pytest e2e -q -rs`
→ **42 passed, 1 skipped** (the skip is the pre-existing
not-enrolled-error-shape test); `ruff check .` and `ruff format --check .`
clean.

## Hygiene note

Every document created by this work — probe scripts and e2e alike — was
trashed. A Drive audit afterwards found two **pre-existing** strays from an
earlier session, left in place rather than deleted:
`e2e-gdocs-review-degradeprobe` and
`e2e-gdocs-review-20260730-231057-b2a59274-preview`.
