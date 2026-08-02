<!-- mcp-name: io.github.taylorwilsdon/workspace-mcp -->

# This is a fork

A fork of **[taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp)** adding one service: **`docs_preview`** — 7 tools that let an agent work with Google Docs **comments** and **edit suggestions** the way a human reviewer does. Built for a client who publishes web pages out of Google Docs and runs heavy review over them.

### → **[HANDOVER.md](HANDOVER.md) is the documentation for this fork. Start there.**

It covers setup (including the Developer Preview enrollment that gates half the surface), every tool's contract and non-obvious parameters, the load-bearing Docs API facts the design rests on, the testing story, and an honest list of known gaps and open questions. Everything in this README after the fork sections is upstream's, unmodified.

| | |
|:---|:---|
| **[HANDOVER.md](HANDOVER.md)** | Setup, tools, API facts, testing, known gaps — the fork's manual |
| [`docs/preview-api-reference.md`](docs/preview-api-reference.md) | What the Developer Preview API actually does, transcribed and marked where still UNCERTAIN |
| [`e2e/README.md`](e2e/README.md) | Running the real-API end-to-end suite |
| [`docs/plans/`](docs/plans/) | Why the surface is shaped the way it is |

### What the fork adds

| | |
|:---|:---|
| **`gdocs_preview/`** | The `docs_preview` service. `list_document_suggestions`, `get_doc_review_view`, `check_docs_review_capabilities`, `suggest_doc_edit`, `manage_document_suggestion`, `reply_to_doc_thread`, `create_anchored_doc_comment`. Opt in with `--tools docs_preview`; it registers nothing unless asked for. |
| **`mockdocs/`** | An in-memory reimplementation of Docs suggesting mode — per-character insert/delete mark sets, same-author merge, accept/reject, the three projections — plus a mock-backed MCP server. Lets the whole surface be tested with no token and no network. |
| **`llmux/`** | A benchmark measuring **tool-surface ergonomics, not model IQ**: a headless agent drives the mock server against seeded scenarios, and reports lead with a mistake taxonomy rather than a pass rate. Ground truth is computed, never authored. |
| **`e2e/`** | Black-box tests against the **real** Google APIs, speaking MCP to the server as a subprocess. Skip cleanly without credentials. |

### What the fork changes in upstream files

Deliberately small — the design target is a self-contained package plus a handful of registration lines, so the fork stays merge-friendly. **112 commits, 293 files, ~57,000 insertions against 25 deletions**; only these upstream modules are touched at all:

- **`core/comments.py`** — adds `update` and `delete` actions to the shared comment factory, and corrects `destructiveHint` to `True`. Because the factory is instantiated three times this lands on Docs, Sheets **and** Slides at once. This is the piece that is obviously upstreamable: it has no dependency on `docs_preview` or on preview enrollment.
- **`gdocs/docs_tools.py`** — one correctness fix: the index-0 remap must not fire when `segment_id`/`tab_id` is set, because a header/footer/footnote is numbered from its own start, so 0 is a real position there.
- **`main.py`, `auth/scopes.py`, `auth/permissions.py`, `core/tool_tiers.yaml`** — four registration entries for the new service.
- **`pyproject.toml`, `.gitignore`** — the `hypothesis` test dependency, three pytest markers, and ignores for credentials and test artifacts.
- **`skills/managing-google-workspace/references/docs.md`** — documents `tab_id`/`segment_id` on the existing Docs tools, matching the fix above.
- **`gsheets/sheets_helpers.py`, `gsheets/sheets_tools.py`** — formatting only (`ruff format`), no behaviour change.

That is the complete list. Two of those entries do change behaviour an existing upstream user would see — the comment tools gain two actions and a corrected `destructiveHint`, and `modify_doc_text` stops remapping index 0 in a named segment — and both are bug fixes to shared code rather than hooks for this fork. Nothing else upstream is altered, and `docs_preview` itself registers only when `--tools docs_preview` asks for it, so an upstream user who does not opt in sees no new tools and no new scopes.

### Running it

```bash
uv run python main.py --transport stdio --single-user --tools docs docs_preview
```

Then call `check_docs_review_capabilities(probe=true, document_id=<a doc you can edit>)` — that is the only way to confirm preview enrollment, and it cannot mutate the document. Without enrollment the server still starts and every read still answers, in a **clearly-flagged degraded mode** rather than a silent one.

---

<div align="center">

# <span style="color:#cad8d9">Google Workspace MCP Server</span> <img src="https://github.com/user-attachments/assets/b89524e4-6e6e-49e6-ba77-00d6df0c6e5c" width="80" align="right" />

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/workspace-mcp.svg)](https://pypi.org/project/workspace-mcp/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/workspace-mcp?period=total&units=NONE&left_color=GREY&right_color=BLUE&left_text=pypi+downloads)](https://pepy.tech/projects/workspace-mcp)
[![MCP Toplist](https://mcptoplist.com/badge/glama%2Ftaylorwilsdon%2Fgoogle_workspace_mcp.svg)](https://mcptoplist.com/server/glama%2Ftaylorwilsdon%2Fgoogle_workspace_mcp)
[![Website](https://img.shields.io/badge/Website-workspacemcp.com-green.svg)](https://workspacemcp.com)

*Full natural language control over Google Calendar, Drive, Gmail, Docs, Sheets, Slides, Forms, Tasks, Contacts, and Chat through all MCP clients, AI assistants and developer tools.*
*Includes a full featured CLI & Code Mode for use with tools like Claude Code and Codex!*

**The most feature-complete Google Workspace MCP server**, it can do things that Google's own tooling and the built in integrations with Claude and ChatGPT can't come close to. With multi-user support, rich fine-grained editing tools and the most extensive coverage of any Google Workspace tool in existence, Workspace MCP is in a different class. 

By leveraging native OAuth 2.1, stateless deployment capability and external auth server & gateway passthrough auth support, it's also the only Workspace MCP you can host for your whole organization centrally & securely!

###### Support for all free Google accounts & Google Workspace plans (Starter, Standard, Plus, Enterprise, Non Profit) with expanded app options like Chat & Spaces. <br/><br /> Interested in a private, managed cloud instance? [That can be arranged.](https://workspacemcp.com/workspace-mcp-cloud)


</div>

<p align="center">
  <a href="https://workspacemcp.com/docs">
    <img src="https://img.shields.io/badge/Read%20the%20Docs-0969DA?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Read the Docs">
  </a><a href="https://workspacemcp.com/quick-start">
    <img src="https://img.shields.io/badge/Quick%20Start-2EA44F?style=for-the-badge" alt="Quick Start Guide">
  </a>
</p>

<div align="center">
<a href="https://www.pulsemcp.com/servers/taylorwilsdon-google-workspace">
<img width="375" src="https://github.com/user-attachments/assets/0794ef1a-dc1c-447d-9661-9c704d7acc9d" align="center"/>
</a>
</div>

---

**See it in action:**
<div align="center">
  <video width="400" src="https://github.com/user-attachments/assets/a342ebb4-1319-4060-a974-39d202329710"></video>
</div>

---

## What It Does

Workspace MCP connects AI assistants to all twelve major Google Workspace services - 120+ tools behind a single MCP server, with OAuth 2.1 multi-user auth, three progressive tool tiers, read-only mode, a full CLI, and stateless container deployment. It runs locally over stdio for legacy clients and remotely over streamable HTTP with full implementation of the latest MCP spec.

The README covers just enough to get you running, with extensive documentation on the website:

| Where to go | What you'll find |
|:---|:---|
| **[Quick&nbsp;Start](https://workspacemcp.com/quick-start)** | Google Cloud setup, credentials, and client connection with screenshots |
| **[Full&nbsp;Documentation](https://workspacemcp.com/docs)** | Every tool, parameter, and auth mode |
| **[Advanced&nbsp;Deployment](https://workspacemcp.com/docs/deployment)** | Reverse proxy & nginx config, origin validation, credential store backends (GCS/CMEK), and the complete environment variable reference |
| **[Client&nbsp;Setup&nbsp;Guides](https://workspacemcp.com/guides)** | Claude Desktop/web Connectors, ChatGPT Developer Mode, and more |
| **[FAQ&nbsp;&&nbsp;Troubleshooting](https://workspacemcp.com/welcome/faq)** | OAuth errors, redirect URIs, Google Chat setup, client quirks |

## <span style="color:#adbcbc">Security & Compliance</span>

<table>
<tr>
<td valign="top" width="50%">

**For Security Teams**

By default, this server sends no data anywhere except Google's APIs, on behalf of the authenticated user, using your own OAuth client credentials. There is no usage reporting, analytics, license server, or SaaS dependency. Optional OpenTelemetry tracing exports only to an OTLP endpoint you explicitly configure. The default data path is: your infrastructure → Google APIs.

- **Fully open source** — every line is auditable in this repo
- **Your OAuth client, your GCP project** — credentials never leave your environment
- **You control the scopes** — read-only, granular per-service permissions, or full access
- **You control the network** — deploy behind your reverse proxy, in your VPC, on your own terms
- **No third-party services** — no intermediary servers, no token relays, no hosted backends
- **Stateless mode** — zero disk writes for locked-down container environments
- **Sensitive path blocking** — local file reads default to the managed attachment directory, and `validate_file_path()` still blocks `.env*` files plus common home-directory credential stores such as `~/.ssh/` and `~/.aws/` even if `ALLOWED_FILE_DIRS` is broadened

Full dependency tree in `pyproject.toml`, pinned in `uv.lock`.

</td>
<td valign="top" width="50%">

**For Legal & Procurement**

This project is [MIT licensed](LICENSE) — not "open core," not "source available," not "free with a CLA." There is no dual licensing, no commercial tier gating features, and no contributor license agreement.

- **Use commercially without restriction** — build products, sell services, deploy internally
- **Fork, embed, redistribute** — MIT requires only attribution
- **No CLA** — contributions remain under MIT
- **No built-in telemetry to disclose** — optional tracing is off unless you configure it
- **No network effects** — the server never contacts any endpoint you didn't configure
- **Standard dependency licenses** — MIT, Apache 2.0, and BSD throughout the dependency chain; no copyleft, no AGPL
</td>
</tr>
</table>

## Services

<table width="100%" align="center">
<tr>
<td align="center" width="25%">
<h3>📧</h3><a href="https://workspacemcp.com/gmail"><b>Gmail</b></a><br>
<sub>15 tools - search, send, draft,<br>labels, filters, attachments</sub>
</td>
<td align="center" width="25%">
<h3>📁</h3><a href="https://workspacemcp.com/google-drive"><b>Drive</b></a><br>
<sub>16 tools - search, create, share,<br>import Office files</sub>
</td>
<td align="center" width="25%">
<h3>📅</h3><a href="https://workspacemcp.com/google-calendar"><b>Calendar</b></a><br>
<sub>7 tools - events, free/busy,<br>Out of Office, Focus Time</sub>
</td>
<td align="center" width="25%">
<h3>📝</h3><a href="https://workspacemcp.com/google-docs"><b>Docs</b></a><br>
<sub>19 tools - edit, style, tables,<br>tabs, comments, export</sub>
</td>
</tr>
<tr>
<td align="center" width="25%">
<h3>📊</h3><a href="https://workspacemcp.com/google-sheets"><b>Sheets</b></a><br>
<sub>14 tools - ranges, tables,<br>formatting, conditional rules</sub>
</td>
<td align="center" width="25%">
<h3>🖼️</h3><a href="https://workspacemcp.com/google-slides"><b>Slides</b></a><br>
<sub>7 tools - create, batch update,<br>thumbnails, comments</sub>
</td>
<td align="center" width="25%">
<h3>📋</h3><a href="https://workspacemcp.com/google-forms"><b>Forms</b></a><br>
<sub>6 tools - build forms, publish,<br>read responses</sub>
</td>
<td align="center" width="25%">
<h3>✅</h3><a href="https://workspacemcp.com/google-tasks"><b>Tasks</b></a><br>
<sub>6 tools - tasks & lists<br>with hierarchy</sub>
</td>
</tr>
<tr>
<td align="center" width="25%">
<h3>👤</h3><a href="https://workspacemcp.com/google-contacts"><b>Contacts</b></a><br>
<sub>8 tools - people, groups,<br>batch operations</sub>
</td>
<td align="center" width="25%">
<h3>💬</h3><a href="https://workspacemcp.com/google-chat"><b>Chat</b></a><br>
<sub>6 tools - spaces, messages,<br>search, reactions</sub>
</td>
<td align="center" width="25%">
<h3>🔍</h3><a href="https://workspacemcp.com/google-search"><b>Custom Search</b></a><br>
<sub>2 tools - programmable<br>web search</sub>
</td>
<td align="center" width="25%">
<h3>⚡</h3><a href="https://workspacemcp.com/google-apps-script"><b>Apps Script</b></a><br>
<sub>15 tools - write, deploy,<br>run & debug scripts</sub>
</td>
</tr>
</table>

Each page lists every tool with its tier, parameters, required scopes, and example prompts. The [complete reference](https://workspacemcp.com/docs) covers all twelve in one place.

> 💬 **Google Chat** needs a one-time Chat app configuration and a Workspace account - see the [Chat setup FAQ](https://workspacemcp.com/welcome/faq).

## Quick Start

> Set credentials → pick a launch command → connect your client. Full walkthrough with screenshots: **[workspacemcp.com/quick-start](https://workspacemcp.com/quick-start)**

You'll need an OAuth client from [Google Cloud Console](https://console.cloud.google.com/) with the APIs enabled for the services you plan to use - the [quick start guide](https://workspacemcp.com/quick-start) walks through it in about five minutes.

<table>
<tr>
<td valign="top" width="50%">

**Confidential Client**

```bash
# 1. Credentials
export GOOGLE_OAUTH_CLIENT_ID="..."
export GOOGLE_OAUTH_CLIENT_SECRET="..."

# 2. Launch - pick a tier
uvx workspace-mcp --tool-tier core       # essential tools
uvx workspace-mcp --tool-tier extended   # core + management ops
uvx workspace-mcp --tool-tier complete   # everything

# Or cherry-pick services
uvx workspace-mcp --tools gmail drive calendar
```

</td>
<td valign="top" width="50%">

**Secretless / Public OAuth 2.1 (PKCE)**

```bash
# 1. Credentials
export MCP_ENABLE_OAUTH21=true
export GOOGLE_OAUTH_CLIENT_ID="..."
export WORKSPACE_MCP_PORT=8000
export GOOGLE_OAUTH_REDIRECT_URI="http://localhost:${WORKSPACE_MCP_PORT}/oauth2callback"
export OAUTHLIB_INSECURE_TRANSPORT=1
export FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY="$(openssl rand -hex 32)"

# 2. Launch - OAuth 2.1 requires HTTP transport
uvx workspace-mcp --transport streamable-http --tool-tier core
```

</td>
</tr>
</table>

**Tool tiers** keep context windows lean: `core` is the essential set, `extended` adds management operations, `complete` loads everything. Combine with `--tools <service> ...`, `--read-only`, or per-service `--permissions` - details in the [server modes docs](https://workspacemcp.com/docs#server-modes).

## Connect Your Client

**Claude Desktop, web & mobile** - run the server in HTTP mode and add it as a **Connector** (Settings → Connectors → Add custom connector). This is the recommended path; the [Connector guide](https://workspacemcp.com/guides/claude-connectors) has step-by-step screenshots. Legacy stdio configuration remains available for clients without Connector support - see the [FAQ](https://workspacemcp.com/welcome/faq).

**Claude Code**

```bash
# Start the server in HTTP mode, then:
claude mcp add --transport http workspace-mcp http://localhost:8000/mcp

# Optional: install the bundled skill for better Workspace tool routing
ln -s "$(pwd)/skills/managing-google-workspace" ~/.claude/skills/managing-google-workspace
```

**ChatGPT** - connect via Developer Mode with the [ChatGPT guide](https://workspacemcp.com/guides/chatgpt-developer-mode).

**VS Code, LM Studio, Open WebUI, and everything else** - any MCP client works over streamable HTTP (recommended) or stdio. Client-specific walkthroughs live in the [guides](https://workspacemcp.com/guides) and [FAQ](https://workspacemcp.com/welcome/faq).

## CLI

`workspace-cli` lists and calls tools against a running server with encrypted, disk-backed OAuth token caching - authenticate once, script forever:

```bash
uv run workspace-cli list
uv run workspace-cli call search_gmail_messages query="is:unread" max_results=5
```

Install globally with `uv tool install .` from this repo. ⚠️ Don't use `uvx workspace-cli` - an abandoned PyPI package squats that name.

## Deployment & Advanced Configuration

Everything you need to run this in production lives in two places. The [documentation](https://workspacemcp.com/docs) covers auth modes and server configuration:

- **[OAuth 2.1 multi-user auth](https://workspacemcp.com/docs#authentication)** - bearer tokens, required for remote or shared HTTP endpoints
- **[Stateless container mode](https://workspacemcp.com/docs#authentication)** - zero disk writes for locked-down deployments
- **[OAuth proxy storage backends](https://workspacemcp.com/docs#authentication)** - memory, disk, or Valkey/Redis for distributed setups
- **[External OAuth provider mode](https://workspacemcp.com/docs#authentication)** - bring your own auth server, validate bearer tokens only
- **[Service accounts with domain-wide delegation](https://workspacemcp.com/docs#authentication)** - per-request user impersonation with an optional domain allowlist
- **[OpenTelemetry tracing](https://workspacemcp.com/docs#server-modes)** - optional, off unless you configure an OTLP endpoint
- **Docker** - `docker build -t workspace-mcp . && docker run -p 8000:8000 workspace-mcp`

The **[Advanced Deployment guide](https://workspacemcp.com/docs/deployment)** covers self-hosting specifics: reverse proxy setup with `WORKSPACE_EXTERNAL_URL` (including the nginx `Origin: null` consent workaround and `Referrer-Policy` pitfall), origin validation and VS Code webview allowlisting, credential store backends (local directory or GCS with CMEK enforcement), and the **[complete environment variable reference](https://workspacemcp.com/docs/deployment#environment-variables)**.

## Security Best Practices

By default this server sends no data anywhere except Google's APIs, using your own OAuth client credentials - no usage reporting, analytics, license server, or SaaS dependency. MIT licensed with no CLA, no dual licensing, and no copyleft in the dependency chain. The full security posture - scope minimization, sensitive-path blocking, stateless mode - is documented at [workspacemcp.com](https://workspacemcp.com/privacy).

A few things worth internalizing before you connect an LLM to your email:

- **Prompt injection is real.** Emails, docs, and events can contain hidden instructions. Only connect trusted data to an LLM, and be deliberate about which write tools you enable.
- **Never commit** `.env`, `client_secret.json`, or `.credentials/` to source control.
- **Local file reads are sandboxed** to the managed attachment directory. Broaden with `ALLOWED_FILE_DIRS` only if you trust the client and its data sources; `.env*`, `~/.ssh/`, `~/.aws/`, and similar paths are always blocked.
- **Production** deployments should use HTTPS and OAuth 2.1.

## Development

```bash
uv sync --group dev    # install deps
uv run ruff check .    # lint
uv run pytest          # test
```

Single-file service modules live in `g<service>/`, tools are registered with `@server.tool` decorators, and tiers are defined in `core/tool_tiers.yaml`. PRs welcome.

## License

MIT - see [`LICENSE`](LICENSE). The license is 21 lines and says what it means.

---

Validations:
[![MCP Badge](https://lobehub.com/badge/mcp/taylorwilsdon-google_workspace_mcp)](https://lobehub.com/mcp/taylorwilsdon-google_workspace_mcp)

