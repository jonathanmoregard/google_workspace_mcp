# Large review sets: what the stress corpus found, and the surface change it argues for

Written 2026-07-30 alongside `llmux/scenarios/stressgen/`. It was a
**recommendation**; it was implemented on 2026-07-31 in
`gdocs_preview/review_page.py`. The measurement and the argument below are
unchanged; what was actually built, and where it departs from the
recommendation and why, is recorded in [What was
built](#what-was-built-2026-07-31) at the end.

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

## What one real run cost

A single sonnet run against the **30-card** tier (run 20260730-224005; the
reports directory is gitignored, so reproduce with `uv run python -m
llmux.runner.run --corpus llmux/scenarios/stress --scenario
stress-030-faq-copyedit --models sonnet`) passed at score 1.00 in 17 turns
and 62 seconds, using one `list_document_suggestions`, one
`get_doc_review_view` and 14 writes, with **zero tool errors**. So the tool
*syntax* is not the constraint even at 30 cards, which is the result the
stress corpus was built to isolate.

The cost is. That run reported:

| | tokens |
| --- | ---: |
| cache creation (input) | 88,738 |
| cache read (input) | 163,934 |
| output | 6,420 |
| **cost** | **$0.68** |

The ~16,000-token read set is re-sent on every turn, so total input scales as
*context × turns*, and both terms grow with card count: the 120-card tier has
2.6x the read set and ~6x the writes. That is the mechanism by which a review
tool that works fine at 30 cards becomes unusable at 300, and it is exactly
what `fields="summary"` and pagination attack.

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
   highest-value change. (Dropping `segment`, `segment_id` and `tab_id` was a
   mistake in this recommendation, corrected on 2026-07-31 -- see [What was
   built](#what-was-built-2026-07-31). Every predicate is decidable without
   them; no *write* is, because an index without its segment and tab is not
   an address.)
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

---

## What was built (2026-07-31)

The narrowing lives in `gdocs_preview/review_page.py` -- pure functions over
analysis records, so the arithmetic is testable without a service object
(`tests/gdocs_preview/test_review_page.py`). The tools stayed thin.

```python
async def list_document_suggestions(
    service, user_google_email, document_id,
    fields: str = "summary",            # "summary" | "full"
    page_size: Optional[int] = None,    # default 200 summary / 40 full
    page_token: Optional[str] = None,
    author: Optional[str] = None,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    status: Optional[str] = None,
) -> str

async def get_doc_review_view(
    service, user_google_email, document_id,
    view_mode: str = "SUGGESTIONS_INLINE",
    fields: str = "text",               # "text" | "paragraphs" | "full"
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    include_comments: bool = True,
) -> str
```

Measured with `uv run python -m llmux.scenarios.stressgen.measure --markdown`,
defaults against every-field-one-response:

| cards | `list` full | `list` default | cut | `review_view` full | `review_view` default | cut | one read of each (est. tokens) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 | 23,732 | 7,556 | 68.2% | 33,727 | 13,136 | 61.1% | 16,423 -> 6,210 |
| 60 | 47,736 | 14,642 | 69.3% | 44,588 | 19,036 | 57.3% | 26,121 -> 9,830 |
| 90 | 70,354 | 21,276 | 69.8% | 45,349 | 20,956 | 53.8% | 32,683 -> 12,274 |
| 120 | 93,636 | 27,891 | 70.2% | 54,980 | 23,954 | 56.4% | 41,811 -> 14,932 |

An earlier revision of this table read 74.2–76.6% for the `list` cut. That
number was bought by dropping `segment`, `segment_id` and `tab_id` from the
summary record, which was wrong: Docs indexes are unique only within a
`(tabId, segmentId)` pair, and `suggest_doc_edit` defaults to the body of the
default tab, so a summary card carrying a bare `start_index` let an agent aim
a footnote's or a header's or a second tab's local index at the body with
nothing in the response to warn it. Putting the three fields back costs ~48
characters a card (summary is now ~232–252 chars/card, was ~183–204) and
takes the cut to **68.2–70.2%**. The honest headline is the one above.

### Four deviations from the recommendation, and why

**1. `fields="summary"` is the DEFAULT, not `"full"`.** The recommendation
asked for "no behaviour change for existing callers". The artifacts of batch
`20260730-224247` argued the other way: at 120 cards the client answered the
tool with `Error: result (105,187 characters) exceeds maximum allowed tokens`
and wrote the payload to a file the agent had no tool to open. The agent
never saw a suggestion id. A default whose response cannot be delivered is
not the conservative option, so the default is the one that fits and `full`
is one parameter away. `summary` also keeps `status`, which the
recommendation would have dropped -- it costs 16 characters a card and it is
the difference between "pending" and "already dealt with".

**2. Page size defaults are per field mode (200 / 40), sized in bytes.** The
binding constraint is what a client will deliver in one tool result, not a
card count; a card costs ~780 characters in `full` and ~172 in `summary`, so
one page size for both would be wrong for one of them. Both defaults land a
full page at 31-35 KB, under the ~57 KB at which the observed client began
spilling output to a file.

**3. `get_doc_review_view` got field selection and an index window, not
pagination.** Measured first, as asked. Its growth is *not* comparable:
~100-200 characters per card against `list`'s flat 780, because it is
dominated by the document, which the review set does not lengthen. What it
does have is redundancy -- at 120 cards the paragraph map was 26,269
characters and `body_text` 13,462, and `body_text` is exactly the
concatenation of the body paragraphs' `text`. So the fix is to stop saying
it twice (default `fields="text"`), plus the window the failing run's agent
explicitly wanted when it worked out that "Limitations of this evidence" was
indices 6841-8518 and had no way to ask for it.

**4. Index ranges are half-open `[start_index, end_index)`, and scoped to one
`(tab, segment)`.** The recommendation said "range overlap" without picking
an end. Half-open, because that is the convention the caller's numbers arrive
in: Docs `endIndex` is exclusive, so a paragraph map reports the next
paragraph's start as this one's end, and an inclusive filter pulls in the
paragraph on the far side of every seam. `end_index <= start_index` is
refused with the convention spelled out rather than silently returning
nothing.

Scoped, because an index is only unique within a `(tabId, segmentId)` pair.
Comparing raw numbers made an index range match the body AND every header,
footer, footnote and other tab whose *local* index fell in the window, so
`matched_count` was wrong and the extra cards looked like they were in the
section under review. The semantics now (`review_page.resolve_range_scope`,
added 2026-07-31):

- a range means the **body** unless `segment_id` names a segment — a non-body
  segment always has an id, so `None` is not ambiguous;
- `tab_id` is **resolved** when the document has one tab and **required**
  when it has more than one (refused with the tab ids listed, rather than
  silently answering about one of them);
- `segment_id` / `tab_id` are ordinary filters in their own right, so nothing
  passed is ever silently ignored;
- cards outside the scope are counted in `filters.excluded_other_segments`,
  and the space used is echoed as `filters.range_scope` (`window.scope` for
  `get_doc_review_view`);
- the page token's fingerprint includes both, so a token minted in one
  segment cannot be replayed in another.

The same segment blindness was in `write_tools._overlaps`, which decides the
`suggestions_at_edit_range` echo; it now compares only within the
`(tabId, segmentId)` the edit itself named.

### The parts that were taken as written

- **Page tokens encode a position, not an offset** -- the recommendation
  called this out and it matters more than it looks. The token anchors on the
  last emitted `suggestion_id`; the next page resumes after that id wherever
  it now sits, so resolving cards from page 1 does not skip the cards behind
  them. When the anchor is itself gone -- the normal working pattern -- the
  recorded ordinal is the fallback and the response says, in the page block,
  that the fallback can skip or repeat a card.
- **No `resolve_many` batch write.** Nothing in the measurement moved.
- **Never truncate silently.** Every response carries `suggestion_count` (the
  document total, meaning unchanged), `matched_count`, `returned_count`,
  `omitted_fields`, the applied `filters`, and -- when there is more -- a
  `next_page_token` and a sentence saying this is a page.

Both tools' new parameters are pinned against the real enrolled API in
`e2e/test_preview_surface.py`
(`test_fields_filters_and_pagination_against_the_real_api`,
`test_review_view_fields_and_window_against_the_real_api`), so the caveat
above is now discharged for the semantics; the ratios remain mock-derived.

### What it did to the curve

Batch `20260731-091321`, same corpus, same models, same runner flags as
`20260730-224247` -- and with the harness defects that contaminated that
batch fixed (`llmux/runner/toolprobe.py`, `session.BUILTIN_TOOLS_DENIED`,
rate-limit parsing, INCONCLUSIVE).

| cards | sonnet before | sonnet after | opus before | opus after |
| ---: | ---: | ---: | ---: | ---: |
| 30 | 1.00 | **1.00** | 1.00 | **1.00** |
| 60 | 0.85 | **1.00** | 0.88 | **1.00** |
| 90 | 1.00 * | **1.00** | 0.40 | **1.00** |
| 120 | 0.35 † | **1.00** | 0.35 | **1.00** |

\* not a result: that run got a shell through the leaked `Monitor` built-in
and used it to parse the tool output the client had spilled to a file.
† not a result either: killed by the wall clock at exactly the do-nothing
baseline score.

8/8 at 1.00, 450 turns, $17.08, 45.7 min wall. No leaked built-ins in any
run, no non-MCP tool calls, no rate limiting (each run emitted one
`rate_limit_event`, all `status: allowed`). Zero errors on either read tool
across 23 calls.

The agents used the parameters unprompted and differently from each other,
which is the ergonomic result worth having: sonnet went `fields='summary'`
and then narrowed with `status='OPEN'`; opus preferred `fields='full'` with
`page_size=40` and walked three pages of the 120-card set by token. The
120-card tier now costs 92 turns and $2.11 on sonnet, against a run that
previously never saw a suggestion id.

One caveat: the opus 120 run terminated on `error_max_budget_usd` ($6.58
against a $6.00 cap) -- after resolving every card correctly, so nothing is
hidden by it, but the tier is at the edge of that budget on opus.
