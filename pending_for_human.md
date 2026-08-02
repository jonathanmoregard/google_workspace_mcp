# Pending for human

## 2026-08-02: A second, NON-enrolled GCP project (to settle preview-enrollment scope)
**Why blocked**: `docs/preview-api-reference.md` open item 3 — does Developer
Preview enrollment propagate **per-project** or **per-account**? Every project
this repo can reach is enrolled (`498052759130`, account
`jonathan@klaffat.com`), so nothing here can produce the negative case. Settling
it needs a brand-new GCP project that is *not* enrolled, and the consent grant
for its OAuth client is a browser flow only a human can complete.
**Steps for human**:
1. console.cloud.google.com → **create a new project** (do not reuse
   `498052759130`, and do not apply for preview enrollment on it)
2. Enable **Google Docs API** and **Google Drive API** on it
3. OAuth consent screen → External → Testing → add `jonathan@klaffat.com` as a
   test user
4. Credentials → Create credentials → OAuth client ID → **Desktop app** →
   download the JSON
5. Put it in a fresh credentials directory, e.g.
   `~/.google_workspace_mcp/credentials-unenrolled/`, and complete the auth flow
   once in a browser, granting consent for the *same* Google account
6. Tell Claude, which then points `WORKSPACE_MCP_CREDENTIALS_DIR` at that
   directory and re-runs `check_docs_review_capabilities(probe=true)`. Same
   account, unenrolled project: `available` ⇒ enrollment is per-account;
   `unavailable` ⇒ per-project, and the classifier's documented inference
   becomes a measurement.
**Everything else done**: the classifier's three marker strings **have** been
validated against real proto-parse errors from the live API — the canonical
grammar, its query-parameter variant that drops `Cannot find field.`, and the
second grammar that carries none of them and is now classified
`("unknown", "request_not_parsed")` instead of `available`
(`docs/findings/errors-and-discovery.md`). What is left is narrowly *what a
non-enrolled project returns for a recognised-but-ungated request type*, not the
whole question. Both earlier entries in this file are resolved: the OAuth client
exists and the e2e suite runs against the real APIs, and Developer Preview
enrollment was granted — every finding in `docs/findings/` (tabs,
suggest-semantics, coverage, errors-and-discovery, merge) was measured through
it.
