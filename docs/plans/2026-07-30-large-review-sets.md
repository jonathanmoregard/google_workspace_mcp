# Large review sets: what the stress corpus found, and the surface change it argues for

Written 2026-07-30 alongside `llmux/scenarios/stressgen/`. This is a
**recommendation, not an implementation**: nothing in `gdocs_preview/` is
changed by it, and it should be read by whoever owns that module next.

## The measurement

The stress corpus (`llmux/scenarios/stress/`) puts 30, 60, 90 and 120
pending suggestions on four genuine 1,500–1,800 word articles and asks an
agent to apply an ordered rule list to all of them. Reproduce the numbers
with:

```
uv run python -m llmux.scenarios.stressgen.measure --markdown
```

| cards | `list_document_suggestions` | est. tokens | `get_doc_review_view` | est. tokens | one read of each |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 30 | 23,541 chars | ~6,500 | 33,648 chars | ~9,300 | ~16,300 tokens |
| 60 | 47,545 chars | ~13,200 | 44,509 chars | ~12,400 | ~26,000 tokens |
| 90 | 70,163 chars | ~19,500 | 45,270 chars | ~12,600 | ~32,600 tokens |
| 120 | 93,443 chars | ~26,000 | 54,901 chars | ~15,300 | ~41,700 tokens |

Token figures are `characters / 3.6`; the character counts are exact.

Three facts fall out of that table.

**1. The output is linear in card count and has no cap.** Cost per card is
flat at **779–792 characters** across every tier, of which roughly 72% is
`pre_text` / `post_text` / the two context windows / per-record metadata the
caller did not ask for. There is no truncation, no
page size, and no way to ask for less. A 300-suggestion page — which is an
ordinary state for an the client article that has been through two rounds
of review — produces roughly **235 KB, about 65,000 tokens, in a single tool
response**. That is not a large response; it is a response that does not fit
in most per-message budgets and blows a meaningful fraction of a context
window when it does.

**2. There is no way to read part of the work.** The whole review surface is
three tools, and both read tools are all-or-nothing:

| tool | parameters | narrowing available |
| --- | --- | --- |
| `list_document_suggestions` | `document_id` | none |
| `get_doc_review_view` | `document_id`, `view_mode` | view mode only |
| `manage_document_suggestion` | `document_id`, `action`, `suggestion_id` | one card at a time |

An agent cannot ask for "the suggestions by dana", "the suggestions between
index 4,000 and 8,000", "the next 25", or "just the ids and authors". The
only lever is `view_mode`, which changes the rendering of the *document*,
not the size of the *suggestion list*.

**3. Re-reading is not optional, and it is expensive.** The saturated
benchmark's headline mistake class was `no_end_state_verification` — 26 of 32
runs never read the document back after writing. The right behaviour is to
re-read; at 120 cards the right behaviour costs ~26,000 tokens per check.
The tool surface currently prices correctness out of reach, and an agent that
resolves 120 cards and verifies its work three times will spend more context
on repeated reads than on the document.

## What this means for the benchmark

Everything above is about *context*, not about tool syntax, and the corpus is
built to separate the two. Grading is per card
(`llmux/scenarios/stressgen/invariants.py`): each suggestion carries an
algebraic witness derived from SPEC L5, so a run that gets 90 of 120 calls
right scores like a run that got 90 of 120 calls right. Calibration, from the
same measurement command:

| strategy | 30 | 60 | 90 | 120 |
| --- | ---: | ---: | ---: | ---: |
| intended answer | 1.000 | 1.000 | 1.000 | 1.000 |
| do nothing | 0.507 | 0.370 | 0.322 | 0.350 |
| accept everything | 0.391 | 0.598 | 0.470 | 0.605 |
| reject everything | 0.319 | 0.394 | 0.650 | 0.490 |
| right calls, never defers | 0.464 | 0.724 | 0.743 | 0.749 |
| 10 wrong calls | 0.754 | 0.858 | 0.907 | 0.930 |
| 40 wrong calls | — | 0.591 | 0.732 | 0.790 |

So a degradation curve is readable directly off the score, and the shape of
the failure (dropped cards? wrong rule? never deferred?) is readable off the
per-card failures.

## Recommended surface change

**Minimal, additive, no behaviour change for existing callers.** Four
optional parameters on `list_document_suggestions`:

```python
async def list_document_suggestions(
    service, user_google_email, document_id,
    author: Optional[str] = None,       # filter: exact author display name
    start_index: Optional[int] = None,  # filter: UTF-16 range overlap
    end_index: Optional[int] = None,
    fields: str = "full",               # "full" | "summary"
    page_size: Optional[int] = None,    # with page_token in the response
    page_token: Optional[str] = None,
) -> str
```

Priority order, by value per line of code:

1. **`fields="summary"`.** Return only `suggestion_id`, `author` (as a
   display-name string, not the nested People object), `type`,
   `summary_text`, `start_index`, `end_index` — drop `pre_text`,
   `post_text`, the two ~40-character context windows, `segment`,
   `segment_id`, `tab_id`, `in_table`, `status`, `create_time`,
   `author_source` and `replies`. Measured on this corpus, that is a
   **71.6–71.9% reduction** at every tier (**79%** if the JSON is also
   emitted compact rather than `indent=2`), taking 120 cards from ~26,000 to
   **~7,300 estimated tokens**. It is also exactly what a rule list needs:
   every predicate in all four stress tasks is decidable from those six
   fields. This alone changes the shape of the problem and is the single
   highest-value change.
2. **`page_size` / `page_token`.** Bounded responses, so a review can be
   worked incrementally and a re-read after writes costs one page rather than
   the document. Pagination must be stable across accept/reject, which
   argues for a token that encodes a document-order position rather than an
   offset.
3. **`author`.** Every real review task we have seen is partly per-reviewer;
   filtering server-side is one comprehension and saves a full re-read.
4. **`start_index` / `end_index`.** Section-scoped review ("everything under
   *Recommendations*") is the other half of real tasks, and index-range
   filtering is how an agent expresses it given that the API speaks in
   indexes.

Not recommended: a `resolve_many` batch write. The one-card-at-a-time write
surface is not what breaks here — the writes are cheap and the errors they
produce (a 400 on a garbage-collected suggestion) are informative. Batching
them would hide exactly the feedback an agent needs.

## Caveat

The measurements are against `mockdocs`, whose payloads are modelled on the
real Developer Preview shapes but are not byte-identical to them. The
*ratios* (cost per card, the share of a record taken up by pre/post text) are
what the recommendation rests on, and those are properties of
`gdocs_preview/analysis.py`'s record shape rather than of the mock. Re-run
the measurement against a real enrolled document before quoting absolute
numbers externally.
