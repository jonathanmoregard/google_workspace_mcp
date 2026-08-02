# The e2e write-quota wall

*Branch `probe/quota`, measured 2026-08-02 against `jonathan@klaffat.com`.*

## What was actually wrong

The Docs API caps one user at **60 write requests per minute**
(`WriteRequestsPerMinutePerUser`), and a *failed* write counts against the
budget exactly like a successful one. The e2e suite had no quota handling of
any kind.

The first measurement contradicted the reported symptom, and that turned out
to be the whole story. On a cold window, unpaced, on current `main`:

| run | result | wall | write requests | 429s |
|---|---|---|---|---|
| `-m e2e_preview`, cold | **22 passed** | 3:19 | 115 | 0 |
| `-m e2e_preview`, immediately again | **22 passed** | 3:17 | 115 | 0 |

115 write requests over ~200 s is ~35 writes/min. **One run of this suite
fits under the ceiling with room to spare, cold or hot.** The back-to-back
case that was expected to be the hard one is not hard at all.

So the defect is not "the suite is too write-dense". It is **contention**.
Several agents were running this same suite against this same Google account
at the same time, and 2×35 or 3×35 does not fit under 60. Reproducing that
directly — three concurrent unpaced `-m e2e_preview` runs — gives back
exactly the reported symptom:

| concurrent run | passed | failed | fixture errors | 429s | wall |
|---|---|---|---|---|---|
| 1 | 10 | 7 | 5 | 12 | 1:42 |
| 2 | 15 | 5 | 2 | 6 | 2:18 |
| 3 | 10 | 7 | 5 | 12 | 1:48 |

35 of 66 tests passed; every failure was `429 ... WriteRequestsPerMinutePerUser`,
and the ones that landed inside `make_scratch_doc`'s `create_doc` surfaced as
**fixture errors**, which is why they read like broken tests instead of a
quota wall.

This reframing matters for the fix: a per-process rate limiter would have
been useless here. The budget has to be shared between processes, because
the quota is.

## The design

Four parts, all in `e2e/quota.py`, wired in at exactly one seam.

**1. Pacing, on a window shared across processes.** A sliding 60 s window
over write *cost*. Before every write the caller waits until the write fits,
rather than sprinting into the wall and backing off afterwards. The window
lives in a flock-guarded JSON file under
`~/.cache/gdocs-review-mcp/e2e-write-window-<hash of account>.json` — keyed on
the **Google account**, not the checkout, so three worktrees driving one
account cooperate on one budget. Default target is 50/min against a real cap
of 60; the headroom absorbs cost-model error and sessions that are not
participating.

**2. Retry, on 429 and nothing else.** `is_rate_limit_error` is anchored on
`HttpError 429` plus explicit quota markers, so a 400 — a bug in the code
under test — still fails on the first attempt, loudly. Backoff is
exponential (2, 4, 8, … capped at 70 s, deliberately above the 60 s window it
is waiting out) with jitter, floored at however long the pacer says capacity
is away, and overridden by `Retry-After` / `retryDelay` when present.

**3. One seam, so fixtures are covered.** The guard wraps
`ServerSession.call_tool` / `call_tool_raw` in `e2e/mcp_session.py`. That is
the only way anything in the suite reaches the server, so `make_scratch_doc`,
`preview_probe`, and every test module that lands later are covered without
touching a single test file. Unknown tool names count as writes by default:
over-pacing a read costs seconds, under-pacing a write costs the run.

**4. Accounting that cannot flatter itself.** `e2e/last_run.md` gains a
**Write quota** section (writes spent, 429s absorbed, retries, seconds
waited, per-tool breakdown) and a **Completeness** section that spells out
how many tests were collected versus passed/failed/skipped. When a call
exhausts its retries the harness raises `WriteQuotaExhausted` — it does not
skip. A test that could not run because the account was out of quota is not
a passing test, and the report says `WALL HIT` and `INCOMPLETE RUN` rather
than quietly reporting a smaller suite.

Read/write classification mirrors the server's own
`@handle_http_errors(..., is_read_only=True)` declarations, and
`tests/e2e_harness/test_quota.py` re-derives that set from the source at test
time, so it cannot drift silently when tools are added.

## Results

Everything below is observed output, on the final code.

| run | result | wall | writes | 429s | waiting |
|---|---|---|---|---|---|
| `-m e2e_ga` | **14 passed, 1 skipped** | 56.9 s | 25 | 0 | 0.0 s |
| `-m e2e_preview` | **22 passed** | 3:13 | 115 | 0 | 0.0 s |
| `-m e2e_preview` again, immediately | **22 passed** | 3:16 | 115 | 0 | 0.0 s |
| `pytest e2e` (everything) | **36 passed, 1 skipped** | 4:13 | 138 | 0 | 0.0 s |

**A solo run pays nothing.** The pacer never waits, because a solo run was
never over budget. The GA baseline is unchanged (14 passed / 1 skipped / ~60 s).

The case that was actually broken — three concurrent suites, same account:

| concurrent run | before (unpaced) | after |
|---|---|---|
| 1 | 10 passed, 7 failed, 5 errors | **22 passed**, 6:33 |
| 2 | 15 passed, 5 failed, 2 errors | **22 passed**, 6:21 |
| 3 | 10 passed, 7 failed, 5 errors | **22 passed**, 6:36 |
| total | 35/66, 30 × 429 | **66/66, 0 × 429** |

345 write requests across three processes, zero rate limiting, ~200 s of
pacing per process. Roughly 2× the solo wall time, which is the arithmetic
floor: 345 writes cannot be spent faster than 50/min.

### Ablation: which half does the work

Retry alone, pacing disabled, three concurrent runs — this is what a
retry-only fix would have bought:

| | passed | 429s absorbed | backoff paid |
|---|---|---|---|
| run 1 | 22 | 6 | 125.8 s |
| run 2 | 22 | 6 | 117.0 s |
| run 3 | 22 | 6 | 117.9 s |

Retry alone also completes, but pays ~120 s of backoff per process and
spends 429s that count against the very budget it is waiting for. Pacing is
what makes the 429 count zero. Retry is the safety net for the sessions that
are not participating in the shared window.

### A bug the ablation found

The first ablation had one genuine failure, and it was worth having:

```
AssertionError: Error: Failed to write kix.ca8fpucwjkry segment content:
<HttpError 429 ... 'quota_limit': 'WriteRequestsPerMinutePerUser'>
```

`update_doc_headers_footers` **catches the 429 internally and returns it as
prose in a perfectly successful MCP result** — `is_error` is not set. The
guard saw a success, did not retry, and the test failed on an assertion about
the response body: a quota failure wearing the costume of a product bug,
which is the exact class of confusion this work exists to remove.
`rate_limited_result_text` now also inspects the *body* of successful write
results. Re-running the same ablation afterwards: 3/3 runs green, 18 real
429s absorbed.

## What is still fragile

- **The cost model is an estimate.** The harness counts *MCP calls*, not API
  requests, and credits `create_doc`-with-content 2, `create_table_with_data`
  2, `update_doc_headers_footers` 2, everything else 1. A tool that quietly
  issues three writes will be under-counted. Mitigations: 50/min instead of
  60, and the pacer shrinks its own budget by 20 % (floor 12/min) every time
  the API says 429. Nothing verifies the weights against real request counts —
  the blackbox cannot see them.
- **`Retry-After` is mostly unavailable.** Through the MCP surface a tool
  error is *text*; the response headers are gone by then. `Retry-After` is
  honoured properly only on the harness-side Drive path (teardown, audits),
  which still has real headers. Everywhere else it is recovered from the
  message when the server happened to preserve it, and otherwise the
  exponential curve is used. This is a limit of blackbox testing, not an
  oversight.
- **Retrying `create_doc` can orphan a document.** `create_doc` with content
  is two API requests; a 429 on the second means the retry produces a *new*
  document and the first becomes garbage nothing tracks. Measured: the
  retry-only ablation orphaned 3 documents. The scratch-doc factory now
  detects this (the guard flags retries of creating tools), looks the twin up
  by its unique title, and adopts it into the tracker so teardown trashes it —
  and the run report points at the `(abandoned retry)` rows. Verified against
  Drive: zero strays after the paced runs. But the reclaim is a lookup by
  title after the fact, not an atomic operation.
- **Only sessions running this code cooperate.** The shared window is
  advisory. A branch without this change, a manual `curl`, or Docs traffic
  from the browser all spend the same quota invisibly. Those collisions land
  on the retry path, not the pacing path.
- **The budget does not recover within a run.** Once shrunk by a 429 it stays
  shrunk until the process exits. Deliberate — a run that has already been
  told it is too fast should not re-litigate — but it means one unlucky 429
  early makes the rest of that run slower than it needs to be.
- **Volume is about to grow.** Four write-dense modules land tonight. The
  suite scales linearly in wall time under pacing: at 50 writes/min, every
  additional 50 write requests costs a minute. If `-m e2e_preview` triples,
  expect ~10 min solo. If that becomes intolerable the honest lever is
  `E2E_WRITE_BUDGET_PER_MIN` toward 58, not disabling pacing.

## Knobs

| variable | default | effect |
|---|---|---|
| `E2E_WRITE_BUDGET_PER_MIN` | 50 | writes/min the pacer targets |
| `E2E_QUOTA_MAX_ATTEMPTS` | 6 | attempts per call; 1 disables retry |
| `E2E_QUOTA_PACING` | on | `off` disables pacing (used for the ablation) |
| `E2E_QUOTA_STATE` | on | `off` disables cross-process window sharing |
| `E2E_QUOTA_STATE_PATH` | per-account cache file | explicit window file |
