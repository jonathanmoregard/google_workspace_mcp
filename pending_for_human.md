# Pending for human

## 2026-07-13: Google OAuth client credentials
**Why blocked**: e2e tests call the real Docs/Drive APIs; only a human can create OAuth credentials and grant consent.
**Steps for human**:
1. console.cloud.google.com → create/pick a project → enable **Google Docs API** and **Google Drive API**
2. OAuth consent screen → External → Testing → add your own email as test user
3. Credentials → Create credentials → OAuth client ID → **Desktop app** → download JSON → save as `~/Repos/gdocs-review-mcp/credentials/oauth_client.json`
4. When Claude first runs the auth flow, open the printed URL in a browser and approve (one time)
**Everything else done**: fork + 61-tool generated layer + curated layer built and unit-green (1425 tests); e2e harness runs GA surface the moment this lands.

## 2026-07-13: Workspace Developer Preview enrollment (long pole)
**Why blocked**: the July 2026 comment/suggestion API surface (anchored comments, SUGGEST mode, accept/reject) only activates for enrolled projects; enrollment is a human application.
**Steps for human**:
1. https://developers.google.com/workspace/preview → apply with the same GCP project as above
2. Tell Claude when approved — the preview e2e suite + discovery re-fetch (replacing the hand-written overlay with real discovery) both fire on it
**Everything else done**: preview tools are built from officially-documented shapes and unit-tested; GA surface is fully usable without enrollment.
