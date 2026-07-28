# Blackbox e2e suite (docs_preview)

Exercises the **real Google APIs through the real MCP server**: the suite
spawns `python main.py --transport stdio --single-user --tools docs_preview`
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
auto-detected once per session by the curated `docs_review_capabilities`
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
