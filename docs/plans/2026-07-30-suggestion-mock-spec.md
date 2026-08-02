# Suggestion mode — mockup specification

A reimplementation spec for Google Docs–style suggesting mode, derived from observed
behaviour. Written to be implementable without further reverse-engineering, and to make
the places where Docs is arguably wrong explicit rather than accidental.

---

## 1. Scope

**In scope (v1):** inline text insertion, deletion, and replacement as suggestions;
multi-author marks; per-suggestion comment threads; accept/reject including bulk;
suggestion merging; undo.

**Out of scope (v1):** suggested formatting changes; block-structural suggestions
(paragraph split/merge, list level changes, table row insert/delete); real-time
concurrent editing. See §12 for why these are cut and what they'd cost.

---

## 2. Data model

Two layers. They are separate because they have different lifetimes: marks are rewritten
freely, suggestion identity must persist across those rewrites (this is what keeps a
comment thread attached while its range changes shape).

```
type SuggestionId = string
type AuthorId     = string

interface Suggestion {
  id:        SuggestionId
  author:    AuthorId
  createdAt: Timestamp
  touchedAt: Timestamp        // last edit that modified this suggestion's marks
  thread:    Comment[]
}

interface Char {
  cp:  string                 // one grapheme cluster
  ins: Set<SuggestionId>      // "exists only if all of these are accepted"
  del: Set<SuggestionId>      // "removed if any of these is accepted"
}

interface Doc {
  chars:    Char[]            // '\n' separates blocks; block attrs in a sidecar
  registry: Map<SuggestionId, Suggestion>
}
```

Base (already-accepted) text has `ins = ∅ ∧ del = ∅`. Store `ins`/`del` as sorted arrays
if you want cheap structural equality for run-coalescing in the renderer.

The two sets have genuinely different logic, and getting this backwards is the single
most likely source of bugs:

| | semantics | dies when |
|---|---|---|
| `ins` | **conjunctive** — char exists only if *every* insertion mark is accepted | *any* is rejected |
| `del` | **disjunctive** — char survives only if *every* deletion mark is rejected | *any* is accepted |

---

## 3. The three projections

Every view is a filter over the same array. No separate document copies.

```
original(D) = [c | c ← D.chars, c.ins = ∅]     // reject everything
final(D)    = [c | c ← D.chars, c.del = ∅]     // accept everything
display(D)  = D.chars                           // all of it, styled per §4
```

A char with both sets non-empty appears in **neither** extreme view. That is correct and
intended — it is text that was proposed and then withdrawn.

---

## 4. Rendering

Four states, total and mutually exclusive:

| `ins` | `del` | render |
|---|---|---|
| ∅ | ∅ | normal |
| non-∅ | ∅ | underline, author colour |
| ∅ | non-∅ | strikethrough, author colour |
| non-∅ | non-∅ | underline **and** strikethrough, author colour |

Coalesce adjacent chars with identical `(ins, del)` into one styled run before painting.

**Colour precedence is unresolved for cross-author runs** — see §13.1. Until tested,
use the colour of the most recently applied mark and keep the choice behind one function
`runColour(char): AuthorId` so it's a one-line change.

---

## 5. Edit operations

All operations take `(Doc, Selection, AuthorId)` and return a new `Doc`. Every one of them
ends by running §6 (merge) and then §11.1 (garbage collection), in that order.

### 5.1 Insert text at a collapsed cursor

Create chars with `ins = {S}`, `del = ∅`, where `S` is the merge target from §6 or a fresh
suggestion. Inherit `del` from the character to the left if that char is itself struck —
otherwise typing into the middle of a deleted region produces text that survives a
deletion the user thinks they made.

If the cursor sits inside another author's pending insertion `T`, the new chars get
`ins = {S, T}`. This is real: the text is meaningless if `T` is rejected, and the
conjunctive rule in §2 handles it automatically.

### 5.2 Delete a selection

For every char in range: add `S` to `del`. Do **not** remove characters, and do **not**
special-case chars that already have `S` in `ins` — those become the both-marks state.

Confirmed against Docs: adding `"ular"` then deleting `"popul"` leaves `"ul"` rendered in
the author's colour with a strikethrough over it.

### 5.3 Replace (type over a selection)

Not atomic. Decompose into 5.2 then 5.1 at the selection start. The two halves share one
suggestion id via §6, which is why they present as a single card.

### 5.4 Backspace inside an active typing burst

If the char to the left has `ins = {S}` where `S` is the current burst's suggestion and no
other author's marks, remove the char outright rather than adding a deletion mark. This is
the one place where deletion is destructive, and it exists so that fixing a typo mid-word
doesn't litter the document with self-cancelling runs. Bound it by the same coalescing
window as §6 — outside the window, fall through to 5.2. Whether Docs draws the line
exactly here is untested (§13.4); this behaviour is defensible on its own merits.

---

## 6. Merge

After any edit, if the touched range abuts or overlaps an existing suggestion by the
**same author**, absorb the smaller into the survivor.

```
mergePredicate(S, T) =
     S.author == T.author
  && gap(range(S), range(T)) <= MERGE_TOLERANCE     // in chars; start with 0
```

```
merge(D, survivor, absorbed):
  for c in D.chars:
    if absorbed ∈ c.ins: c.ins := c.ins \ {absorbed} ∪ {survivor}
    if absorbed ∈ c.del: c.del := c.del \ {absorbed} ∪ {survivor}
  survivor.thread    := survivor.thread ++ absorbed.thread    // policy, see §10
  survivor.touchedAt := now()
  registry.delete(absorbed)
```

**Survivor selection:** the candidate with the greatest `touchedAt`. This reproduces the
observed behaviour where a select-all deletion absorbs four prior word deletions and
presents as one card.

**Merge must be same-author.** Cross-author merging would let one person's single
accept/reject silently dispose of another person's proposal.

Merge is destructive and not invertible from its own output — §9 is a direct consequence.

---

## 7. Resolve

```
accept(D, S):
  D.chars := [c | c ← D.chars, S ∉ c.del]
  for c in D.chars: c.ins := c.ins \ {S}
  registry.delete(S)

reject(D, S):
  D.chars := [c | c ← D.chars, S ∉ c.ins]
  for c in D.chars: c.del := c.del \ {S}
  registry.delete(S)
```

Note the deliberate asymmetry in which set is filtered on versus stripped. Both operations
remove a char that has `S` in *both* sets, which is the correct outcome — `"ul"` should not
survive either decision.

`acceptAll` and `rejectAll` are folds. By **L3** the fold order is irrelevant, so they need
no special implementation.

Permissions: commenter-level access may create suggestions and post to threads; only
editor-level may accept or reject.

---

## 8. Cards and labels

A card is rendered per live suggestion, anchored to the first char carrying its id.

The label is a **pure function of the rendering**, not of the marks:

```
struck(S) = [c | c ← D.chars, S ∈ c.del]                 // everything with a strikethrough
added(S)  = [c | c ← D.chars, S ∈ c.ins, S ∉ c.del]      // underlined and not struck

label(S) | added(S)  = [] = Delete:  “{flat(struck)}”
         | struck(S) = [] = Add:     “{flat(added)}”
         | otherwise      = Replace: “{flat(struck)}” with “{flat(added)}”
```

**Grammar verified against prod 2026-07-30** (`SuggestionThread.summaryText`
from the live Docs API, enrolled account): `Add: “Say”`, `Delete: “brave”`,
`Replace: “brave” with “bold”`. Google uses **typographic** quotes
(U+201C/U+201D); this spec originally guessed ASCII `"` and lost — prod is the
oracle, and `mockdocs/model.py` was changed to match. See
`docs/preview-api-reference.md` § "summaryText grammar".

The asymmetry is load-bearing: the struck side shows everything being removed, the added
side shows only what will actually survive. This is what produces
`Replace: “popul” with “ar”` rather than `... with “ular”`.

`flat` normalises for single-line display: block boundaries → one space, list markers
dropped, collapse whitespace runs, truncate at ~60 chars with a trailing ellipsis.

**Grouping is entirely a view concern.** Cards map 1:1 to registry entries; there is no
second grouping pass. If cards feel too granular, tune `MERGE_TOLERANCE` in §6 — do not
add display-layer clustering, or the card the user clicks will stop corresponding to the
suggestion that gets resolved.

---

## 9. Undo

**Snapshot `(chars, registry)` jointly, per user action.** Do not implement undo by
inverting mark operations.

Because merge destroys the absorbed suggestion, the post-merge state does not contain
enough information to reconstruct the pre-merge decomposition. Docs demonstrates the
failure mode: make a small deletion, overlap it with a larger one, undo — and both are
gone, because the small one ceased to exist at merge time and the undo stack has no record
of it.

A joint snapshot fixes this for free: undo restores the registry, so the smaller
suggestion and its thread come back. **This is a deliberate divergence from Docs in favour
of the correct behaviour.** Flag it if fidelity matters more to you than correctness.

---

## 10. Comment threads

Threads anchor to `SuggestionId`, never to a character range. Thread survival is therefore
exactly suggestion survival, which is what lets a comment ride from `Add: "ular"` through
`Replace: "popul" with "ar"` to `Delete: "Greeting and Help…"` untouched.

Every operation that destroys a suggestion needs an explicit thread policy:

| event | Docs | recommended |
|---|---|---|
| merge | drops absorbed thread (apparent) | **migrate** — concatenate onto survivor, ordered by `createdAt` |
| accept / reject | drops | drop, but surface in a resolved-comments log |
| GC (§11.1) | drops | drop; warn in dev builds, it usually indicates a bug |

Silently destroying a colleague's comment because someone extended a selection over it is
hard to defend. Migration costs one array concat.

---

## 11. Invariants and algebraic laws

These are written to be run as property tests. Generate a random base document plus a
random sequence of edit operations from §5, then assert.

### 11.1 Structural invariants (check after every operation)

- **I1 — No orphan marks.** `∀c. ∀S ∈ c.ins ∪ c.del. S ∈ registry`
- **I2 — No empty suggestions.** `∀S ∈ registry. ∃c. S ∈ c.ins ∪ c.del`
  Violation is not an error — it is the **garbage-collection trigger**. A suggestion whose
  last marked char is removed must be deleted from the registry, with §10's thread policy.
- **I3 — Colour determinism.** `runColour` is a pure function of the char; no dependence on
  render order or scroll position.
- **I4 — Render totality.** Every char matches exactly one row of §4's table.

### 11.2 Resolution laws

- **L1 — Extremes.**
  `foldr accept D (keys registry) ≡ final(D)` and `foldr reject D (keys registry) ≡ original(D)`,
  each yielding a document with no marks and an empty registry.

- **L2 — Resolution is destructive-idempotent.** `accept(S) ∘ accept(S) ≡ accept(S)`; the
  second application is a no-op because `S` has left the registry. Same for `reject`.

- **L3 — Resolution commutes.** For distinct `S, T` and any `f, g ∈ {accept, reject}`:
  ```
  f(S) ∘ g(T) ≡ g(T) ∘ f(S)
  ```
  This is the most valuable law in the spec. It means the order the user clicks in cannot
  matter, bulk operations need no ordering logic, and a resolution queue can be reordered
  freely. Test it exhaustively over small documents — if it ever fails, the cause is
  almost always an operation reading `chars` after mutating it in the same pass.

- **L4 — Self-cancelling spans.** If `S ∈ c.ins ∧ S ∈ c.del` then
  `c ∉ original(D) ∧ c ∉ final(D)`, and both `accept(S)` and `reject(S)` remove `c`.
  (The `"ul"` case.)

- **L5 — Survival.** Once every suggestion is resolved, a char survives iff every
  suggestion in its `ins` was accepted and every suggestion in its `del` was rejected.
  Death is the dual: any `ins` rejected *or* any `del` accepted.

- **L6 — Original is mark-free and stable.** `original(D)` contains no marks, and
  `original(original(D)) ≡ original(D)`. Likewise for `final`.

### 11.3 Merge laws

- **L7 — Merge preserves both extremes.**
  ```
  original(merge(D, s, a)) ≡ original(D)
  final(merge(D, s, a))    ≡ final(D)
  ```
  This is the correctness condition on merging. Merge may change *granularity of choice*;
  it must never change *content*. Property-test it directly — it catches essentially every
  merge bug.

- **L8 — Merge coarsens the outcome lattice.** With `n` live suggestions there are up to
  `2ⁿ` reachable final documents, one per accept/reject assignment. Merging two of them
  yields `2ⁿ⁻¹`: the surviving outcomes are exactly the corners where both were accepted
  and where both were rejected. The mixed corners become unreachable.

  This is the precise statement of what merging costs, and the reason `MERGE_TOLERANCE`
  is a real product decision rather than a rendering tweak. Aggressive merging gives tidy
  cards and takes away the reviewer's ability to accept half of an edit.

- **L9 — Merge preserves rendering.** `display(merge(D, s, a))` differs from `display(D)`
  only in run colour, and not at all when both suggestions share an author — which §6
  guarantees. So merging must never cause visible reflow.

- **L10 — Merge is not invertible.** There is no `unmerge` computable from the post-state.
  Hence §9.

### 11.4 Label laws

- **L11 — Label determinism.** `label(S)` depends only on the chars marked `S`, in
  document order. It is recomputed on read, never stored — a stored label goes stale the
  moment a merge rewrites the range.
- **L12 — Label/render agreement.** The struck substring of the label equals the
  concatenation of every strikethrough-rendered char marked `S`, in order (pre-`flat`).
  Same for the added substring and underline-only chars. If these ever disagree, the label
  is being computed from marks rather than from rendering.

---

## 12. Deferred, with reasons

- **Block-structural suggestions.** A suggested paragraph split or merge is not a change to
  any character — it is a change to the tree. On a flat char array with `'\n'` you can
  fake it by marking the newline, which is why v1 uses that representation. On a block
  tree (ProseMirror, Lexical, Slate) *pending* joins and splits are the hardest problem in
  this whole domain, because the document must render in a shape that no version of it
  actually has. Budget for it separately.
- **Suggested formatting.** A third mark kind, not expressible as insert/delete: it renders
  with the new formatting already applied plus a descriptive card. Needs
  `styleChanges: Map<SuggestionId, StyleDelta>` on the char, and §7's resolve grows a
  third branch. Roughly a day once §11 passes.
- **Concurrency.** Everything above is single-writer. Under OT or CRDT the marks are fine
  — they are per-character and commutative — but merge (§6) is a global read-modify-write
  and will need to become an intention-preserving operation or move server-side.

---

## 13. Open questions

Each is a five-minute experiment in a real Doc, and each currently has a guess hardcoded.

1. **Cross-author colour precedence.** A inserts, B suggests deleting A's insertion. Is the
   both-marks run coloured for A or B? Affects `runColour` only.
2. **`MERGE_TOLERANCE`.** Binary-search it: make an edit, move the cursor `n` unchanged
   characters away, edit again, observe whether one card or two appears. Test separately
   for insert-then-insert and insert-then-delete; they may differ.
3. **Thread policy on merge.** Comment on two separate word deletions, then delete a range
   spanning both. Does the survivor card show one thread or two? Determines whether §10's
   recommendation is a divergence or a match.
4. **The backspace boundary (§5.4).** Type a word as a suggestion, wait past the coalescing
   window, then backspace one character. Does it vanish or gain a strikethrough? This
   locates the boundary between destructive and non-destructive deletion.
5. **Nested-insertion rejection.** A inserts a sentence; B types inside it; reject A. Does
   B's text disappear? The conjunctive rule in §2 says yes. Worth confirming, since it is
   the most surprising consequence of the model and the one a reviewer is most likely to
   file as a bug.

---

## 14. Adapter addendum (mock implementation notes)

Everything above §14 is the owner's spec, verbatim. This section records the decisions
taken when implementing it as `mockdocs/` — the in-memory mock backing the LLM-UX test
harness — and is the only part of this document written by the implementation.

- **Unit boundary.** `cp` = one grapheme cluster in the model (§2, as specified). The
  **API boundary is UTF-16 code units**: `mockdocs/adapter.py` converts on the way out
  (`startIndex`/`endIndex` in emitted `documents.get` payloads) and on the way in
  (`batchUpdate` ranges/locations). This mismatch is deliberate — it is what exercises
  `gdocs_preview/analysis.py`'s UTF-16 index discipline, so fixtures deliberately include
  astral-plane emoji (2 UTF-16 units, 1 grapheme) and combining sequences.
- **Skipped sections.** §5.4 (backspace-burst destructive deletion) and §9 (undo) are
  **not implemented**: both are editor-interaction concerns with no MCP tool surface, so
  no tool under test can reach them. Open question §13.4 is therefore untouched by this
  mock.
- **Merge** — *§13.2 resolved 2026-08-01 against the live API; §6's mechanism is a known
  divergence.* `MERGE_TOLERANCE = 0`, same-author only, per §6. The **tolerance value is
  confirmed**: prod joins two same-author edits that touch and keeps two cards at a gap of
  one unchanged character, identically for all four insert/delete orderings (§13.2
  suspected insert-then-insert and insert-then-delete might differ — they do not), and it
  is a distance rule rather than a coalescing window (130 s apart still joins).
  The **mechanism differs**, and knowingly so: prod does not merge existing suggestions at
  all, it **absorbs a new edit at creation time** into an abutting/overlapping same-author
  suggestion — no second id is minted, the *pre-existing* id survives, the response carries
  `updatedSummarySuggestionIds` and no `createdSuggestionIds`, and two suggestions that
  already exist stay two forever. §6 as written merges to a fixpoint with the *newest* id
  surviving. Making the mock prod-faithful failed 51 tests (mostly the checked-in llmux
  scenario ground truth, whose regeneration would invalidate the recorded benchmark
  numbers), so the mock still follows §6 and every merge is flagged (`model.merge_log`)
  so the divergence stays diffable. The `ix-merge-absorb` interference scenario was
  re-founded on absorption at creation time, so it no longer depends on §6's survivor
  selection ([`docs/findings/merge-absorb-premise.md`](../findings/merge-absorb-premise.md)).
  Raw evidence, blast-radius measurement and the decision:
  [`docs/findings/merge.md`](../findings/merge.md); permanent coverage:
  `e2e/test_merge_semantics.py`.
- **Threads on merge** — *§13.3 resolved vacuously 2026-08-01.* Migrate-on-merge, i.e.
  §10's *recommended* column, not the observed-Docs column. Prod never faces the question:
  because it never absorbs an existing suggestion, it never orphans a thread. Two threaded
  same-author deletions plus a deletion spanning both leaves **two** cards, each still
  carrying its own replies. So §10's "Docs drops the absorbed thread (apparent)" describes
  the editor, not this API, and the mock's migration is the right policy for a model that
  does absorb.
- **Colour / `runColour`.** §13.1 is unresolved and colour has no MCP surface, so
  `runColour` returns the most-recently-applied mark's author (§4's stated interim rule)
  and is exercised only by invariant I3.
- **Not-enrolled simulation.** The adapter takes a constructor flag that makes every
  preview-only request type fail with an `HttpError`-shaped 400 ("Unknown name ..."),
  so `gdocs_preview/preview_status.py`'s error classifier can be tested against both
  enrolled and non-enrolled backends. This targets open UNCERTAIN item #5 of
  `docs/preview-api-reference.md`.
- **Thread exposure in `documents.get`** — *resolved 2026-07-30 against the live API.*
  A plain `documents.get` carries **no** thread objects at all (the adapter's earlier
  guessed `suggestionThreads` key does not exist). Threads live at the top level of the
  `commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED` + `includeTabsContent=true` read, as
  `suggestions[]` / `comments[]`, with the content moved under
  `tabs[i].documentTab`. `mockdocs.adapter` now models both reads separately
  (`document_payload` vs `tabs_document_payload`) and `mockdocs.fake_services`
  enforces the API's "comments view mode requires tabs content" 400. Details:
  `docs/preview-api-reference.md` § "Reading threads".

### 14.1 Tabs and segments — an amendment to §2 (2026-07-31)

§2 gives a document **one** flat `Char` array, and §6/§11 are stated over it. That is
one coordinate space, and a real document has many: Docs numbers every
`(tabId, segmentId)` pair from its own start. The mock now models that, because the
single-array shape made a whole bug class *unrepresentable* — three consecutive review
rounds found an index emitted or compared without its `(tab, segment)` in the production
code, and every one of them was invisible to the mock-backed unit tests and to every
llmux scenario. Only the prod e2e suite could see them. A test harness that cannot
express the failure is not evidence about it.

- **`Doc.chars` becomes `Doc.segments: {(tab_id, segment_id): Segment}`**, each `Segment`
  holding its own `Char` array plus its `kind` (`body`/`header`/`footer`/`footnote`).
  The registry stays document-wide: suggestion ids and comment threads are not per-tab.
  A freshly constructed document is single-tab (`t.0`) and body-only, so everything
  above §14 still describes it exactly, and `MockDoc.chars` remains a live property onto
  that body.
- **Index bases.** A body is numbered from **1** (index 0 is that tab's leading section
  break) — in *every* tab, not only the first. A header, footer or footnote is numbered
  from its own **0**, and 0 is a writable position there (verified against the live API
  2026-07-31 by inserting at `{"index": 0, "segmentId": <headerId>}`). The API serialises
  proto3, so `startIndex: 0` is never emitted; the adapter omits it, which is what
  exercises `gdocs_preview.analysis._indexes`.
- **§6 merge is segment-local.** Two same-author suggestions in different segments or
  different tabs may report the same numbers and are still different places; `gap` is
  only defined within one segment. Merging them would be a fiction the editor cannot
  produce.
- **New invariant I5** (§11.1): *a suggestion's marks live in exactly one segment.* It is
  what a cross-segment merge would break, and it is what lets §8's `anchor` stay a single
  integer — paired, now, with the `tab_id`/`segment_id` that make it an address.
- **An omitted `tabId`/`segmentId` resolves silently to the default tab's body**, as the
  API does. That is reproduced rather than papered over: it is the footgun, and the
  llmux scenario `adversarial-header-segment` exists to make an agent fall into it.
