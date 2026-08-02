# Errors shaped like successes

*Branch `fix/errors-as-success`, off `integration/empirics` at `7f7f69f`.
Audited and fixed 2026-08-02.*

## The bug class

An MCP tool calls a Google API, catches the exception, and **`return`s it as a
string**. Because the tool returned normally, the MCP layer never sets
`is_error`, so the caller is told the call **succeeded** and the API failure
is just prose in the body.

`core/utils.py:handle_http_errors` is the shared decorator that turns a
**raised** `HttpError` into a real error. It wraps every tool in `gdocs/` and
`gdocs_preview/`. It cannot help a handler that catches instead of raising —
**every site below bypassed a decorator that was already applied to it.**

This breaks both halves of the standard in `HANDOVER.md` §4 at once:

- an error is shaped like a success, and
- the body then makes a claim the tool has no evidence for — `"Table creation
  failed"` about a table that was created, `"No header found in document"`
  when the *read* failed, `"Could not access Drive file"` when the file was
  fine and the quota was not.

## What reproduced

Not hypothetical. From `docs/findings/e2e-quota.md`: on 2026-08-02, while
building the e2e write-quota pacer, `update_doc_headers_footers` swallowed a
live 429 and returned it as a successful result. The quota guard read
`is_error`, saw success, did not retry, and the test failed on an assertion
about the response body — a quota failure wearing the costume of a product
bug.

Before touching any source, every candidate was driven with a mocked 429 and
its actual return value recorded. **Eight sites returned the 429 as a
successful result**; two probes that looked like candidates turned out fine.
The measurements are encoded as
[`tests/gdocs/test_api_errors_are_not_success.py`](../../tests/gdocs/test_api_errors_are_not_success.py),
which was committed **failing** (`12 failed, 5 passed`) before any fix landed.
The 5 that passed on that commit are the behaviours the fix had to preserve.

## Sites FIXED (8 measured + 2 unreachable twins)

| # | site | what it returned on a 429 | now |
|---|---|---|---|
| 1 | `gdocs/managers/header_footer_manager.py` `_replace_section_content` | `(False, "Failed to write kix.… segment content: <HttpError 429 …>")` → `"Error: …"` | raises |
| 2 | same file, `update_header_footer_content` | blanket `except Exception` also swallowed the document read and the section lookup | raises |
| 3 | same file, `_create_missing_section` | any failure → `None` → `"No {header} found in document and automatic creation failed"` | raises |
| 4 | `gdocs/managers/batch_operation_manager.py` `execute_batch_operations` | `"Error: Batch operation failed: <HttpError 429 …>"` | raises |
| 5 | `gdocs/managers/table_operation_manager.py` `create_and_populate_table` | `"ERROR: Table creation failed: <HttpError 429 …>"` | raises **before** creation, degrades **after** |
| 6 | same file, `populate_existing_table` | `"Failed to populate existing table: …"` | raises |
| 7 | `gdocs/docs_tools.py` `export_doc_to_pdf` (metadata read) | `"Error: Could not access document <id>: …"` | raises |
| 8 | same, `export_doc_to_pdf` (export) | `"Error: Failed to export document to PDF: …"` | raises |
| 9 | same, `export_doc_to_pdf` (upload) | `"Error: Failed to upload PDF to Drive: …"` | raises, keeping "the PDF was generated but nothing was written" |
| 10 | same, `insert_doc_image` (Drive lookup) | `"Error: Could not access Drive file <id>: …"` | 404 → `UserInputError`; everything else raises |
| 11 | same, `get_doc_as_markdown` (timeout) | `"Error: Timed out fetching document …"` | raises `TransientNetworkError` |

Plus two **unreferenced** methods in `header_footer_manager.py` with the same
shape — `get_header_footer_info` (returned `{"error": str(e)}`) and
`create_header_footer`. Neither is reachable from any MCP tool today
(grepped the whole tree; only `update_header_footer_content` has a caller).
Fixed because they are what the next caller steps on.

### The two claims that were also false, not just mis-shaped

Two sites did not merely have the wrong shape — the sentence in the body was
wrong about the world:

- **`create_and_populate_table`** reported `"Table creation failed"` when the
  table *had been created* and only cell population failed. A partial-success
  degrade for exactly this already existed, but only fired when
  `header_rows > 0`; otherwise the exception escaped into the blanket handler.
  `header_rows` has nothing to do with whether the table exists, so the
  degrade now covers both. This one matters beyond shape: an error here
  invites a retry, and a retry creates a **second table**.
- **`_create_missing_section`** reported `"No header found in document"` — a
  claim about the *document* — when what actually happened is that the *call*
  failed.

## Deliberate degrades — preserved, and why they are not this bug

A degrade is legitimate when the operation genuinely half-landed and saying
so is more useful than failing. The test for whether it is a degrade rather
than a fail-open: **does the response say it degraded?**

| degrade | why it stays |
|---|---|
| `gdocs_preview/preview_read.py` `read_for_review` — GA fallback | The documented one (`HANDOVER.md` §4.6). Reports `read_source`, `degraded_reason`, `complete=False`, and every downstream absence claim is gated on `complete`. **Untouched.** |
| `table_operation_manager` post-creation failures | The table exists; an error would invite a retry that creates a second one. Returns `partial_success: True`, `table_created: True`, and "Do not retry table creation" in the message. **Widened** to cover `header_rows == 0`, which previously raised into a false failure. |
| `gdocs_preview/write_tools.py` `_post_write_read` | The write already landed; a verification failure must not make a successful mutation look like something to redo. Surfaces as `verification.source: "unavailable"` with a reason. **Untouched.** |
| `header_footer_manager` first-write retry | A freshly created segment can lag before its content is visible. The *first* failure is retried after a re-read; the *second* propagates. **Preserved deliberately** — pinned by `test_the_lagging_segment_retry_still_happens_before_raising`. |

## Sites CHECKED and found FINE

This list is the point of the audit as much as the fixes are.

### `gdocs_preview/` — clean, zero instances

The package that the fork exists for has none of this. Its four write paths
(`_execute_preview_batch_update`, `manage_document_suggestion`,
`reply_to_doc_thread`, `create_anchored_doc_comment`) all either
`raise UserInputError` with actionable guidance or bare-`raise`. An HTTP 200
carrying `ALL_FAILED_UNKNOWN_REASON` is *raised*, not returned
(`_execute_preview_batch_update(..., enforce_comment_update=True)`).
`analysis.py`, `suggestion_ledger.py`, `preview_status.py` and `address.py`
contain **no `except` statements at all**.

One borderline call, examined and left alone:
`curated_tools.py:379`, the capabilities probe, catches `HttpError` and
converts it into a verdict rather than raising. That is not this bug — the
tool's *product* is a classification of a deliberately-failing call, and the
docstring enumerates the mapping. A 429 during a probe classifies as
`("unknown", "unexpected_http_429")`, which is the honest answer: it proves
nothing about enrollment either way.

### `core/comments.py` — clean

One `except` in the whole file, on `int(os.getenv(...))` — env parsing, no
API call. All five write impls (`create`/`reply`/`resolve`/`update`/`delete`)
and the pagination loop are bare `await asyncio.to_thread(….execute)` with no
try/except, so failures propagate to `handle_http_errors`.

### The rest of `gdocs/`

| site | why it is fine |
|---|---|
| `docs_tools.py:737` `modify_doc_text` | `raise _rewrite_modify_doc_text_http_error(...) from error` — the in-repo precedent this fix follows: rewrite what is actionable into a `UserInputError`, re-raise the rest |
| `docs_tools.py:2346` `update_paragraph_style` | the `try` does enclose an API call, but catches only `ValueError`, which only `create_bullet_list_request` raises. `HttpError` propagates. Safe today; fragile if the `except` is ever widened |
| `docs_tools.py:320` `get_doc_content` | `UnicodeDecodeError` on already-downloaded bytes — a fact about content |
| `docs_tools.py` validation returns (`:529, 548, 568–588, 866–892, 1065–1075, 1339–1343, 1827–1835, 2245–2294, 2466–2470`) | pre-API input validation; no call failed |
| `docs_tools.py:977, 1465, 1467, 1942, 2024` | facts derived from a **successful** response — wrong MIME type, tab absent, table index out of range |
| `batch_operation_manager.py:147` | `document_length = None` on a post-batch length probe; cosmetic enrichment on an already-succeeded write |
| `batch_operation_manager.py:226–253` `_preflight_…` | facts from a successful `documents().get` |
| `docs_helpers.py`, `docs_markdown_writer.py`, `docs_structure.py`, `docs_tables.py`, `validation_manager.py` | **no `except` statements at all** |
| `docs_markdown.py:293` | local timestamp parse |

**Validation strings were deliberately left as returned prose.** They are not
failures of the API, they are the tool answering the caller about the
caller's own input, and turning ~40 of them into raises across an upstream
file would be a large, merge-hostile diff for no gain in honesty. Three of
them are pinned as *must not change* by the new test file.

## Found, IN SCOPE for the bug class, NOT fixed here

`gdrive/drive_tools.py` has three instances. They are **not** fixed on this
branch, deliberately: `gdrive/` is untouched by the fork, and `HANDOVER.md`
§8 asks that the fork stay merge-friendly and that upstream files not be
edited when the change can live elsewhere. None of the three is on the docs
review surface. Recorded here so the list exists:

- **`drive_tools.py:1629` `get_drive_file_permissions`** — the textbook case,
  and the worst of the three. Blanket `except Exception` around
  `files().get(...).execute` → `return f"Error getting file permissions: {e}"`.
  A 403/429/500 is reported as a successful permission check.
- **`drive_tools.py:2284` `manage_drive_access`** (`grant_batch`) — per-recipient
  `HttpError`s folded into a prose summary. Defensible for a mixed batch;
  the all-failed case reports `"0 succeeded, N failed"` as a success.
- **`drive_tools.py:771` `_fetch_organizers`** — enrichment failures rendered
  as `Organizers: <error: 403: …>` inside an otherwise successful listing.

Adjacent but a different cause, noted for completeness:
`drive_tools.py:547` returns a **local disk** failure as prose (the API
download had already succeeded).

## The pattern is upstream-wide, not fork-local

Counted so the next person does not have to. Roughly half the service
packages carry it:

| package | direct instances | notes |
|---|---|---|
| `gdrive/` | 3 | above |
| `gchat/` | 2 | `chat_tools.py:685,693` |
| `gmail/` | 1 | `gmail_tools.py:1981` |
| `gappsscript/` | 1 | `apps_script_tools.py:530`, its only `except` |
| `gcalendar/`, `gtasks/`, `gcontacts/` | 0 | all `except` blocks raise |
| `gforms/`, `gsearch/` | 0 | zero `except` statements |
| `gsheets/`, `gslides/` | 0 direct | but see below |

The `gmail` one is worth singling out because its prose actively
**misdiagnoses**: `get_gmail_attachment_content` reports a 429 as *"The
attachment ID may have changed"*, sending the agent to re-fetch a message
that was never the problem.

There is also a weaker sibling in `gsheets/sheets_helpers.py:1089,1207` —
enrichment fetches swallowed into `return "", []`, which `read_sheet_values`
then renders as "no formulas / no formatting" with nothing saying a call
failed. Same fail-open direction, expressed as a silent absence rather than
an error string.

**None of this is introduced by the fork**, and none of it is fixed here.
`core/comments.py` §3.5 remains the piece that is cleanly upstreamable; a
repo-wide sweep of this bug class would be a second, larger PR.

## Gates

| gate | result |
|---|---|
| `uv run pytest tests/ -q` baseline on `integration/empirics` | **2529 passed, 3 skipped** (855 s) |
| `uv run pytest tests/ -q` after the fix | see below |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean, 317 files |
| `uv run pytest e2e -q` | see below |

17 tests added in `tests/gdocs/test_api_errors_are_not_success.py`. One
existing test changed contract deliberately:
`tests/gdocs/test_table_row_style.py::test_success_reports_effective_index`
stubbed the manager *returning* the document-end rejection; it now *raises*
it, because that is what the manager does.
