# How Google Docs merges adjacent suggestions — measured, 2026-08-01/02

Three questions that `mockdocs` answered with a **guess** are settled here
against the live Docs Developer Preview API (account `jonathan@klaffat.com`,
enrolled project `498052759130`). Every payload below is verbatim from a probe
run; nothing is reconstructed.

| # | question | verdict |
|---|---|---|
| Q1 | the real `MERGE_TOLERANCE` (spec §13.2) | **RESOLVED** — 0, identical for all four insert/delete orderings |
| Q2 | thread policy on merge (spec §10 / §13.3) | **RESOLVED (vacuously)** — no suggestion is ever destroyed by a merge, so no thread is ever orphaned |
| Q3 | does the API rename or report a pre-merge id (`mockdocs/adapter.py:579`) | **RESOLVED** — never renames; the **pre-existing** id survives and is reported under `updatedSummarySuggestionIds` |

**One mechanism explains all three.** What the repo has been calling "merge" is
not two suggestions becoming one. It is **absorption at creation time**: a
SUGGEST edit whose range abuts or overlaps an existing same-author suggestion
never mints a second suggestion — the API extends the one already there and
reports it as *updated*. Two suggestions that already exist stay two
**forever**, even when a later edit pushes them into contact or spans both of
them.

Permanent coverage: `e2e/test_merge_semantics.py` (5 tests, marker
`e2e_preview`). Throwaway probe scripts are not in the repo (they were run out
of `/tmp`); their raw output is transcribed below.

---

## Q1 — the real merge tolerance: **RESOLVED, it is 0**

### Method

One **fresh document per case** (so nothing can contaminate anything else),
seeded with `0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz`.
Batch 1 makes suggestion S1 at index 10 in SUGGEST mode; the document is
re-read to learn S1's real range `[lo, hi)`; batch 2 then edits at `hi + gap`,
so exactly `gap` unchanged characters separate the two edits. Merge is read off
the document, not off the response: `len(suggestions) == 1` means one card.

All four orderings were tested, because spec §13.2 suspected insert-then-insert
and insert-then-delete might differ.

### Result

```
mode      gap=0                          gap=1                  gap=2
ins_ins   1 card  updatedSummary…        2 cards  created…      2 cards  created…
ins_del   1 card  updatedSummary…        2 cards  created…      2 cards  created…
del_ins   1 card  updatedSummary…        2 cards  created…      2 cards  created…
del_del   1 card  updatedSummary…        2 cards  created…      2 cards  created…
```

Verbatim per-case summary (`batch2.suggestionResponses` + the labels the
document reported afterwards):

```
ins_ins  gap=0 n=1 [{'updatedSummarySuggestionIds': ['suggest.gfv5ri56n8oi']}] {'suggest.gfv5ri56n8oi': 'Add: “XY”'}
ins_ins  gap=1 n=2 [{'createdSuggestionIds': ['suggest.66gqlkze0cfw']}]        {'suggest.66gqlkze0cfw': 'Add: “Y”', 'suggest.xgys7kb7ddga': 'Add: “X”'}
ins_ins  gap=2 n=2 [{'createdSuggestionIds': ['suggest.rzuf818r6gh5']}]        {'suggest.njd2jftfxzs7': 'Add: “X”', 'suggest.rzuf818r6gh5': 'Add: “Y”'}
ins_del  gap=0 n=1 [{'updatedSummarySuggestionIds': ['suggest.7axpgtp5fcrw']}] {'suggest.7axpgtp5fcrw': 'Replace: “9” with “X”'}
ins_del  gap=1 n=2 [{'createdSuggestionIds': ['suggest.fqddqqwdyepb']}]        {'suggest.v3bc2nqaq8zn': 'Add: “X”', 'suggest.fqddqqwdyepb': 'Delete: “A”'}
ins_del  gap=2 n=2 [{'createdSuggestionIds': ['suggest.gdch267g8p9w']}]        {'suggest.qyxi6omj0wls': 'Add: “X”', 'suggest.gdch267g8p9w': 'Delete: “B”'}
del_ins  gap=0 n=1 [{'updatedSummarySuggestionIds': ['suggest.segkkdop6dbb']}] {'suggest.segkkdop6dbb': 'Replace: “9” with “Y”'}
del_ins  gap=1 n=2 [{'createdSuggestionIds': ['suggest.3q36hwtauzym']}]        {'suggest.3q36hwtauzym': 'Add: “Y”', 'suggest.v9kxi7why0cu': 'Delete: “9”'}
del_ins  gap=2 n=2 [{'createdSuggestionIds': ['suggest.8fa2cfiquri3']}]        {'suggest.8fa2cfiquri3': 'Add: “Y”', 'suggest.vcq43y3k4hpj': 'Delete: “9”'}
del_del  gap=0 n=1 [{'updatedSummarySuggestionIds': ['suggest.s7b2ty28syn7']}] {'suggest.s7b2ty28syn7': 'Delete: “9A”'}
del_del  gap=1 n=2 [{'createdSuggestionIds': ['suggest.oubuirb1uech']}]        {'suggest.v1153abgz2t8': 'Delete: “9”', 'suggest.oubuirb1uech': 'Delete: “B”'}
del_del  gap=2 n=2 [{'createdSuggestionIds': ['suggest.nf2f52ui5cpb']}]        {'suggest.nf2f52ui5cpb': 'Delete: “C”', 'suggest.an0tc31tswmr': 'Delete: “9”'}
```

**`MERGE_TOLERANCE = 0` is correct, and it does not differ by edit kind.** The
mock's hardcoded guess was right; §13.2's suspicion that insert-then-insert and
insert-then-delete might differ is **not** borne out.

### The gap-0 exchange in full

```json
POST …/documents/1nD_…/batchUpdate
{
  "requests": [{"insertText": {"location": {"index": 10}, "text": "X"}}],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```json
{
  "replies": [{}],
  "documentId": "1nD_KOW1cM0Js4Noao7_fbDNgkPRK46x6BnI4GF357Yk",
  "suggestionResponses": [{"createdSuggestionIds": ["suggest.e79qrxxlopy"]}],
  "commentUpdateState": "ALL_SAVED"
}
```

Read back (`suggestionsViewMode=SUGGESTIONS_INLINE&commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED&includeTabsContent=true`):

```
runs: (1,10,'012345678',[],[]), (10,11,'X',['suggest.e79qrxxlopy'],[]), (11,75,'9ABC…',[],[])
suggestions[0].summaryText = 'Add: “X”'
```

Second batch, at index 11 — exactly where the pending insertion ends:

```json
{
  "requests": [{"insertText": {"location": {"index": 11}, "text": "Y"}}],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```json
{
  "replies": [{}],
  "suggestionResponses": [{"updatedSummarySuggestionIds": ["suggest.e79qrxxlopy"]}],
  "commentUpdateState": "ALL_SAVED"
}
```

```
runs: (1,10,'012345678',[],[]), (10,12,'XY',['suggest.e79qrxxlopy'],[]), (12,76,'9ABC…',[],[])
suggestions[0].summaryText = 'Add: “XY”'   (one card, the ORIGINAL id, updateTime bumped)
```

### Controls run, so the number means what it says

- **Not a time window.** The spec's §5.4 talks about a "coalescing window", so
  the same gap-0 insert-then-insert was repeated with **130 seconds** between
  the two batches. Still one card (`Add: “XY”`, `updatedSummarySuggestionIds`).
  Absorption is a property of *distance*, not of recency.
- **Symmetric.** A second insertion at the existing card's **start** index
  (i.e. to its left) is absorbed just the same: `Add: “X”` →
  `Add: “WX”`, one card, same id.
- **Kind-agnostic.** A deletion that swallows one's own pending insertion is
  absorbed too — `Add: “XX”` at `[10,12)` became `Delete: “78XX9AB”` at
  `[8,15)` after `deleteContentRange [8,15)`, still one card, still the same
  id. (The `XX` characters now carry the same id in *both* the insertion and
  deletion sets — spec §11.2 L4's self-cancelling span, computed by prod.)

### Still unknown

- **Cross-author.** Only one Google account is available to this project, so
  "merge must be same-author" (spec §6) remains **untested against prod**. Every
  observation above is single-author.
- Gaps larger than 2 were not tested; monotonicity is assumed, not measured.
  Given that gap 1 already keeps two cards, the threshold cannot be higher.

---

## Q2 — threads on merge: **RESOLVED, the question is vacuous on prod**

Spec §10 gives merge the policy "Docs *drops* the absorbed thread (apparent);
*recommended*: migrate". `mockdocs` implements migrate. Neither column
describes the live API, because **the live API never absorbs a suggestion that
has a thread — it never absorbs a suggestion at all.**

### The construction that was supposed to force it

Document `alpha bravo charlie delta echo foxtrot`. Delete `bravo` `[7,12)` →
`S1`; delete `delta` `[21,26)` → `S2`; put a distinct, identifiable reply on
each; then delete `[7,26)`, which **overlaps both**.

```json
POST addCommentReply
{"requests": [{"addCommentReply": {"suggestionId": "suggest.k4fbvxmszvkh",
                                   "post": {"content": "ALPHA-reply-on-S1"}}}]}
```
```json
{"replies": [{"addCommentReply": {"post": {
   "postId": "AAACEYWPA9U", "content": "ALPHA-reply-on-S1",
   "author": {"displayName": "Jonathan Moregård", "me": true,
              "user": "users/108544169371250993163"},
   "createTime": "2026-08-01T22:17:00.500Z"}}}],
 "commentUpdateState": "ALL_SAVED"}
```

The spanning deletion:

```json
{
  "requests": [{"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 26}}}],
  "writeControl": {"writeMode": "SUGGEST"}
}
```
```json
{
  "replies": [{}],
  "suggestionResponses": [{"updatedSummarySuggestionIds": ["suggest.k4fbvxmszvkh"]}],
  "commentUpdateState": "ALL_SAVED"
}
```

### Result: **both threads survive, on their own suggestions**

```
spans after the spanning delete:
  {'suggest.k4fbvxmszvkh': (7, 26), 'suggest.99go7nu36559': (21, 26)}
absorbed: set()          # nothing was destroyed
```

```
suggest.k4fbvxmszvkh  summaryText 'Delete: “bravo charlie delta”'
                      replies[0].content 'ALPHA-reply-on-S1'
suggest.99go7nu36559  summaryText 'Delete: “delta”'
                      replies[0].content 'BETA-reply-on-S2'
```

Two cards. Two threads. The characters in `[21,26)` simply carry **two
deletion ids at once**. So the survivor does not "carry both threads' posts,
one, or neither" — the premise (an absorbed suggestion) does not occur.

### Confirmed by the converse construction too

Two deletions **one character apart** (`bravo [7,12)`, `charlie [13,20)`, gap
1 → two cards by Q1), then a third deletion consuming exactly the separating
space `[12,13)`:

```json
{"requests": [{"deleteContentRange": {"range": {"startIndex": 12, "endIndex": 13}}}],
 "writeControl": {"writeMode": "SUGGEST"}}
->
{"suggestionResponses": [{"updatedSummarySuggestionIds": ["suggest.sy5egily0sca"]}]}
```

```
spans after gap-fill: {'suggest.ise6pmrti3tk': (7, 12), 'suggest.sy5egily0sca': (12, 20)}
labels:               {'suggest.ise6pmrti3tk': 'Delete: “bravo”',
                       'suggest.sy5egily0sca': 'Delete: “charlie”'}
spans after +20 s:    identical  (not an eventually-consistent view of a pending merge)
```

Two same-author, same-kind deletions now **touching** — `[7,12)` and
`[12,20)` — and they stay two cards. Absorption is evaluated once, when the
edit arrives; it is never re-evaluated over the existing set. Repeated with the
creation order reversed (charlie first), same outcome.

### Sub-finding: **which** neighbour absorbs is nondeterministic

When a new edit touches two existing cards, the API picks one — and the pick is
not stable. Five identical constructions of the spanning-deletion case, run
back to back:

```
run 0 replies=False -> absorbed by bravo   (bravo (7,26) 'Delete: “bravo charlie delta”', delta (21,26) 'Delete: “delta”')
run 1 replies=False -> absorbed by bravo
run 2 replies=False -> absorbed by delta   (bravo (7,12) 'Delete: “bravo”',              delta (7,26)  'Delete: “bravo charlie delta”')
run 3 replies=True  -> absorbed by delta
run 4 replies=True  -> absorbed by bravo
```

Three left, two right, independent of whether replies were attached. **An agent
must not predict which card grows.** `e2e/test_merge_semantics.py` therefore
asserts the invariant (exactly one card covers the spanned range, the other is
untouched, both threads intact) and never the identity.

---

## Q3 — rename / reporting of the pre-merge id: **RESOLVED**

| sub-question | answer | evidence |
|---|---|---|
| what id does the survivor carry — an original, or a new one? | **an original** — the *pre-existing* suggestion's id | every gap-0 case above; `Add: “X”` → `Add: “XY”` keeps `suggest.e79qrxxlopy` |
| does the response report anything about the absorbed id? | there **is no absorbed id**. The absorbed *write* reports **no `createdSuggestionIds` at all**; it reports the surviving id under `updatedSummarySuggestionIds` | `{"suggestionResponses": [{"updatedSummarySuggestionIds": ["suggest.e79qrxxlopy"]}]}` |
| is a pre-merge id still resolvable afterwards? | **not applicable** — no id is ever retired by a merge. Probes designed to catch a retired id found none to test | `absorbed: set()` in the spanning case; the swallowed-insertion case kept its id too |

So `mockdocs/adapter.py`'s `_resolve_merges` — which rewrites `createdSuggestionIds`
so it never names an id the mock's own merge destroyed — is compensating for a
mock-only problem. Prod has nothing to rewrite, because prod never mints the id
in the first place.

`HANDOVER.md` §4.5's existing sentence ("A merged write returns **no
`createdSuggestionIds`**") is **confirmed**, and can now be strengthened: it
also returns the surviving id under `updatedSummarySuggestionIds`, and that id
is the one the *earlier* write created.

Note the consistency with the documented SUGGEST-replacement shape (§4.5,
second bullet): request 0 (`deleteContentRange`) reports the id under
`createdSuggestionIds`, request 1 (`insertText`) reports the **same** id under
`updatedSummarySuggestionIds`. That is exactly absorption applied inside one
batch — the insert abuts the deletion the same batch just created, so it joins
it.

---

## What changes in the repo

### Added

- **`e2e/test_merge_semantics.py`** — 5 `e2e_preview` tests encoding everything
  above that is stable. They use the existing `make_scratch_doc` fixture, so
  teardown is the suite's normal Drive trash + `test_zz_teardown_audit.py`
  re-audit.
  - `test_a_touching_same_author_edit_joins_the_existing_card` (Q1 gap 0 + Q3)
  - `test_a_one_character_gap_keeps_two_cards` (Q1 gap 1 — the threshold)
  - `test_a_deletion_touching_a_pending_insertion_joins_it` (Q1, cross-kind)
  - `test_an_edit_spanning_two_pending_cards_destroys_neither_card_nor_thread` (Q2)
  - `test_two_cards_pushed_into_contact_do_not_collapse` (Q2 converse)
- **`docs/findings/merge.md`** — this file.

### Changed

- `mockdocs/model.py` — `MERGE_TOLERANCE`'s comment now says the value is
  **measured**, not guessed, and the thread-migration comment says what prod
  does instead of citing §13.3 as open.
- `mockdocs/adapter.py` — `_resolve_merges`' `UNCERTAIN` block replaced with the
  measured answer.
- `docs/plans/2026-07-30-suggestion-mock-spec.md` §14 — the stale `UNCERTAIN`
  merge bullet and the "§13.3 stays open" bullet, corrected. Only §14 (the
  implementation's own addendum) is touched; §1–13 is the owner's spec verbatim
  and is left alone.

### Deliberately NOT changed: the mock's merge semantics

`mockdocs` diverges from prod in three ways that all follow from spec §6:

| | `mockdocs` | prod |
|---|---|---|
| a new edit touching an existing same-author card | creates a suggestion, then merges | never creates one; extends the existing card |
| survivor selection | greatest `touched_at` — i.e. the **new** id wins | the **pre-existing** id wins |
| merging runs to a **fixpoint** | yes: an edit touching two cards collapses all three into one | no: it joins exactly one, and the other survives untouched forever |
| threads | absorbed thread migrated onto the survivor | no thread is ever absorbed |

I implemented the prod-faithful version (survivor = the pre-existing neighbour;
absorb at most one; no fixpoint) and measured the blast radius:

```
uv run pytest tests/mockdocs tests/mockdocs_concurrency tests/llmux \
              tests/llmux_runner tests/gdocs_preview -q
51 failed, 791 passed, 1 skipped
```

The failures are not incidental. They land in
`tests/llmux/test_scenario_corpus.py`, `test_stress_corpus.py`,
`test_scenario_traps.py` and `tests/llmux_runner/test_scenarios.py` — i.e. the
**checked-in llmux scenario ground truth**, which is computed from the model and
would all have to be regenerated, invalidating every recorded benchmark
comparison — plus `tests/mockdocs/test_model_properties.py` (spec §11.3 L7–L10
are stated over §6's merge) and the `ix-merge-absorb` interference scenario,
whose entire premise ("the id the agent is holding gets absorbed by a merge") is
now known to be **unreachable on prod**.

That is squarely the "would cascade into many test changes" case, so the change
was **reverted** and the mock left alone with comments pointing here. The mock
remains faithful to its written spec (§6), which is what it is for; this file is
the record of where that spec and prod part company.

If someone does want to close the gap later, the order is: (1) rewrite §6 in the
spec, (2) change `_merge_around`, (3) regenerate `llmux/scenarios/generated`,
`llmux/scenarios/stress` and `llmux/interference` ground truth, (4) retire or
re-found `ix-merge-absorb`. It is a project, not an edit.

---

## Reproducing

The probes were throwaway scripts run out of `/tmp` against the real API with
the server's own credentials (`e2e.gating.resolve_credentials_dir()` →
`~/.google_workspace_mcp/credentials/jonathan@klaffat.com.json`). Every scratch
document was trashed in a `finally:` block; none survive.

The permanent, re-runnable form is:

```bash
uv run pytest e2e/test_merge_semantics.py -q     # 5 tests, ~45 s
```

**Quota note.** The Docs API allows **60 write requests per minute per user**
(`WriteRequestsPerMinutePerUser`), and the probe sweeps above hit it repeatedly:

```
HttpError 429 … "Quota exceeded for quota metric 'Quota group for write operations'
and limit 'Quota group for write operations per minute per user' …
{'quota_limit': 'WriteRequestsPerMinutePerUser', 'quota_limit_value': '60'}"
```

The 5 new tests cost ~24 writes. Measured from a cold start:

```
uv run pytest e2e -q --ignore=e2e/test_merge_semantics.py   36 passed, 1 skipped, 4m26s
uv run pytest e2e -q                                        41 passed, 1 skipped, 5m22s
```

so the suite still fits inside the quota with them. It does **not** fit if a
run starts while a previous run's or a probe script's writes are still inside
the same rolling minute — that is what produced a `RATE_LIMIT_EXCEEDED`
cascade during this work, and the fix is to space the runs, not to retry
harder.
