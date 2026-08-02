# Blackbox e2e suite (docs_preview)

Exercises the **real Google APIs through the real MCP server**: the suite
spawns `python main.py --transport stdio --single-user --tools docs docs_preview`
as a subprocess and talks MCP protocol to it with the fastmcp client
(`e2e/mcp_session.py`). Nothing is mocked; the server process is the same
one an MCP client would launch.

## Credential gating (the suite NEVER auths interactively)

| state | behavior |
|---|---|
| no OAuth token in the credential store | every test **skips** with step-by-step instructions |
| token present, refreshable | `e2e_ga` tests run against the GA surface |
| + Developer Preview enrollment detected | `e2e_preview` tests run too |

The token store is the server's own: `~/.google_workspace_mcp/credentials`
(override with `WORKSPACE_MCP_CREDENTIALS_DIR`). Preview enrollment is
auto-detected once per session by the `check_docs_review_capabilities`
probe against a scratch doc; unenrolled runs skip the preview block with
the probe's classification evidence in the skip message.

## One-time setup (human)

1. Create a **Desktop app** OAuth client and save its JSON at
   `credentials/oauth_client.json` (exact console steps:
   `pending_for_human.md` at the repo root).
2. Run the only interactive step - it prints a consent URL, waits for the
   localhost callback, and writes the token where the server expects it,
   with every scope the docs_preview + Drive comment tools need:

   ```bash
   uv run python e2e/bootstrap_auth.py
   ```

## Running

```bash
uv run pytest e2e -m e2e_ga                     # GA surface (needs token)
uv run pytest e2e -m e2e_preview                # preview surface only
uv run pytest e2e                               # everything eligible
uv run pytest e2e -rs                           # show skip reasons
```

With no credentials all tests skip cleanly (exit 0) - safe in CI.

## Write quota (why a run sometimes waits)

The Docs API allows **60 write requests per minute per user**, and failed
writes count too. One run of this suite spends ~115 write requests over
~200 s, which fits — but the quota belongs to the *Google account*, so two
or three sessions running at once do not, and the failures land inside
`make_scratch_doc`'s `create_doc` as **fixture errors** that look like
broken tests.

`e2e/quota.py` sits on `ServerSession.call_tool`, the single seam every
test and fixture reaches the server through:

- **pacing** — a sliding 60 s window over write cost, targeting 50/min.
  The window is shared between processes through a flock-guarded file
  under `~/.cache/gdocs-review-mcp/`, keyed on the account, so concurrent
  checkouts cooperate instead of colliding. A solo run never waits.
- **retry** — 429 only, exponential backoff with jitter, honouring
  `Retry-After` where it survives (the harness-side Drive calls; the MCP
  surface renders errors to text and loses headers). **A 400 still fails
  on the first attempt** — it is a bug, not a transient.
- **honesty** — exhausting the retries raises `WriteQuotaExhausted`, it
  never skips; `e2e/last_run.md` reports what the run spent and says
  `WALL HIT` / `INCOMPLETE RUN` if quota stopped it.

Tools are classified read vs write by mirroring the server's own
`is_read_only=True` declarations (a unit test re-derives the set from the
source so it cannot drift); anything unrecognised is paced as a write.

| variable | default | effect |
|---|---|---|
| `E2E_WRITE_BUDGET_PER_MIN` | 50 | writes/min the pacer targets |
| `E2E_QUOTA_MAX_ATTEMPTS` | 6 | attempts per call; 1 disables retry |
| `E2E_QUOTA_PACING` | on | `off` disables pacing |
| `E2E_QUOTA_STATE` | on | `off` disables cross-process sharing |
| `E2E_QUOTA_STATE_PATH` | per-account cache file | explicit window file |

Measurements, design rationale and the known-fragile list:
[`docs/findings/e2e-quota.md`](../docs/findings/e2e-quota.md).

## Hygiene & determinism

- Every run creates fresh scratch docs titled
  `e2e-gdocs-review-<timestamp>-<rand>` **through the MCP surface** and
  trashes them in fixture teardown via a direct Drive client (works even
  if the server under test died). `test_zz_teardown_audit.py` re-verifies
  against Drive at the end of each run.
- No sleeps-as-synchronization: eventual consistency is handled by
  `e2e/util.py:poll_until` (bounded retries).
- Several preview tests double as empirical probes: they RECORD real
  payload/error shapes (where suggestion threads surface in
  `documents.get`, response-union member names, 400-error message shapes
  feeding `gdocs_preview/preview_status.py`) into the run report.

## Artifacts

- `e2e/last_run.md` (path override: `E2E_RUN_REPORT_PATH`) - date, token
  identity, per-marker pass/fail/skip counts, preview classification
  evidence, scratch-doc hygiene table, observed API error shapes. This is
  the report forwarded to the client requester.
- `e2e/_artifacts/server-*.log` - server stderr per session, for debugging.

## Harness unit tests

The gating/report/session-construction logic is itself unit-tested
(mocked, no credentials or network) in `tests/e2e_harness/` - those run
with the normal suite: `uv run pytest tests/e2e_harness`.
