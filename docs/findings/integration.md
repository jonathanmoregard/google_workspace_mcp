# Integrating the five empirical probe branches — 2026-08-02

`integration/empirics` is `main` plus all five probe branches, merged in
descending order of invasiveness so that each later merge faced a smaller
surface. Every branch's change is backed by a measured live-API finding, so
the resolution rule throughout was **keep both intents** — no side was ever
picked over another.

| # | branch | result | conflicted files |
|---|--------|--------|------------------|
| 1 | `probe/coverage` | fast-forward | — |
| 2 | `probe/tabs` | 2 conflicts, resolved by hand | `gdocs_preview/preview_read.py`, `tests/gdocs_preview/test_preview_read.py` |
| 3 | `probe/suggest` | clean auto-merge | — |
| 4 | `probe/merge` | clean auto-merge | — |
| 5 | `probe/errors` | clean auto-merge | — |

Only two files needed hand resolution. Both conflicts were **positional, not
semantic**: two branches appended new top-level definitions at the same
anchor point. No two branches ever asserted contradictory things about the
same behaviour, so the "stop and report" clause was never reached.

---

## Conflict 1 — `gdocs_preview/preview_read.py` (coverage vs tabs)

Both branches inserted a new block immediately after
`suggestion_threads_by_id`, so git saw one overlapping region.

- **coverage** added the unreported-suggestion inventory:
  `RESOLVED_THREAD_STATUSES`, `thread_is_pending`, `pending_thread_ids` —
  the API's own pending set, which is wider than what the content-mark walk
  can model (`docs/findings/coverage.md`).
- **tabs** added `anchor_tab_ids`, the `commentAnchors` join that places a
  comment thread in a tab (`docs/findings/tabs.md`).

**Resolved:** both blocks kept, coverage's first, tabs' second, separated by
the usual two blank lines. They share no names and no call sites. Tabs'
other edits in this file — `parent_tab_id`/`nesting_level` on `TabDocument`,
`tab_id` on `comment_threads`, the depth-carrying `walk` — sat outside the
conflicted region and merged automatically.

Evidence for keeping both: the two features are independent (a suggestion
inventory and a comment-to-tab attribution), and each is the sole carrier of
its finding. Dropping either would silently un-fix a measured defect.

## Conflict 2 — `tests/gdocs_preview/test_preview_read.py` (coverage vs tabs)

Both branches appended after
`TestTabDocuments.test_payload_without_tabs_yields_one_implicit_tab`, and
tabs *also* extended that test's body with two assertions
(`parent_tab_id is None`, `nesting_level is None` for the GA fallback).

**Resolved:**

1. tabs' two assertions folded back into the existing test body;
2. tabs' two new `TestTabDocuments` methods
   (`test_a_nested_tab_reports_its_parent_and_depth`,
   `test_nesting_is_read_off_the_walk_not_off_tabproperties`) and its new
   `TestCommentTabAttribution` class kept in place;
3. coverage's new `TestPendingThreadIds` class **moved to the end of the
   file**.

Step 3 is the load-bearing one. Taking the conflict in the naive order would
have left coverage's `class TestPendingThreadIds:` sitting between the tab
tests and their class header, silently re-parenting tabs' two methods onto
the wrong class — they would still have run, still passed, and no longer
tested `TabDocument`. Ordering, not content, was the whole risk here.

## Files that merged cleanly but were checked anyway

Three files were flagged as likely conflicts. Git resolved all three because
the branches' hunks turned out to be disjoint; each was verified by hand
afterwards.

- **`gdocs_preview/write_tools.py`** (coverage vs suggest) — coverage's
  `_PostWriteRead.pending_state` fix lives at lines ~73/215/308, suggest's
  replacement of the SUGGEST index-resolution `UNCERTAIN` comment at ~1495.
  Both present. The fail-open on the destructive path is fixed:
  `pending_state` now answers from `self.records or self.pending_thread_ids`,
  so a rejected paragraph-style card the analysis layer cannot describe can
  no longer be reported as gone while the API still lists it OPEN.
- **`mockdocs/adapter.py`** (suggest vs merge) — suggest's
  `SUGGEST_UNSUPPORTED = SUGGEST_UNSUPPORTED_OFFICIAL` (dropping
  `PREVIEW_REQUEST_TYPES`, which prod measurably accepts) plus the verbatim
  refusal string at line 107/666; merge's `RESOLVED 2026-08-01` block at 593.
  Disjoint, both present.
- **`tests/gdocs_preview/test_curated_tools.py`** (tabs vs errors vs
  coverage) — three branches, three separate regions, all auto-merged. Every
  test from all three survives.

Across that file exactly **two** pre-existing lines were replaced, by
different branches, in different tests, with no semantic overlap:

| line | branch | change |
|------|--------|--------|
| ~143 | tabs | tab metadata assertion gains `parent_tab_id` / `nesting_level` |
| ~1101 | errors | the `HttpError` fixture message becomes a real semantic 400 copied off the live API, because the old string is parse-failure grammar and now classifies as `unknown` |

Neither is a contested assertion; both are the same measured-evidence
upgrade applied to unrelated fixtures.

## How "no work was lost" was verified

Two independent checks, because a green suite alone would not catch a test
quietly re-parented or dropped.

1. **Arithmetic on the test count.** Each branch's delta over `main` adds up
   exactly to the integrated total, with nothing absorbed or double-counted:

   ```
   2391 (main)
   + 30 (coverage)  -> 2421
   +  6 (tabs)      -> 2427
   +  2 (suggest)   -> 2429
   +  0 (merge)     -> 2429
   + 23 (errors)    -> 2452
   ```

   Every intermediate number was measured after its own merge and matched
   the branch-alone baseline.

2. **AST symbol union.** For each of the five files any two branches touched,
   the qualified names (`Class.method`) of every class, function and
   module-level assignment on each branch were checked to be a subset of
   HEAD's, with no duplicates. Because the names are *qualified*, this also
   proves the re-parenting hazard in conflict 2 did not happen.

   ```
   ok  gdocs_preview/preview_read.py: 27 symbols, union intact
   ok  tests/gdocs_preview/test_preview_read.py: 40 symbols, union intact
   ok  tests/gdocs_preview/test_curated_tools.py: 75 symbols, union intact
   ok  gdocs_preview/write_tools.py: 66 symbols, union intact
   ok  mockdocs/adapter.py: 50 symbols, union intact
   ```

## Final gate

| gate | result |
|------|--------|
| `uv run pytest tests/ -q` | **2452 passed, 3 skipped** (main: 2391/3) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 313 files already formatted |
| `uv run python -m llmux.scenarios.validate` | **17/17 scenarios valid** |
| `uv run python -m llmux.runner.run --corpus llmux/scenarios/generated --dry-run --all` | dry run clean, 0 problems, 17 scenarios |
| `uv run pytest e2e -m e2e_ga -q` (smoke, one run) | 25 passed, 1 skipped, 54 deselected — no 429 |

The full `e2e` suite was **not** run: it is quota-broken independently of
this integration and is being fixed on `probe/quota`.

## Notes

- `HANDOVER.md` and `docs/preview-api-reference.md` were **not touched** —
  no merge conflicted there, so nothing had to be taken from main's side.
  They are still the orchestrator's to consolidate, and they now describe
  five branches' worth of findings that have landed without them.
- `uv.lock` picks up unrelated churn on every `uv run` (this `uv` writes
  lockfile `revision = 3` and `upload-time` fields where the committed file
  has `revision = 1`). It was reverted before each commit and is unchanged
  on this branch.
- **Observed, deliberately not fixed:** `mockdocs/model.py:352` still reads
  `same-author batch suggestions is UNCERTAIN (spec §14)`, while
  `probe/merge` resolved §14 and rewrote the spec section. The line is
  identical on `main` and on `probe/merge` itself, so this is not a merge
  artifact — it is a loose end that branch chose to leave. Flagged rather
  than edited, since this branch resolves conflicts and changes nothing else.
