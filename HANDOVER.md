# HANDOVER — `docs_preview`, a Google Docs review surface

Written for an AI agent picking this repo up cold. It assumes you can read
code, so it points at files rather than restating them; everything below that
is load-bearing is stated outright.

Verified against the tree at commit `df64924` on branch `docs-preview`,
2026-08-01 (rounds 1-4 of the cross-vendor review loop applied). §3.6 and the
enrollment item in §7.2 were written against `00420aa` on branch
`feat/multi-account-routing`, 2026-08-27.

---

## 1. What this is

A fork of [`taylorwilsdon/google_workspace_mcp`](https://github.com/taylorwilsdon/google_workspace_mcp)
(a Python MCP server for Google Workspace) adding one service: **`docs_preview`**
— 7 tools that let an agent work with Google Docs **comments** and **edit
suggestions** the way a human reviewer does.

Built for **the requester**, who publishes web pages out of Google Docs and
runs heavy comment/suggestion review over them.

Requirements it satisfies (from the requester, recorded in
[`docs/plans/2026-07-13-mvp.md`](docs/plans/2026-07-13-mvp.md)):

| requirement | where |
|---|---|
| comments and suggestions each carry **id + author** | `author` on every record and reply; from the preview thread read |
| replies | `replies[]` on both thread kinds; `reply_to_doc_thread` writes them |
| quoted / commented text | `quoted_text` (the API's `plainTextQuote`) on comment threads |
| pre/post text for a suggestion | computed `pre_text` / `post_text` + two ~40-char context windows |
| read / edit / write / create | `get_doc_review_view`, `suggest_doc_edit`, upstream `gdocs` tools, `create_anchored_doc_comment` |
| accept / reject | `manage_document_suggestion` |

**Reactions are DESCOPED.** No API surface exposes comment reactions —
not Docs, not Drive. The only way to reach them is a browser sidecar, which
was explicitly rejected as a design constraint ("no browser automation").
See `docs/plans/2026-07-13-mvp.md:37` and
`docs/plans/2026-07-14-native-integration.md:572`.

The preview write/thread surface is the Google Docs API **Developer Preview**
launched 2026-07-07. It is not in the public discovery document and is gated
on enrollment (§2). Everything the GA `documents.get` can answer still works
without enrollment, in a clearly-flagged degraded mode (§4.6).

---

## 2. Setup

### 2.1 Developer Preview enrollment — do this first, it is the long pole

The preview request types (`insertComment`, `acceptSuggestion`,
`rejectSuggestion`, `addCommentReply`, `writeControl.writeMode: SUGGEST`, …)
and the thread-bearing read only exist for **enrolled projects**. Apply at
<https://developers.google.com/workspace/preview>.

- **Whether enrollment is a property of the GCP project, of the Google account,
  or of both, is an open question and the repo no longer claims an answer.** It
  has been tested neither with a second, non-enrolled project nor with a second
  Google account against this one; it stays listed as an open UNCERTAIN item
  (`docs/preview-api-reference.md`, item 3; §7.2), with the two experiments that
  would settle it.

  This used to read the other way. The not-enrolled error in
  `_execute_preview_batch_update` (`gdocs_preview/write_tools.py`) said
  "enrollment for the authenticated project", and this section cited that
  wording as evidence for the per-project reading — a claim resting on nothing
  but its own error message. The error now states only what was observed: that
  the preview request was rejected as not enrolled for that account.

  Everything downstream already modelled it correctly and is unchanged. The
  cached verdict is keyed by `user_google_email`
  (`gdocs_preview/preview_status.py`) and an account nothing has been observed
  for reads `unknown`. Per-account and unknown-until-observed is the model that
  stays correct under **either** answer — §3.6.

  One thing that is known independently of all this: the requester having
  org-level approval does not automatically cover a *new* GCP project, so
  register the project you actually build the OAuth client in.
- Without enrollment the server still starts and every read still answers —
  see §4.6.

### 2.2 GCP project + APIs

1. <https://console.cloud.google.com> → create or pick a project.
2. Enable **Google Docs API** and **Google Drive API**. (Drive is not
   optional: the comment factory and the e2e scratch-doc teardown both go
   through Drive v3.)
3. OAuth consent screen → External → Testing → add the reviewing account as a
   test user.
4. Credentials → Create credentials → OAuth client ID → **Desktop app** →
   download the JSON → save it at `credentials/oauth_client.json` in this
   repo. `credentials/` is gitignored; keep it that way.

Scopes requested are `BASE_SCOPES + DOCS_PREVIEW_SCOPES`
(`auth/scopes.py`, `e2e/gating.py:31`): Docs readonly + write, Drive full +
readonly + file. Full Drive scope is deliberate — it is what keeps comment
operations visible to collaborators, matching upstream `core/comments.py`.

### 2.3 Getting a token — two bootstrap paths

Both write the token into the **server's own** credential store, so the
server and the test suite share one token. Resolution order
(`e2e/gating.py:48`, mirroring `auth.google_auth.get_default_credentials_dir`):

```
WORKSPACE_MCP_CREDENTIALS_DIR  >  GOOGLE_MCP_CREDENTIALS_DIR  >  ~/.google_workspace_mcp/credentials
```

**Path A — the normal one** ([`e2e/bootstrap_auth.py`](e2e/bootstrap_auth.py)):

```bash
uv run python e2e/bootstrap_auth.py [--client PATH] [--credentials-dir DIR] [--port N]
```

Runs the installed-app flow: prints a consent URL, spins up a short-lived
localhost listener, waits for the callback.

**Path B — when Path A cannot work**
([`e2e/bootstrap_manual.py`](e2e/bootstrap_manual.py)):

```bash
uv run python e2e/bootstrap_manual.py start
# approve in ANY browser; copy the localhost URL it lands on
# (the page will show a connection error — that is expected and correct)
uv run python e2e/bootstrap_manual.py finish '<pasted localhost URL>'
```

Read that file's docstring before choosing. It exists for three concrete
failure modes of Path A, all observed:

1. the library's callback wait can be **outrun by a real consent flow** (the
   unverified-app interstitial alone can take longer than the listener
   lives), and the auth code is then unusable because its PKCE verifier died
   with the listener;
2. a **stale `localhost:<port>` tab** from an earlier attempt re-hits the new
   listener on reload and kills the flow with `MismatchingStateError` before
   the good callback lands;
3. **the browser is on a different machine** than the one running the code.

Path B persists the PKCE verifier and CSRF state to
`credentials/.oauth_pending.json` (gitignored) between the two halves and
uses the client's registered `http://localhost` redirect with nothing
listening, so the code stays exchangeable regardless of timing or host.

### 2.4 Running the server

```bash
uv run python main.py --transport stdio --single-user --tools docs docs_preview
```

`--tools` filters services; `docs_preview` maps to the `gdocs_preview`
package (`main.py` `SERVICE_MODULES`). With no `--tool-tier` flag all 7 tools
register; `--tool-tier core` drops `check_docs_review_capabilities`
(`core/tool_tiers.yaml`).

First call after setup: `check_docs_review_capabilities(probe=true,
document_id=<any doc you can edit>)`. That is the only way to confirm
enrollment, and it cannot mutate the document (§3.3).

---

## 3. The tools

### 3.1 The 7 `docs_preview` tools

Registered in `gdocs_preview/curated_tools.py` (read/diagnostic) and
`gdocs_preview/write_tools.py` (writes). Canonical list:
`curated_tools.REVIEW_TOOL_NAMES`.

| tool | returns |
|---|---|
| `list_document_suggestions` | one record per pending suggestion + a full accounting block (`suggestion_count` / `matched_count` / `returned_count`, `filters`, `page`, `read_source`, `tabs`) |
| `get_doc_review_view` | the document as a reviewer sees it: text with CriticMarkup markers (`{+ins+}` / `{-del-}`), optionally a paragraph map, plus the comment threads |
| `check_docs_review_capabilities` | scopes, tool inventory, and the cached-or-probed Developer Preview verdict |
| `suggest_doc_edit` | `created_suggestion_ids` + a post-write `verification` block echoing the created card's pre/post text, context and resulting range |
| `manage_document_suggestion` | accept/reject result + `verification` with `still_pending`, `matches_expectation`, `also_removed_suggestion_ids` |
| `reply_to_doc_thread` | the stored `Post` (id, author, content, create_time) + `comment_update_state`; self-verifying with no extra read |
| `create_anchored_doc_comment` | the stored `CommentThread` (comment_id, post_id, author, `anchor_id`, `quoted_text`, status) + `comment_update_state`; self-verifying with no extra read |

### 3.2 Non-obvious parameters

**`list_document_suggestions`**

| param | default | notes |
|---|---|---|
| `fields` | `"summary"` | `summary` or `full`. See §3.4 for why. |
| `page_size` | 134 (summary) / 43 (full) | The bound is **bytes, not cards**. `DEFAULT_PAGE_CHARS = 35_000` ÷ `CHARS_PER_RECORD` (260 summary / 800 full). Ceiling is `MAX_PAGE_CHARS = 50_000` → 192 / 62. A request above the ceiling is reduced **and says so** in `page.page_size_requested` + `page.page_size_note`. Constants: `gdocs_preview/review_page.py:128-151`. |
| `page_token` | – | Encodes the **last emitted suggestion id**, not an offset, because resolving a card renumbers everything after it. If the anchor id is gone (the normal working pattern), it falls back to the recorded ordinal **and the response says so**, since that can skip or repeat a card. Must be replayed with the same fields/filters; a token from a different query is refused. |
| `author` | – | Display name, case-insensitive, **exact — not substring**. On no match the response lists the authors present. |
| `status` | – | Thread status, e.g. `OPEN`. Case-insensitive exact. |
| `start_index` / `end_index` | – | Half-open `[start, end)`. Either bound alone is fine. Read in **one** `(tab, segment)` — see §4.1. Cards outside it are counted in `filters.excluded_other_segments`; the space used is echoed as `filters.range_scope`. |
| `segment_id` / `tab_id` | `None` = body of default tab | Both a filter and the coordinate space the index range is read in. |

**`get_doc_review_view`**

| param | default | notes |
|---|---|---|
| `view_mode` | `SUGGESTIONS_INLINE` | also `PREVIEW_SUGGESTIONS_ACCEPTED`, `PREVIEW_WITHOUT_SUGGESTIONS` |
| `fields` | `"text"` | `text` \| `paragraphs` \| `full`. The paragraph map's `text` values concatenate to `body_text`, so `full` restates a quarter of the response. |
| `start_index`/`end_index` | – | Narrows to overlapping paragraphs. `body_text` and the header/footer/footnote texts are **recomputed from exactly the paragraphs returned**, so no part of the response describes a different window than another. |
| `segment_id`/`tab_id` | – | Name the window's coordinate space *only*. Passed without a window they filter nothing, and the response says so in `scope_note` rather than looking scoped. |
| `include_comments` | `true` | `false` returns `comments: []` plus `comments_omitted: <n>` |

**`suggest_doc_edit`** — mode is inferred: `text` only → insertion;
`end_index` only → deletion; both → replacement (delete+insert in one batch).
`start_index` floor is **1 in the body** (index 0 is the section break) and
**0 in a header/footer/footnote** (each segment is numbered from its own
start; verified live 2026-07-31).

**`verify`** (on `suggest_doc_edit` and `manage_document_suggestion`,
default `true`) buys exactly **one** extra `documents.get` after the write and
turns the response into evidence rather than a receipt. `verify=false` returns
`verification.source: "skipped"`, `still_pending: null`,
`still_pending_unavailable: "not_verified"`, and a note saying the response ids
alone do not say the write landed. It keeps the **full documented key set**,
nulled (`_unverified_verification`) — the block used to carry five keys while
the docstring documented `matches_expectation` and friends unconditionally, so
a client reading them raised `KeyError` on the one path where nothing checked
the write. `pending_suggestion_count` is `null`, not `0`: a count is a claim
about the document. `resolved_suggestion` is echoed, since it comes from this
session's own listing rather than from a read. Set it false only for a batch
you will verify at the end — collateral removals (§4.5) then go unreported.

The verification block's **pending accounting is two numbers, not one**, for
the same reason the read tools' is (`docs/findings/coverage.md`):
`pending_suggestion_count`/`pending_suggestion_ids` are what this layer models,
`unreported_suggestion_count`/`unreported_suggestions` are the rest of what the
API lists as OPEN. `still_pending` is derived from **both**, so an id it calls
pending is always in one of the two lists; a review is done when both numbers
are zero. Both are emitted by the same `review_page.attach_unreported` the read
tools call, so the two surfaces cannot answer it differently
([`docs/findings/closeout-fixes.md`](docs/findings/closeout-fixes.md)).

`reply_to_doc_thread` and `create_anchored_doc_comment` have no `verify`
because the batchUpdate response already carries the stored object
(§4.4), so their echo is free.

### 3.3 The capabilities probe

`check_docs_review_capabilities` is side-effect free by default. With
`probe=true` (requires `document_id`) it POSTs the cheapest preview call that
**cannot mutate content**: a batchUpdate with a single `acceptSuggestion` for
a deliberately unresolvable id (`curated_tools._PROBE_SUGGESTION_ID`).
Classification lives in `gdocs_preview/preview_status.py`:

| outcome | verdict |
|---|---|
| 400 naming an unknown field / "cannot find field" / "invalid json payload" | `unavailable` — not enrolled; the request type was not even parsed |
| any other 400 | `available` — type recognised, failed only on the bogus id |
| 404 naming a missing suggestion/comment/reply | `available` (this is what prod actually returns, verified 2026-07-30) |
| any other 404, or 403 | `unknown` — proves nothing about enrollment |
| 200 | `available` |

The verdict is cached **per caller** (`user_google_email`, bounded to
`MAX_USERS`, oldest-touched evicted), and later probe-free calls by that same
caller report it. It was one process-global verdict, which crossed tenants in
the server's default multi-user mode: the probe-free branch makes no API call,
so caller B was answered entirely out of caller A's probe — including
`evidence.message`, which is the failed call's error text, and
`HttpError.__str__` embeds the request URI and therefore **A's document id**.
An unrecorded caller now gets `unknown`, which costs a probe rather than a
wrong belief (and enrollment being per-project vs per-account is still open —
§7).

### 3.4 Why `fields="summary"` is the default

Measured, not guessed (`gdocs_preview/review_page.py:14-27`,
`docs/plans/2026-07-30-large-review-sets.md`). The listing is linear in card
count and a document has no cap on it. At 120 pending suggestions the full
listing was **93,443 characters**, and a real client answered a
105,187-character tool result with

> `Error: result (105,187 characters) exceeds maximum allowed tokens. Output has been saved to /home/.../*.json`

and spilled it to a file the agent could not open. The agent never saw a
single suggestion id, spent its remaining turns trying to reach the spilled
file, and ended by asking an absent user to paste it in. **A default response
that cannot be delivered is not the conservative choice.**

`summary` costs ~232–252 chars/card and keeps everything a decision needs:
`suggestion_id`, `type`, `author` (display name as a plain string),
`summary_text` (Google's own label), the full address
(`segment`/`segment_id`/`tab_id`/`start_index`/`end_index`) and `status`.
What it drops is listed in the response's `omitted_fields`. `full` restores
`pre_text`, `post_text`, both context windows, `create_time`, the full author
object, `author_source`, the table flag and `replies`.

**Nothing is ever silently truncated.** Compare `suggestion_count` /
`matched_count` / `returned_count` and you know whether you have seen
everything; a non-final page carries `page.next_page_token` and a
`notice_page` saying so in words.

### 3.5 Comment lifecycle: `update` and `delete` (shared factory)

`core/comments.py` is upstream's per-app comment tool factory. The fork adds
two actions to `_manage_comment_dispatch` and their impls
(`_update_comment_impl`, `_delete_comment_impl`, both Drive v3):

- `update` — requires `comment_id` + `comment_content`
- `delete` — requires `comment_id`; permanently removes the comment and its
  replies

`MANAGE_COMMENT_ANNOTATIONS.destructiveHint` was flipped to `True` as a
consequence. Because the factory is instantiated three times
(`gdocs/docs_tools.py:2796`, `gsheets/sheets_tools.py:2435`,
`gslides/slides_tools.py:477`), this lands as `manage_document_comment`,
`manage_spreadsheet_comment` **and** `manage_presentation_comment` at once.
This is the obviously upstreamable piece (§8).

### 3.6 More than one account on one server

`list_google_accounts` — registered in `core/server.py`, tiered `docs: core`
in `core/tool_tiers.yaml` — reports every account the credential store holds,
which one is the default, and each one's cached Developer Preview verdict.
**It makes no API call and never probes an account.** Probing another identity,
even read-only, is itself an access attempt and has to be an explicit decision,
never an implementation detail inside a resolver. It sits at the `core` tier
rather than `extended` because a tool absent from every tier is silently
dropped for every `--tool-tier` user, and knowing which accounts exist is a
prerequisite for using any of them.

The whole feature lives in one fork-owned module,
[`core/account_directory.py`](core/account_directory.py) — enumeration, the
report, the server-instructions string, the arbitrary-pick warning — so what
upstream files carry is an import and a one-line delegation each (§5.2).

**Adding a second account.**

```
start_google_auth(service_name="docs", user_google_email="other@example.com")
```

Its credentials land beside the first in the same directory, one file per
account named from the URL-encoded address
(`LocalDirectoryCredentialStore._get_credential_path`), resolved in the same
order as §2.3: `WORKSPACE_MCP_CREDENTIALS_DIR`, then
`GOOGLE_MCP_CREDENTIALS_DIR`, then `~/.google_workspace_mcp/credentials`.
Nothing else needs configuring —
`enumerate_accounts()` reads the store rather than any registry of its own.

**Name the account explicitly, or you re-authenticate the one you have.**
`start_google_auth`'s signature defaults `user_google_email` to
`USER_GOOGLE_EMAIL`, *and* the `call_tool` override below injects that same
default into any call that omits it. `start_google_auth(service_name="docs")`
therefore runs the consent flow for the account already configured. Naming the
new address is the whole operation.

**The routing rule.** `USER_GOOGLE_EMAIL` (`core/config.py`; forced to `None`
under OAuth 2.1) is the default, and `SecureFastMCP.call_tool` in
`core/server.py` injects it into any call that omits `user_google_email` —
*before* FastMCP validates arguments against the function signature, since
pydantic would otherwise reject the call for the missing field. Switching
accounts is therefore exactly one thing: pass `user_google_email` yourself.
Two guards around that injection, both load-bearing:

- `_tool_takes_user_email()` checks the registered signature first. Injecting
  the default into a tool that has no such parameter is a pydantic
  `unexpected_keyword_argument`, which would have made `list_google_accounts`
  — which takes no arguments at all — uncallable on exactly the servers where
  it is worth having. An unknown tool name answers `True`, so it still fails
  the way FastMCP makes it fail.
- In trusted-gateway mode nothing is injected and a caller-supplied
  `user_google_email` is *dropped* rather than validated: the verified gateway
  assertion is authoritative, and older clients may still hold the pre-gateway
  schema.

**Substitution is refused, not silently performed.** Under `--single-user`
(`MCP_SINGLE_USER_MODE=1`, set in `main.py`), `auth.google_auth.get_credentials`
branches on whether an email was named. Named: it loads *that* user's
credentials from the store, and on a miss returns `None` — logging "no
credentials for requested user …; not falling back to another user". Unnamed:
`_find_any_credentials()` takes `users[0]`, which is the alphabetically first
address, since `LocalDirectoryCredentialStore.list_users()` returns
`sorted(users)`. That pick used to be silent; it now goes through
`warn_on_arbitrary_account_pick()`, which names every account found, names the
one it bound, and says `USER_GOOGLE_EMAIL` pins it. Silent for zero or one
account, where nothing about the choice is arbitrary.

**Why there is no automatic fallback.** The obvious feature — 404 under one
account, retry under the next — is refused on two independent grounds, either
of which is sufficient alone.

*The trigger is unreliable by construction.* Google documents `404 notFound`
as covering **both** "the user does not have read access to the file" **and**
"the file does not exist"
(<https://developers.google.com/workspace/drive/api/guides/handle-errors>).
Nothing in the response separates the two. A fallback keyed on that error
cannot tell a document that another identity really can read from a document
id with a typo in it — and on the typo it does not fail fast, it walks every
credential in the wallet, turning one mistyped character into an enumeration
sweep across identities. The cost grows with the number of accounts held,
which is the wrong direction for a feature whose entire purpose is holding
several.

*A retried write cannot be taken back.* Comments and edit suggestions are
authored **as** the account and are visible to everyone else with the document
— §3.1: both thread-write tools return the stored object carrying its
`author`, which is exactly the field a reader sees. A retry under a second
identity permanently attributes a comment or suggestion to the wrong person in
someone else's document. Deleting it afterwards does not unsend it to anyone
who already read it, and the owner's notification has already gone out. There
is no post-hoc repair, so there is no tolerable error rate.

So the design **surfaces the affordance and does not take the action.** The
contract on an error path:

> On a 403, a 404, or a preview verdict of `unavailable`, the error NAMES the
> other authenticated accounts and says outright that no call was attempted
> under any of them. Using one is a second, explicit call, with
> `user_google_email` set — never the accounts tried in turn.

`candidate_account_hint()` in `core/account_directory.py` produces that
sentence, to be appended verbatim to an existing error message. Naming costs a
directory listing and nothing else; attempting costs an access under an
identity nobody chose. Four properties are worth knowing before you rely on
it:

- **401 is deliberately not in `_STATUS_HINTS`.** It means nobody is
  authenticated for this call, not that the wrong identity was used, so the
  fix is to re-authenticate — pointing at a second account there would invite
  exactly the retry this feature exists to prevent.
- **`other_accounts()` returns empty rather than guess**, in four cases: the
  two managed-identity modes (those addresses belong to other tenants); a
  store that could not be enumerated ("there are no other accounts" must never
  be inferred from "the store could not be read"); a caller whose own account
  is unknown (`handle_http_errors` substitutes `"N/A"` for a tool with no
  `user_google_email` keyword); and a store that does not contain the caller's
  account, where the enumeration is describing a different world from the one
  the call ran in.
- **It is empty with zero or one account**, so the inherited error messages
  stay byte-identical on a single-account server.
- The 404 variant carries the `notFound` ambiguity above, and the
  preview-`unavailable` variant carries the §7.2 caveat — the verdict is about
  the calling account only and says nothing about the others'.

The rule is stated to the model in three further places, because it is the one
thing an agent will otherwise do on its own initiative: `list_google_accounts`'s
docstring ("Seeing an account here is NOT permission to use it"), the report's
own `notes` (`_ROUTING_NOTE` and `_WRITE_NOTE`, appended only when the store
holds more than one account), and the multi-account server instructions.

**Capability is per-account and unknown-until-observed.**
`gdocs_preview/preview_status.py` keys the Developer Preview verdict by
`user_google_email` (§3.3), bounded to `MAX_USERS = 64` with the oldest-touched
evicted, and `get_status()` answers `unknown` — never `unavailable` — for an
account nothing has been recorded for. An evicted account also falls back to
`unknown`, i.e. to re-probing, never to somebody else's verdict.
`account_directory.preview_capability()` projects three fields from that state
(`availability`, `source`, `checked_at`) and deliberately drops `evidence`:
evidence is the failed call's error text, and `HttpError.__str__` embeds the
request URI, i.e. a **document id** belonging to whichever account produced
it.

`unknown` is the honest answer while §7.2's question is open, and it is the
cheap one under either resolution. If enrollment turns out to be per-project,
every account on one OAuth client shares one verdict and per-account keying
costs one redundant probe per account. If enrollment has a per-account
component, a shared verdict is a wrong belief about an identity that was never
tested. That asymmetry is the whole reason the key is what it is. **An
`available` verdict for one account is not evidence about another**, and
nothing in this repo treats it as one.

**The two managed-identity modes are not enumerated at all.** In
trusted-gateway and OAuth 2.1 multi-user mode the credential store is shared
across principals, so `build_account_report` returns
`store_status: not_enumerated` with an empty account list and a note saying
why (`_managed_identity_report`). Handing one caller the other callers' email
addresses is a cross-tenant leak, and the caller could not route to them
anyway: the account is fixed per request by the gateway assertion or by the
authenticated principal. Note that `_identity_mode()` labels everything else
`single_user`, which here means *neither gateway nor OAuth 2.1* — a broader
set than the `--single-user` flag, which is what gates the refusal branch
above.

**One account behaves exactly as before.** `build_server_instructions` emits
the multi-account string only when the store enumerates successfully *and*
holds more than one account. One account, zero accounts, one account that is
not the configured default, or any store failure all yield
`_single_account_instructions`, whose bytes a test asserts literally
(`tests/core/test_account_directory.py::test_single_account_instructions_are_byte_identical`)
precisely so a fork that means to stay merge-friendly cannot drift the
single-account surface. That builder runs at **import time** — FastMCP's
constructor needs the value — so it uses `peek_credential_store()` rather than
the cached `get_credential_store()` (caching a store built before `main.py`
loads `.env` would hand every later caller the wrong credentials directory),
and absorbs every store failure rather than raising: a GCS deployment must not
fail to start because of this feature.

Enumeration also distinguishes "no accounts" from "could not read the store".
`LocalDirectoryCredentialStore.list_users()` returns `[]` for a missing
directory *and* for one it cannot read — it swallows `OSError` — so
`_describe_local_directory()` looks at the directory itself before the report
is allowed to say zero. `store_status` carries the difference (`ok`,
`unreadable`, `unsupported`, `unavailable`, `not_enumerated`) and
`accounts_enumerated` is the flag every absence claim rests on, the same way
`complete` is for a read (§4.6).

---

## 4. Load-bearing API facts

Every claim here is checked against the code that implements it. Fuller
transcription in [`docs/preview-api-reference.md`](docs/preview-api-reference.md).

### 4.1 An index is only half of an address

Docs numbers **every `(tabId, segmentId)` pair from its own start**. `start_index`
412 in a footnote and 412 in the body are different characters in different
coordinate spaces. Every write tool defaults to `tab_id=None`/`segment_id=None`,
which the API reads as *the body of the default tab*.

So an agent that reads a bare index out of a response and hands it back
writes into the body at that number — **successfully, silently, in a customer
document**. Index 0 fails loudly on the floor check; every other index does
not.

Two structural consequences, both enforced in code:

- **`gdocs_preview/address.py` is the only module that projects an index into
  an agent-facing payload**, and it cannot project one without its
  `segment`/`segment_id`/`tab_id`. `address_of()` returns all five
  `ADDRESS_FIELDS` or none. This bug class was found three separate times
  (summary cards, the post-write echo + resolution ledger, the coordinate-space
  filters) before the emitter was centralised.
- **Two indexes may only be compared when numbered in the same space.**
  `resolve_range_scope()` decides which space a caller meant — resolving an
  omitted `tab_id` when the *document* has exactly one, **refusing to guess**
  when it has several — and takes the document's tab inventory as a required
  argument. It previously counted the tabs occupied by the caller's *records*,
  so a three-tab document whose cards all sat in one tab presented as
  single-tab and silently resolved to it.

**`segment_id` and `tab_id` are a pair, not two independent options.** A
segment id is resolved *within* the tab the request names. A segment id sent
without its tab is a 400 against a multi-tab document — `"Segment with ID
kix.… was not found"`, measured against the live API 2026-07-31 — with
nothing in the response saying which tab would have worked. Send both or
neither (`gdocs_preview/write_tools.py:2170`).

### 4.2 Docs omits `startIndex: 0`

The API serialises proto3, which omits default values, so index 0 is never
written out. Verified live 2026-07-31: a header segment's only paragraph came
back as `{"endIndex": 13, "paragraph": …}` with **no `startIndex` at all**.

Index 0 is only reachable in a header, footer or footnote (a body's first
paragraph starts at 1), so reading the absence as "no index" made every
suggestion at the start of such a segment unaddressable: `start_index: null`,
excluded from every index-range filter, nothing to hand back to
`suggest_doc_edit`. `gdocs_preview/analysis.py` applies the default **only
where `endIndex` is present**, i.e. only to elements the payload did index.

This is the *one* place the module derives an index. Everywhere else, indexes
are passed through verbatim — they are UTF-16 code units, Python strings are
code points, and `analysis.py` never computes a document index from a Python
string length. Context windows are sliced for display only and never fed back
to the API.

### 4.3 Threads (and therefore authors) need two parameters, one of which the client rejects

Comment and suggestion **thread** objects — and with them every `author`,
`status`, `create_time`, `summaryText` and `replies` — are absent from a plain
`documents.get`. They appear only for:

```
GET https://docs.googleapis.com/v1/documents/{id}
      ?suggestionsViewMode=SUGGESTIONS_INLINE
      &commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED
      &includeTabsContent=true
```

- `includeTabsContent=true` is **mandatory** alongside `commentsViewMode`.
  Without it: `400 "Comments view mode may only be specified if tabs content
  is also requested."`
- `commentsViewMode` is **not in the public discovery document**, so the
  `googleapiclient` Resource refuses it *before any request is sent*:
  `TypeError: Got an unexpected keyword argument commentsViewMode`.
  `documents().get` accepts only `documentId`, `suggestionsViewMode`,
  `includeTabsContent` and system parameters.

[`gdocs_preview/preview_read.py`](gdocs_preview/preview_read.py) handles this
by trying the client first and, on exactly that `TypeError`, falling back to a
`google.auth.transport.requests.AuthorizedSession` built from the credentials
the injected Resource already holds (`service._http.credentials`). Patching
discovery for one query parameter was judged too large a blast radius. When
Google publishes the parameter the client path simply starts working and the
fallback goes quiet.

Asking for tabs content **removes the top-level `body`** — content moves to
`tabs[i].documentTab`. `preview_read.tab_documents()` flattens each tab
(depth-first through `childTabs`) back into a GA-shaped `Document` so
`analysis.py` has exactly one input shape. `tabs[0].documentTab.body` is
byte-identical to the GA read's `body` for a single-tab document: **indexes
are unchanged** across the two reads.

Threads arrive at the **top level** as `suggestions[]` and `comments[]`, with
no tab attribution of their own — the repo attributes a suggestion to the tab
whose body carries its id. Empty repeated fields are omitted proto3-style, so
a document with suggestions but no comments has no `comments` key at all.

### 4.4 The response's ids are not proof the write took effect

The batchUpdate `Response` union does gain `insertComment.commentThread` and
`addCommentReply.post`, carrying the full object **including `author`**
(verified 2026-07-30) — that is why the two thread-write tools need no
follow-up read to report authorship. Requests producing no response member
(e.g. `insertText`) occupy their index with `{}`, so `replies` stays 1:1 with
the request list.

But **those ids are a receipt for the request, not evidence of effect.** A
resolution that resolved nothing can come back HTTP 200 sitting beside a
populated `rejected_suggestion_ids` and `commentUpdateState: ALL_SAVED`
(`tests/gdocs_preview/test_write_tools.py::TestAnUnverifiedResolutionSaysSo`).
Against prod, the repeat-reject path was observed answering **200 with an
*empty* id list** rather than erroring
(`e2e/test_preview_surface.py::test_a_reject_that_takes_no_effect_is_never_reported_as_a_match`);
both shapes are handled, neither is treated as proof.

So the tool derives its verdict from **structure first**:

> An accepted or rejected suggestion is gone from the post-write pending set.
> An id still in it is a resolution that did not take effect, whatever the
> text reads.

`_ResolutionVerdict` (`gdocs_preview/write_tools.py:630`) is the only producer
of the `(still_pending, matches_expectation)` pair, and `__post_init__`
re-checks the field against the same rule the factory used, so a verdict not
entailed by its own evidence **raises**. `matches_expectation` is therefore
never true while `still_pending` is true. This matters most on a **reject**,
where base text is identical whether or not the reject landed (the
suggestion's insertion is stripped either way, its deletion kept either way),
so the text alone cannot distinguish the two worlds — and equally on a
style-only accept, whose `pre_text` and `post_text` are one string.

For thread writes there is no pending set, so `verification.saved` is the
API's own `commentUpdateState` **and nothing else**: `true` on `ALL_SAVED`,
`false` on a reported failure state, `null` when the API sent no state at all.
**Null is not false** — on a null, re-read rather than retry, since a needless
retry leaves a duplicate reply or comment in the document. An HTTP 200 that
reports `ALL_FAILED_UNKNOWN_REASON` is raised as an error, never returned as
success (`_execute_preview_batch_update(..., enforce_comment_update=True)`).

### 4.5 Same-author merges, and collateral removal

- **Adjacent same-author suggestions merge.** Confirmed against prod for two
  overlapping same-author edits made in **separate batches**. A merged write
  returns **no `createdSuggestionIds`** — the API reports nothing new was
  created. `suggest_doc_edit` then echoes the suggestion(s) now covering the
  edited range under `verification.suggestions_at_edit_range` (with the
  `range_scope` they were read in) plus a note explaining the merge, rather
  than reporting an empty result or inventing an id.
- A SUGGEST replacement (`deleteContentRange` + `insertText`) yields **one**
  suggestion id: request 0 reports it under `createdSuggestionIds`, request 1
  reports the *same* id under `updatedSummarySuggestionIds`.
- **Resolving a suggestion deletes others.** Any suggestion whose last marked
  character disappears with the resolution is garbage-collected, along with
  its comment thread. The next call naming such an id fails with a bare
  `"Suggestion with ID … does not exist."`, indistinguishable from a typo.
  `gdocs_preview/suggestion_ledger.py` is the memory that fixes this: every
  read feeds it the live records, every resolution diffs the before/after
  reads, and `verification.also_removed_suggestion_ids` names the casualties
  so the error never has to happen. `explain_missing()` answers on an explicit
  honesty ladder — *resolved directly* (proven) → *collateral* (observed, not
  proven; a concurrent editor could have done it) → *may have been removed* →
  *never seen*.
  Rung 1 additionally needs the resolution to have **landed**. Calling
  accept/reject is not evidence that it worked — an HTTP 200 that resolves
  nothing is a shape prod returns — so every `Resolution` carries `landed`,
  set from the same derived `still_pending` the response reports. On `False`
  (the post-write read still listed the id) the answer says the call did not
  remove it and points away from this session; on `None` (`verify=false`, or
  the post-write read failed) it offers the resolution as the likely cause
  rather than a proven one. `collateral_note` **and the collateral rung of
  `explain_missing`** are worded off the same field — the collateral rung is
  causation too, and it went on asserting the GC rule for a round after the
  direct rung was fixed.
  `record_resolution` separates `suggestion_id` (the id an action was aimed
  at) from `cause` (the id collateral is explained by). They coincide for
  accept/reject and must not for a merge: `_verify_suggest` passed the
  just-CREATED id as `suggestion_id`, filing a "how it went away" record for a
  card that had just arrived, so a later lookup of it answered "You
  suggest_doc_edited it yourself".
  **Every rung** reads `landed`, including *may have been removed* — a
  resolution the read contradicted is not offered as a possible remover of
  anything, and an unverified one is offered with that said. And a read is
  evidence about the resolutions already on file: `observe()` flips a
  `landed=None` record to `False` when a later read still lists that id as
  pending. `record_resolution` only drops its cached record when the removal
  was confirmed — the verify-less paths never call `observe()` afterwards, so
  dropping there erased the only copy of a still-pending card and the next
  attempt was told "this session never listed" it.
  The merge sentence states the **observation** before the mechanism, in both
  places it is written: all a write sees is "listed before, absent after", and
  a second reviewer resolving their own card in that window is
  indistinguishable from here.
  `observe()` **merges rather than replaces on a degraded read**. A degraded
  read cannot attest an absence, so replacing turned "this read did not look
  there" into "we never saw this": a complete listing caching A and B,
  followed by a resolution whose post-write read degraded, dropped B — and the
  next verification of B said "this session never listed" it. A complete read
  still replaces, because it *is* authoritative about absence.
- Also-created-since: a card that merely **appeared between the last listing
  and this write** — which is what a second reviewer looks like — is reported
  under `appeared_since_last_read`, never under `created_suggestions`, which
  claims authorship. Check the author before resolving or replying to one.
  "Appeared since" is a claim about the **listing**, so both halves of that
  subtraction are gated on `before.complete`: a card of *ours* is promoted to
  `created_suggestions` only when the prior listing could see the whole
  document. After a degraded listing every pre-existing card of ours in the
  tabs it could not see is "new" to the diff without anything having happened,
  and it used to be claimed by the write. The API's own
  `createdSuggestionIds` is proof and is unaffected either way.

### 4.6 A degraded read reports UNKNOWN, never a guess

If the preview read fails for any reason (not enrolled, network, or a payload
with an empty `tabs` array — every Google Doc has at least one tab, so that is
a broken payload, not an empty document), `read_for_review` falls back to the
GA `documents.get` and returns `ReviewRead(source="ga_documents_get",
degraded_reason=…, complete=False)`.

`complete` is not a diagnostic. **It is the premise every absence claim
downstream rests on.** The GA read returns *one unnamed body and no tab ids at
all*. An id missing from a complete read is missing from the document; the
same id missing from the GA fallback may be sitting in a tab that read
structurally could not see. It defaults to `False` so a hand-assembled read
cannot attest an absence it never checked.

Concretely, on a degraded read:

- thread-derived fields (`author`, `status`, `create_time`, `summary_text`,
  `replies`) are `null`, with `author_source: "unavailable"` — **never
  guessed** — and both field modes carry `degraded_notice` + `null_fields`,
  because the nulls are a property of the read, not of the document;
- an `author`, `status` **or `tab_id`** filter is **refused**, not answered
  with an empty page: `matched_count: 0` there means "this read cannot see
  authors", not "there are none". `tab_id` belongs to that list for the same
  reason and was missing from it — the GA payload is one *unnamed* body, so
  every record carries `tab_id: null` and any named tab matched nothing,
  answering `matched_count: 0` with `tabs_present: []` about a read that
  cannot see tabs at all;
- `still_pending` is `null` with `still_pending_unavailable` ∈
  {`segment_not_in_read`, `read_incomplete`}, `matches_expectation` is `null`,
  and `also_removed_suggestion_ids` is withheld in favour of
  `also_removed_suggestion_ids_unavailable`. Nothing in the response reports
  on the write; re-read with `list_document_suggestions` rather than repeating
  the resolution.
- **Counts are partial.** `suggestion_count` on a degraded read is the count
  of what that read could see, and `read_source` says which read it was.
- `get_doc_review_view` carries its own `degraded_notice` plus
  `comments_unavailable: "read_degraded"`. Its losses differ from the
  listing's: comment threads exist only on the preview read, so `comments: []`
  there is a fact about the read, and `tabs: []` plus the prose are one
  unnamed body's. Both were unqualified absence claims on the tool an agent
  actually reads a document with.

### 4.7 Miscellaneous, cheap to get wrong

- `SuggestionThread.headPost` has **no `content`** (unlike a comment head
  post); the human-readable label is `summaryText` on the thread.
- `summaryText` grammar uses **typographic** quotes (U+201C/U+201D):
  `Add: "Say"`, `Delete: "brave"`, `Replace: "brave" with "bold"`. An earlier
  ASCII guess lost to prod.
- `PostAuthor.anonymous` is **omitted entirely when false**, so a missing
  `anonymous` means unknown, not `False`. `normalize_author` preserves that.
- A bogus suggestion id is **HTTP 404**, not 400.
- `Post.content` caps at 2048 UTF-8 code units.
- Docs comment ids interoperate with Drive v3: a thread created with
  `insertComment` is visible to Drive `comments.list` and manageable through
  it. But the Docs surface is strictly richer — Drive exposes no anchor id, no
  per-post ids and no People resource names. Use `get_doc_review_view` for
  review, `list_document_comments` (Drive) for cross-surface management.
- Eight request types are unsupported in SUGGEST mode (`addDocumentTab`,
  `createNamedRange`, `deleteFooter`, `deleteHeader`, `deleteNamedRange`,
  `deleteTab`, `updateDocumentTabProperties`, `updateTableColumnProperties`).

---

## 5. Architecture

### 5.1 `gdocs_preview/` module map

| file | responsibility |
|---|---|
| [`address.py`](gdocs_preview/address.py) | The only projector of an index into an agent-facing payload; owns `ADDRESS_FIELDS`, range-scope resolution and the membership test (§4.1). |
| [`analysis.py`](gdocs_preview/analysis.py) | Pure, side-effect-free analysis of Document JSON: suggestion extraction, pre/post text, context windows, rendering, resolution checking. No service object, no I/O. |
| [`preview_read.py`](gdocs_preview/preview_read.py) | The **one** read every tool performs. Issues the thread-bearing preview GET (with the AuthorizedSession fallback), normalises tabs → GA-shaped Documents and threads → snake_case records, degrades to GA and reports it. |
| [`review_page.py`](gdocs_preview/review_page.py) | Field selection, filtering, pagination and the accounting block, for both read tools. Pure: records in, projected page out. |
| [`write_tools.py`](gdocs_preview/write_tools.py) | The 4 write tools + the whole post-write verification layer (`_ResolutionVerdict`, `_PostWriteRead`, the unavailable-reason vocabulary and its notes). |
| [`curated_tools.py`](gdocs_preview/curated_tools.py) | The 2 read tools + the capabilities probe. Thin API-call wrappers over `preview_read` and `analysis`. |
| [`suggestion_ledger.py`](gdocs_preview/suggestion_ledger.py) | Process-wide memory of what our own writes did, keyed by `(user_email, document_id)`, bounded to `MAX_DOCUMENTS` (oldest-touched evicted). Turns "does not exist" into a cause (§4.5). |
| [`preview_status.py`](gdocs_preview/preview_status.py) | Preview-availability verdict **keyed by `user_google_email`** (§3.3) + the error classifier. |

Importing `gdocs_preview` registers all 7 tools via decorator side effects.

### 5.2 Integration points into upstream

The design target is a **self-contained module plus a handful of registration
lines**, so the fork stays merge-friendly. The canonical three
(`docs/plans/2026-07-14-native-integration.md:189`):

1. `main.py` — `SERVICE_MODULES["docs_preview"] = "gdocs_preview"` (plus an
   emoji in the startup banner).
2. `auth/scopes.py` — `DOCS_PREVIEW_SCOPES`, registered in `TOOL_SCOPES_MAP`
   and `TOOL_READONLY_SCOPES_MAP`.
3. `core/tool_tiers.yaml` — a `docs_preview:` section (6 core, 1 extended).

In the current tree there is a **fourth**: `auth/permissions.py` gained a
`docs_preview` entry in `SERVICE_PERMISSION_LEVELS` (`readonly` / `full`),
which the design note predates. Everything else the fork touches upstream is
either the comment factory (§3.5), a `gdocs/docs_tools.py` index-0 correctness
fix, `pyproject.toml` (hypothesis + three markers), `.gitignore`, or docs and
tests.

### 5.3 The two invariants the design is built on

**One address emitter.** *An index may not appear in an agent-facing payload
without its `segment`, `segment_id` and `tab_id`.* Enforced structurally
rather than by vigilance: `address.py` is the only module that can emit one,
and it cannot emit the numbers alone. Dropping the pairing requires deleting a
name from `ADDRESS_FIELDS`, where the docstring stating the rule is looking at
it.

**Derived verdicts.** *A verdict is computed from its evidence, never
assembled beside it.* `_ResolutionVerdict.derive()` is the only producer of
`(still_pending, text_check, matches_expectation)`, and the constructor
re-derives and raises on a mismatch — the contradictory pair is
unrepresentable. `still_pending` is `Optional[bool]` so **UNKNOWN is a
first-class input** that must be propagated, not collapsed to `False`. Four
consecutive review rounds found this verification claiming success on a write
it had not verified, each time through a different branch; deriving rather
than comparing is what retires the branch class.

---

## 6. Testing

### 6.1 Unit suite (no credentials, no network)

```bash
uv run pytest tests/          # or: .venv/bin/python -m pytest tests/
```

**2455 tests collected** (2026-08-02). Config is entirely in
`pyproject.toml [tool.pytest.ini_options]` — there is no `pytest.ini` and no
root `conftest.py`. Notable sub-suites: `tests/gdocs_preview` 467,
`tests/llmux` 168, `tests/llmux_runner` 120, `tests/core` 168,
`tests/mockdocs` 85, `tests/mockdocs_concurrency` 64, `tests/e2e_harness` 38
(the e2e gating/report/session logic, unit-tested with no credentials).

CI (`.github/workflows/pytest.yml`) runs bare `uv run pytest` from the repo
root, which also collects `e2e/` — safely, since those skip without
credentials.

### 6.2 Real-API e2e

[`e2e/README.md`](e2e/README.md) is the authority. The suite spawns the real
server as a subprocess and speaks MCP to it; nothing is mocked.

```bash
uv run pytest e2e -m e2e_ga        # 26 tests — needs only an OAuth token
uv run pytest e2e -m e2e_preview   # 54 tests — additionally needs enrollment
uv run pytest e2e -rs              # everything eligible, with skip reasons
```

- **`e2e_ga` (26)** — gated on **credentials, not env vars**. The `ga_auth`
  session fixture resolves the credential dir, inspects the token offline,
  and hard-skips with actionable instructions on any non-`ok` status
  (`no_credentials_dir`, `no_token`, `unreadable`, `no_refresh_token`,
  `missing_scopes`, `refresh_failed`). It **never** starts an interactive
  flow.
- **`e2e_preview` (54)** — everything above plus a live
  `check_docs_review_capabilities` probe against a scratch doc, once per
  session; skips with the probe's classification evidence embedded in the
  message if the verdict is not `available`.
- `e2e/conftest.py` raises a `UsageError` if any test under `e2e/` lacks one
  of the two markers, so the directory is marker-covered by construction.
- Hygiene: scratch docs are created **through the MCP surface**, trashed in
  fixture teardown via a direct Drive client (works even if the server died),
  and re-audited against Drive by `test_zz_teardown_audit.py`. No
  sleeps-as-synchronisation — `e2e/util.py:poll_until`.
- Artifacts: `e2e/last_run.md` (override `E2E_RUN_REPORT_PATH`) and
  `e2e/_artifacts/server-*.log`. **Both gitignored**; the run report contains
  real account identity and document ids.

### 6.3 `mockdocs/` — the in-memory Google Docs model

A spec-faithful reimplementation of Docs suggesting mode, specified in
[`docs/plans/2026-07-30-suggestion-mock-spec.md`](docs/plans/2026-07-30-suggestion-mock-spec.md)
(§14 onward is the implementation's own addendum).

| file | responsibility |
|---|---|
| `graphemes.py` | The two unit systems: approximate UAX-#29 grapheme splitting and `utf16_len()`. The only place either is computed. |
| `model.py` | The pure model: per-`(tabId, segmentId)` `Char` arrays with conjunctive `ins` / disjunctive `del` mark sets, a document-wide suggestion+comment registry, insert/delete/replace, same-author merge, accept/reject, the three projections, §8 labels, and invariants I1–I5 via `check_invariants()`. Counts graphemes only. |
| `adapter.py` | Model ↔ Docs API payloads: grapheme↔UTF-16 conversion, `document_payload` vs `tabs_document_payload`, `writeControl.writeMode` SUGGEST/EDIT, proto3 omission of `startIndex: 0`, and the non-enrolled 400 simulation. |
| `fake_services.py` | Duck-typed googleapis service objects — `FakeDocsService` / `FakeDriveService` matching the exact call shapes the tools use, behind a `FakeBackend` with `me` / `not_enrolled` / `fail_comment_updates` flags. |
| `state.py` | Lossless JSON snapshot of a backend, atomically rewritten on every call, so an out-of-process grader can read end state. |
| `serve.py` | **Mock-backed MCP server entry point.** |
| `concurrency.py` | The scripted second editor (§6.5). |

The **API boundary is UTF-16 while the model counts graphemes** — deliberately,
so fixtures with astral-plane emoji and combining sequences exercise
`analysis.py`'s index discipline for real.

Run the mock-backed server (no token, no network, zero diffs to upstream
files — it rebinds `auth.service_decorator._authenticate_service` and nothing
else):

```bash
python mockdocs/serve.py --transport stdio --single-user --tools docs docs_preview
```

Env knobs: `MOCKDOCS_SEED`, `MOCKDOCS_ME`, `MOCKDOCS_NOT_ENROLLED`,
`MOCKDOCS_FAIL_COMMENTS`, `MOCKDOCS_STATE_DUMP`, `MOCKDOCS_INTERFERENCE`.

### 6.4 `llmux/` — the LLM-UX benchmark

Measures **tool-surface ergonomics, not model IQ**. One run = a fresh
mock-backed server seeded from a scenario + a headless `claude -p` process
whose only capability is that server, then the backend's end state read out
and handed to the scenario's `grade()`. Reports lead with a **mistake
taxonomy** rather than a pass rate, on purpose. Verdicts are three-valued:
`PASS` / `FAIL` / `INCONCLUSIVE` (rate-limited, wall-clock-killed, or a
contaminated tool surface); INCONCLUSIVE runs are excluded from every rate.

Ground truth is **computed, never authored**: each scenario is built twice —
through the model and replayed as real MCP calls — and generation fails if
they disagree.

Corpora: `llmux/scenarios/generated/` (17 scenarios, easy→adversarial),
`llmux/scenarios/stress/` (4: 30/60/90/120 suggestions on real 1,500–1,800
word articles), `llmux/interference/` (5, §6.5), `llmux/runner/_fixtures/`
(3 cheap harness smokes). All from the repo root:

```bash
# free — always do this first
uv run python -m llmux.runner.run --corpus llmux/scenarios/generated --dry-run --all
uv run python -m llmux.scenarios.validate          # corpus gate, free
uv run python -m llmux.runner.analyze llmux/runner/reports/<stamp>/runs   # rebuild a report, free

# one scenario
uv run python -m llmux.runner.run --scenario easy-kind-split --models sonnet --limit 1 --concurrency 1 --yes

# a batch
uv run python -m llmux.runner.run --corpus llmux/scenarios/generated --all --models sonnet,opus
```

> ### COST WARNING — this spends real Anthropic API money
>
> The runner spawns the **Claude Code CLI** (`claude -p`) against the real
> API. `--models sonnet` resolved to `claude-sonnet-4-6`; `opus` to
> `claude-opus-5`, roughly 4× the per-run cost.
>
> Measured batches: generated full matrix (32 runs) **$6.04 / 23 min**; the
> stress corpus (8 runs) **$17.08 / 46 min**; a *single* `stress-120` run
> **$3.18 / 94 turns / 9 min**; interference (5 runs) **$1.33**.
>
> Guards: `--limit` defaults to **3** (cheapest first) precisely so a careless
> invocation costs ~$1 rather than $17; `--max-budget-usd` defaults to $1.00
> **per run**; a 600 s wall clock; `--concurrency` hard-capped at 3. There is
> **no `--max-turns`** — turns are bounded only by budget and wall clock.
>
> **On a TTY the cost estimate is confirmed interactively; a
> non-interactive run proceeds automatically without asking.** Anything
> scripting this needs its own gate.
>
> A mandatory pre-flight tool probe (one `haiku` turn) aborts the batch if any
> non-MCP built-in is advertised to the agent — a stale deny list makes every
> run INCONCLUSIVE. One batch burned **$12.82** that way before the probe
> existed. `--skip-toolprobe` disables it.

Reports go to `llmux/runner/reports/<stamp>{.md,.json}` plus
`reports/<stamp>/runs/<scenario>__<model>/` (argv, mcp-config, transcript,
state, run.json). **Gitignored** — they contain full transcripts and document
state.

The one paid test in the pytest suite is opt-in:
`LLMUX_SMOKE=1 uv run pytest tests/llmux_runner/test_smoke_e2e.py`.

### 6.5 The concurrency harness

A **scripted second editor**, not real concurrency. Engine:
`mockdocs/concurrency.py` — no threads, timers or sleeps; the clock is the
agent's own call sequence (`{"tool": "list_document_suggestions", "nth": 2}`,
phase `before`/`after`). `InterferenceMiddleware` holds one lock across
`on_call_tool` so parallel `tool_use` blocks still serialise into one total
order. Other-editor edits go through the same model ops as the seed replay,
and invariants are re-checked after every firing and **recorded on the backend
rather than raised**, so a harness bug stays distinguishable from an agent
mistake at grading time.

Runner wiring is `llmux/runner/interference.py` (additive, so scenarios
without interferences behave exactly as before); the 5 cases are
`ix-vanished-id`, `ix-stale-indexes`, `ix-thread-gone`, `ix-merge-absorb`,
`ix-overlap-both-marks`. Grading runs a harness gate first (`HARNESS:`-prefixed
failures), then replays the correct end state, then scores adaptation —
**blind retry is not credited even when the end state comes out right.**

```bash
uv run pytest tests/mockdocs_concurrency                                  # 64 tests, free
uv run python -m llmux.runner.run --corpus llmux/interference --all --dry-run   # free
uv run python -m llmux.runner.run --corpus llmux/interference --all       # SPENDS TOKENS
```

---

## 7. What was open, what was measured, and what is still unknown

Everything in this section was, until 2026-08-02, either untested or a guess.
It was then put to the live API. The evidence for each answer — verbatim
requests, responses and error strings — is in [`docs/findings/`](docs/findings/);
this section is the index, not the record.

Five findings changed the code. Two of them were bugs of the class this repo
exists to prevent: a response that asserted more than its evidence supported.

### 7.1 Answered, and the code changed

| question | answer | evidence |
|---|---|---|
| Nested tabs | Creatable via `addDocumentTab` + `tabProperties.parentTabId`; `tab_documents()` flattens them correctly. **But `index` is position among *siblings***, so a document whose first tab had a child returned **two tabs at `index: 0`** and the flat inventory presented a nested tab as a colliding top-level one. Tab metadata now carries `parent_tab_id` and `nesting_level`. | [`tabs.md`](docs/findings/tabs.md) |
| Tab attribution of comments (UNCERTAIN 4) | No thread object carries any tab field. Attribution-by-id-location is correct for **suggestions** and ambiguity has no mechanism — a range cannot span tabs. **Comments were different and carried no address at all**: each tab has a disjoint `commentAnchors` map, so `anchorId` is the join key. Comment records now carry `tab_id`, null (never "the default tab") when unplaceable. | [`tabs.md`](docs/findings/tabs.md) |
| Are the thread ops SUGGEST-incompatible? (UNCERTAIN 5) | **Split, and the overlay was half wrong.** The eight *officially unsupported* request types really are refused — `400 Invalid requests[0].X: Request does not support application as suggestion.`, each accepted in EDIT mode on the next call. The eight *preview thread* ops (`insertComment`, `addCommentReply`, `acceptSuggestion`, `rejectSuggestion`, the deletes) are **NOT** refused: HTTP 200, `ALL_SAVED`, and they take effect. `mockdocs` was rejecting batches prod accepts, so no mock scenario could exercise a comment written alongside a suggested edit. Fixed. | [`suggest-semantics.md`](docs/findings/suggest-semantics.md) |
| Does SUGGEST resolve indexes against the pre-batch document? | **No — progressively, exactly like EDIT.** Over `"0123456789"`, `[insert@1 "AAAA", insert@5 "B"]` gives `"AAAAB0123456789"` in both modes; pre-batch resolution would have given `"AAAA0123B456789"`. The modes differ only in that a *suggested* deletion leaves its characters in the `SUGGESTIONS_INLINE` space while an EDIT deletion shifts. `suggest_doc_edit`'s replacement shape was correct — for the other reason, which is why the wrong justification survived. | [`suggest-semantics.md`](docs/findings/suggest-semantics.md) |
| Paragraph-style and table-structure suggestions | **The "out of scope by design" claim did not survive.** Table row/column edits were **already reported** — that half was simply wrong. Paragraph-style edits were **silently dropped**: `updateParagraphStyle` in SUGGEST mode creates a real OPEN suggestion that lands only on the paragraph (`suggestedParagraphStyleChanges`), and `analysis.py` walks content marks, so a document whose only card was that one reported `suggestion_count: 0`. All three accounting numbers agreed with each other and disagreed with the document. Alignment, spacing, indent, bullets and table row/cell styles were all invisible. Both read tools now emit `unreported_suggestion_count` + `unreported_suggestions` + a notice — counting what is not modelled rather than modelling it. | [`coverage.md`](docs/findings/coverage.md) |
| The not-enrolled classifier | The three markers appear verbatim in one canonical grammar (`Invalid JSON payload received. Unknown name "X" at 'P': Cannot find field.`) at every nesting depth, and the query-parameter variant drops `Cannot find field.` — so the redundancy is load-bearing. **A second grammar carried none of them**: `Invalid value at 'P' (TYPE), "V"` fell through to `available`, recording a request the API never parsed as proof the surface is reachable. Now classified `("unknown", "request_not_parsed")` — deliberately not `unavailable`, which would tell an enrolled caller with a typo to go and enrol. | [`errors-and-discovery.md`](docs/findings/errors-and-discovery.md) |
| Unanchored `insertComment` (UNCERTAIN 1) | **The API refuses it**: `400 Invalid requests[0].insertComment: Insert comment requests must specify a range to anchor to.` Empty and tab-only ranges are refused too. `create_anchored_doc_comment`'s mandatory range is Google's restriction, not a self-imposed one, so it stays. The Drive redirect now has evidence: a Drive-created unanchored comment reads back through the Docs preview as a full `CommentThread` with `anchorId` and `plainTextQuote` simply absent — that absence *is* "document-level". | [`errors-and-discovery.md`](docs/findings/errors-and-discovery.md) |
| Merge tolerance (mock spec §13.2) | **0 — the mock's guess was right**, and the suspicion that insert-then-insert differs from insert-then-delete is **not** borne out. Twelve documents, four orderings × gaps 0/1/2: gap 0 joins, gap 1 does not, uniformly. Not a coalescing window (130 s apart still joins) and symmetric. | [`merge.md`](docs/findings/merge.md) |
| Threads on merge (§13.3), and id renaming | **Both dissolve.** What this repo has been calling "merge" is **absorption at creation time**, not two suggestions becoming one: a new edit touching an existing same-author card is absorbed into it, the write returns no `createdSuggestionIds` at all — only `updatedSummarySuggestionIds` naming the pre-existing id — and no second id is ever minted. So no thread is ever orphaned (two threaded deletions plus a spanning deletion left two cards, each keeping its own reply), and "does the id get renamed" has no subject. | [`merge.md`](docs/findings/merge.md) |
| Which card absorbs, when a new edit touches two | **Deterministic — the touched card with the lexicographically greatest suggestion id.** First measured 2026-08-01 as "nondeterministic, 3 left / 2 right over five identical runs"; a 2026-08-02 re-measurement that decouples position from creation order (44 fresh documents, 56 two-card-touch events, four edit-type conditions) matched "greatest id wins" 56/56 and every position/age rule near chance — the ids are random, which is what made five same-order trials look like a coin. The "later card (higher index) survives" hypothesis is rejected (27/56). Undocumented behaviour: one e2e test asserts it so drift is caught, but agents and product code still must not rely on it — the invariant tests stay id-agnostic. | [`merge.md`](docs/findings/merge.md) |

Two API facts worth carrying forward, both found twice independently:

- **The thread array is not the pending set.** A resolved suggestion does not
  leave `suggestions[]` — it is restatused (`ACCEPTED`/`REJECTED`) and gains a
  `suggestionAction` reply. §4.4's rule holds only because `analysis.py`
  derives the pending set from the body's marks, never from `len(suggestions)`.
- **Both `PREVIEW_*` view modes always degrade to the GA read.**
  `documents.get` refuses `commentsViewMode` there: `400 "Comments may not be
  requested when previewing suggestions."`

### 7.2 Still open — and why

- **Does preview enrollment propagate per-project or per-account?**
  (UNCERTAIN 3.) **Still open** — and it is two questions wearing one label,
  with a separate experiment for each half.

  *Experiment A — vary the project, hold the account fixed.* Specified in
  `pending_for_human.md` steps 1-6: a **second GCP project that is not
  enrolled**, with its own OAuth client and an interactive consent grant for
  the *same* Google account. That is a human in a browser; no amount of probing
  from an enrolled project substitutes. Same account, unenrolled project:
  `available` ⇒ the project is not the gate; `unavailable` ⇒ it is, and the
  classifier's documented inference becomes a measurement.

  *Experiment B — vary the account, hold the project fixed.* Not previously
  written down, and much the cheaper of the two: **one OAuth client, one GCP
  project, two different Google accounts** — one in a Workspace org that is
  enrolled, one that is not. Authenticate both into the same credential store
  (§3.6), then run `check_docs_review_capabilities(probe=true, document_id=…)`
  under each and compare the verdicts. Prerequisite: the second account has to
  be a test user on the same consent screen (§2.2 step 3). Decision rule,
  fixed before the data:

  > Both `available` ⇒ the gate is per-project; the account is irrelevant.
  > One `available` and one `unavailable` ⇒ there is a per-account component.

  Any other pair settles nothing. In particular an `unknown` from either probe
  means it never reached the question — §3.3's table says which outcomes those
  are — and must be re-run, not interpreted.

  *What the public documentation establishes, and what it does not* (web
  research, 2026-08-25). Google's **Classroom** preview page states the gate as
  a property of the project: "The calling Google Cloud project must be enrolled
  in the Google Workspace Developer Preview Program and allow listed by Google"
  (<https://developers.google.com/workspace/classroom/reference/preview>). The
  **program page** ties access to Google Group membership of an individual: "If
  your email address cannot be added to the Google Group, you won't be able to
  access the dedicated client library, and you won't get access to some of the
  features" (<https://developers.google.com/workspace/preview>). Neither page
  addresses the two-accounts-one-client case, and the Docs-API-specific preview
  page was not retrieved. **The documentation supports both readings and
  settles neither.** That is why the repo models capability as per-account and
  unknown-until-observed (§3.6) — the one model that is correct whichever way
  this lands, and whose cost if it lands the other way is a redundant probe.

  *The confound that makes any probe ambiguous.* The preview request types are
  absent from the public discovery document and no label brings them back —
  `labels=DEVELOPER_PREVIEW`, `PREVIEW`, `TRUSTED_TESTER` and
  `LIMITED_AVAILABILITY` each return the byte-identical public document
  ([`errors-and-discovery.md`](docs/findings/errors-and-discovery.md)). So a
  failure can mean not-enrolled *or* a client-library / payload problem, and
  the error text does not separate them. The same document records the related
  case measured live: "`insertcomment` is an unknown name, not a
  case-insensitive match — so a non-enrolled caller and a typo are
  indistinguishable from the message alone." Run either experiment with a
  payload already observed to work under an enrolled account, or a negative
  result proves nothing.

  What the classifier work above *does* establish is that the marker strings
  match real proto-parse errors — it does not establish what a non-enrolled
  project returns for a recognised-but-ungated request type. The bridge between
  the two is inference, and it is labelled as inference in the code.
- **The discovery type names for the two `status` enums** (UNCERTAIN 2).
  Every labelled variant (`DEVELOPER_PREVIEW`, `PREVIEW`, `TRUSTED_TESTER`,
  `LIMITED_AVAILABILITY`) returns the byte-identical public document;
  `v1preview`/`v1beta`/`v1alpha` are 404s; enrolled credentials change nothing.
  The transcoder names preview proto types on a value error, but `status` is
  output-only on both threads, so no request can carry one and no error can
  name one. The **values** are confirmed live (`CommentThread.status`
  OPEN→RESOLVED→OPEN; `SuggestionThread.status` OPEN→ACCEPTED/REJECTED); only
  the type names remain unavailable, and they are unavailable by construction.

### 7.3 Unreachable by construction, not merely untested

- **`runColour` / cross-author colour precedence (§13.1).** Colour appears
  nowhere in the API transcription, nowhere in any captured payload, and
  nowhere in `gdocs_preview/`. Docs never serialises the colour a suggestion
  renders in; it is computed client-side from authorship. The mock's rule is an
  internal detail exercised only by invariant I3 and cannot change any answer
  any tool gives.
- **§5.4 backspace-burst deletion and §9 undo.** Editor-interaction semantics.
  `batchUpdate` has no burst, no keystroke timing and no undo request type, so
  no test could distinguish an implementation from its absence.

### 7.4 The mock's remaining divergence, stated plainly

`mockdocs` merges two existing suggestions; prod absorbs a new edit into an
existing one and never merges two that already exist. Implementing the
prod-faithful rule was tried and measured: **51 failures**, almost all of them
checked-in llmux scenario and stress ground truth that would need regenerating
— which would invalidate the recorded benchmark numbers.

It was therefore **not** changed. The divergence is deliberate, documented at
`mockdocs/model.py` and `mockdocs/adapter.py`, and the ordering for closing it
later is in [`merge.md`](docs/findings/merge.md). Anyone regenerating the llmux
corpora should close this first, or the ground truth will bake the mock's rule
in again.

The one place the divergence had leaked into a *measurement* has been closed:
`ix-merge-absorb` used to score agents on two live cards merging, which prod
cannot produce. It was re-founded on absorption at creation time — the agent's
own edit joins a same-author card that is already there — and grades only the
end state both rules reach, never which id survives. See
[`merge-absorb-premise.md`](docs/findings/merge-absorb-premise.md), which also
states the one cue the mock cannot stage (prod's write that returns no created
id at all).

---

## 8. Relationship to upstream

- **105 commits ahead** of `upstream/main` (`taylorwilsdon/google_workspace_mcp`)
  as of `433218a`; 287 files changed, almost entirely additive.
- **Keep the fork merge-friendly.** The whole point of §5.2's shape is that
  `docs_preview` is a self-contained package plus four registration entries.
  New work belongs in `gdocs_preview/`, `mockdocs/`, `llmux/`, `e2e/`,
  `docs/`, `tests/` — not in upstream modules. Before touching an upstream
  file, check whether the change can live in the fork's own package instead.
- **The obviously upstreamable piece** is the comment-lifecycle extension to
  `core/comments.py` (§3.5): `update` and `delete` actions on the shared
  factory, which benefit Docs, Sheets and Slides equally, plus the
  `destructiveHint=True` correction and +131 lines of tests in
  `tests/core/test_comments.py`. It has no dependency on the preview surface
  or on enrollment.
- The `docs_preview` service itself is **not** a good upstream PR as-is:
  registering enrollment-gated tools for every upstream user would break them.
  If upstreamed it should stay opt-in behind `--tools docs_preview`, which is
  already how it works.
- `gdocs/docs_tools.py` carries one small upstream-relevant correctness fix
  (the index-0 remap must not fire when `segment_id`/`tab_id` is set) and the
  matching skill-reference documentation for `tab_id`/`segment_id`.

---

## 9. Where to look first

| question | file |
|---|---|
| what does a tool do / what are its params | `gdocs_preview/curated_tools.py`, `gdocs_preview/write_tools.py` — the docstrings are the contract the MCP client sees |
| what does the preview API actually do | [`docs/preview-api-reference.md`](docs/preview-api-reference.md) |
| why is the surface shaped like this | `docs/plans/2026-07-14-native-integration.md`, `docs/plans/2026-07-30-large-review-sets.md` |
| how does suggesting mode work | `docs/plans/2026-07-30-suggestion-mock-spec.md` |
| how do I get credentials | [`e2e/README.md`](e2e/README.md), `pending_for_human.md` |
| is the preview surface reachable right now | `check_docs_review_capabilities(probe=true, document_id=…)` |
| which accounts exist, and which one am I acting as | `list_google_accounts` (no API call), then §3.6 for the routing rule |

**Never commit**: `credentials/`, `e2e/last_run.md`, `e2e/_artifacts/`,
`llmux/runner/reports/`. All four are gitignored and contain account
identity, document ids, or full transcripts. This is a public repository.
