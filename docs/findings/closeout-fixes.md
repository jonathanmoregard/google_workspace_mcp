# Close-out review: what reproduced, and what changed

Two independent reviewers (Opus and GPT-5.5) read the combined
`integration/empirics` diff. Four findings came back. **Every one was
reproduced with a failing test before anything was changed** — reviewer
agreement raises the priority of reproducing a claim, it does not stand in
for the reproduction. All four reproduced; nothing here is a fix for a
finding that turned out not to exist.

All four are the same rule applied in four places, and it is the rule the
package exists for: **no response asserts more than its evidence supports,
and nothing is silently omitted.**

---

## 1. The write path's pending counts were still the modelled set

*Reported by both reviewers, HIGH.* **Reproduced.**

### The claim

`_verify_resolution` and `_verify_suggest` built

```python
"pending_suggestion_count": len(read.records),
"pending_suggestion_ids": sorted(read.records),
```

while `_PostWriteRead.pending_state` beside them had been widened (the
`docs/findings/coverage.md` work) to also consult `pending_thread_ids` — the
OPEN threads the API itself reports, including the kinds `analysis.py` does
not model: paragraph style, bullets, table row/cell style. The two answers
came from different sets inside one response.

### The reproduction

`tests/gdocs_preview/test_write_tools.py::TestThePendingCountsAccountForWhatTheApiCallsPending`
— a `reject` against a document whose only pending card is an alignment
suggestion, on a **complete** preview read:

```
E  AssertionError: {'expected_text': None, 'matches_expectation': False,
E   'notes': ["suggestion 'suggest.para1' is STILL in the document's ..."],
E   'pending_suggestion_count': 0, ...}
E  assert 'suggest.para1' in set()
```

`still_pending: true` printed beside `pending_suggestion_count: 0` and
`pending_suggestion_ids: []`, with **no** `pending_suggestions_are_partial` to
qualify them — because the read really was complete. A response contradicting
itself about a customer's document, on the one destructive path this package
has.

### The change

The read tools' answer, reused rather than re-derived, so the two surfaces
cannot drift apart again: `_PostWriteRead` now keeps the raw `threads`, and
both verification builders end with `_attach_unreported_pending`, which
delegates to `review_page.attach_unreported` — the same function
`list_document_suggestions` and `get_doc_review_view` call.

- `pending_suggestion_count` / `pending_suggestion_ids` keep their meaning
  (what this tool models) and the docstrings now say so outright.
- `unreported_suggestion_count` + `unreported_suggestions` (id, Google's own
  `summary_text` label, author, status) + `notice_unreported` sit beside them.
- The document's pending set is the two together, so **an id reported as
  `still_pending` is always in one list or the other** — that is the
  reconciliation the finding asked for.
- On a degraded read the thread array does not exist, so the number is
  refused: `null` + `unreported_suggestions_unavailable: "read_degraded"`.
  On the two unverified paths (`verify=false`, or the post-write read failed)
  it is `null` + `not_verified`, present-and-null like every other documented
  key there.

`Returns` on both `suggest_doc_edit` and `manage_document_suggestion` document
the new keys and say a review is finished only when **both** numbers are zero.

---

## 2. A false statement about the read's coverage

*Reported by Opus, MEDIUM.* **Reproduced.**

### The claim

`_PostWriteRead.absences()` split ids two ways — `state is False` was "gone",
**everything else** was "this read cannot say". But `pending_state` answers
`True` for an id the API still lists as OPEN while the analysis layer no
longer describes it, and that `True` fell into the second pile.
`_collateral_unavailable_note` then asserted *"that read did not cover the
whole document"*.

### The reproduction

`tests/gdocs_preview/test_write_tools.py::TestStillPresentIsNotTheSameFactAsCouldNotLook::test_a_complete_read_is_never_described_as_partial`:

```
E  AssertionError: ... 'suggest.para1' was listed before this accept and is
E  absent from the post-write read, but that read did not cover the whole
E  document (read_source='preview_threads', which carries no tab ids at
E  all), so whether this write removed anything else is UNKNOWN.
```

`read_source='preview_threads'` with `complete=True`. The sentence is false
about the read, and it is the sentence explaining why a claim about the
document is being withheld.

### The change

`absences()` returns three lists — `(gone, still_pending, unattested)` — and
the third is now reachable only from `complete is False`, which is what makes
its sentence true. The middle list gets its own key and its own sentence:

- `still_pending_unmodelled_suggestion_ids` — ids that were in the last
  listing, are no longer described by this tool, and are **still pending per
  the API**. They were not removed, so they are deliberately absent from
  `also_removed_suggestion_ids`, and `also_removed_suggestion_ids_unavailable`
  is not set for them.
- `_unmodelled_still_pending_note` says exactly that, names the kinds that
  leave no content mark, and points at `unreported_suggestions` and at
  `manage_document_suggestion`, which resolves them by id regardless.

"Still present but unmodelled" and "we could not look" are opposite facts
about the read and no longer share a sentence.

---

## 3. `e2e/quota.py` — three races and an inflated sleep

*Split verdict: GPT-5.5 HIGH, Opus LOW (bounded by session count, absorbed by
the 50/60 headroom).* Opus is right that it is bounded. It was cheap to make
correct, so it was made correct. **All three reproduced.**

### 3a. `acquire()` checked capacity outside the lock

`tests/e2e_harness/test_quota.py::test_two_sessions_cannot_both_take_the_last_slot`
— two threads, each with its own `WindowStore` on one shared path (two
`open()` calls are independent open file descriptions, so `flock` really
contends), a barrier pinning both at their first clock read, budget 1:

```
E  AssertionError: {'a': 0.0, 'b': 0.0}
E  assert 0.0 > 0.0
```

Both sessions admitted a full-budget write against a budget of one.

**Fix:** `WindowStore.reserve(now, cost, limit=, force=)` does the whole
check-and-reserve under one exclusive `flock` and returns either `(0.0,
window)` having recorded the spend, or `(delay, window)` having recorded
nothing. `WritePacer.acquire` loops over that instead of
`seconds_until_capacity` + `append`, so the capacity it was told about is the
capacity it gets. `seconds_until_capacity` survives as what it always
actually was — the retry backoff's advisory `floor` — and its docstring now
says so. `append` is `reserve(..., force=True)`. The "out of patience after 24
passes, spend anyway" behaviour is preserved, and still recorded.

### 3b. `WindowStore.load()` read without a lock

`append` truncates and then writes; between those syscalls the file is empty.
`tests/e2e_harness/test_quota.py::test_a_reader_never_sees_a_half_written_window`
holds an exclusive lock across a truncate + 0.25 s + write and calls `load()`
in the middle: the old reader returned `[]` for a full window.

**Fix:** `_read()` takes `LOCK_SH`, so a reader waits for a half-written file
instead of observing one.

### 3c. `backoff_delay` jittered after the cap, and inflated `Retry-After`

`min(delay, cap) * (1 + jitter*rand())` multiplies straight through the
ceiling — attempt 10 at maximum jitter slept `70 * 1.25 = 87.5` s — and it
did the same to a server-supplied `Retry-After`, turning a 30 s hint into a
37.5 s sleep. Two tests, both failing before the change:
`test_jitter_never_pushes_the_delay_past_the_cap`,
`test_a_server_supplied_retry_after_is_never_inflated`.

**Fix:** the cap is applied last, and `Retry-After` is returned unjittered.
The pacing `floor` may still hold a call longer — that is our own window, not
a reinterpretation of the server's.

---

## 4. Notice prose pointed at a key that tool does not emit

*Reported by Opus, LOW.* **Reproduced.**

`review_page.unreported_notice` said the cards "are NOT included in
`suggestion_count`" and told the agent not to call a review complete "on
`suggestion_count` alone". One string, three tools:
`get_doc_review_view` has no `suggestion_count` at all, and after fix 1 the
write tools' counts are called `pending_suggestion_count`.

`tests/gdocs_preview/test_curated_tools.py::TestSuggestionsThisLayerDoesNotModel::test_the_notice_names_no_field_the_emitting_tool_lacks`
asserts the premise (`"suggestion_count" not in view`) and then the absence of
the backticked name; before the change it failed on the full notice text.

**Fix:** the sentence talks about the response rather than about a field — "NOT
counted by any other suggestion count in this response" / "Do NOT report a
review as complete on this response's other suggestion counts alone while
`unreported_suggestion_count` is non-zero". `unreported_suggestion_count` is
the one field it names, and all three tools emit it.

---

## Reported, examined, and left alone

- **`preview_status.py:174` marker precedence.** Confirmed as described: a
  string matching both families hits `_UNKNOWN_FIELD_MARKERS` first and comes
  back `unavailable` / `not_enrolled`. That is the conservative direction
  (tell a caller to check enrollment rather than tell a non-enrolled caller
  the surface is fine), and the two observed grammars are disjoint
  (`docs/findings/errors-and-discovery.md`). No change.
- **`quota.py` `rate_limited_result_text` sniffing success bodies.** Confirmed
  as described: the in-band branch is gated on `cost > 0`, and a call that
  keeps matching it ends in `WriteQuotaExhausted`, which is raised, never
  skipped. It fails loud; it cannot silently pass. No change.

---

## Gate

| gate | before | after |
|---|---|---|
| `uv run pytest tests/ -q` | 2512 passed, 3 skipped | **2529 passed, 3 skipped** |
| `uv run ruff check .` | clean | clean |
| `uv run ruff format --check .` | 316 files formatted | 316 files formatted |
| `uv run python -m llmux.scenarios.validate` | 17/17 | **17/17** |
| `uv run pytest e2e -q` (real API) | 79 passed, 1 skipped | **79 passed, 1 skipped** |

17 new tests, all of which fail against the pre-change tree:

| file | class / test | finding |
|---|---|---|
| `tests/gdocs_preview/test_write_tools.py` | `TestThePendingCountsAccountForWhatTheApiCallsPending` (8) | 1 |
| `tests/gdocs_preview/test_write_tools.py` | `TestStillPresentIsNotTheSameFactAsCouldNotLook` (4) | 2 |
| `tests/e2e_harness/test_quota.py` | `test_two_sessions_cannot_both_take_the_last_slot` | 3a |
| `tests/e2e_harness/test_quota.py` | `test_a_reader_never_sees_a_half_written_window` | 3b |
| `tests/e2e_harness/test_quota.py` | `test_jitter_never_pushes_the_delay_past_the_cap` | 3c |
| `tests/e2e_harness/test_quota.py` | `test_a_server_supplied_retry_after_is_never_inflated` | 3c |
| `tests/gdocs_preview/test_curated_tools.py` | `test_the_notice_names_no_field_the_emitting_tool_lacks` | 4 |

One existing assertion was inverted rather than deleted:
`tests/gdocs_preview/test_review_page.py::TestAttachUnreported::test_an_unmodelled_card_is_counted_listed_and_narrated`
asserted `` "`suggestion_count`" in notice `` — it was pinning the bug in
place, and now asserts the opposite plus the guidance the notice still has to
carry.
