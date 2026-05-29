# Dynatrace Bridge MCP

A **self-hosted MCP server** that talks **directly to the Dynatrace Platform / Grail
API**, working around managed-MCP limitations: it can **execute DQL** and make
**arbitrary authenticated API calls**, which the managed/hosted MCP may not expose in
your tenant.

It runs locally over **stdio** and plugs into any MCP client (VS Code + Copilot Chat,
Claude Desktop, etc.). Every call runs with **your** token, scoped to **your**
permissions.

## Tools

| Tool | SaaS | Managed | Purpose |
|------|------|---------|---------|
| `execute_dql` | ✅ | ❌ | Run any DQL query against Grail (async execute + poll). |
| `dynatrace_api_request` | ✅ | ✅ | Authenticated call to **any** Dynatrace API path (GET always; writes gated). |
| `fetch_logs` | ✅ | ❌ | Convenience DQL wrapper for recent logs. |
| `fetch_logs_classic` | ✅ | ✅ | Logs via `/api/v2/logs/search`. Use on Managed or as SaaS alternative. |
| `list_davis_problems` | ✅ | ❌ | Convenience DQL wrapper for Davis problems (events table). |
| `list_problems_classic` | ✅ | ✅ | Problems via `/api/v2/problems`. Use on Managed or as SaaS alternative. |
| `whoami` | ✅ | ✅ | Connectivity / auth / scope sanity check (no secrets printed). |

## 1. Get a token

**Recommended: a Platform Token** (long-lived, used directly as a Bearer).
In Dynatrace: *Account/Settings → Platform tokens → create*.

Grant the scopes you actually need (least privilege). For read-only observability:

```
storage:buckets:read
storage:logs:read
storage:metrics:read
storage:events:read
storage:bizevents:read
storage:entities:read
storage:system:read
```

Add more `storage:*:read` (spans, security events…) per use case. For the generic
API tool you may also need the relevant classic/platform API scopes.

> OAuth client-credentials is also supported (`DT_OAUTH_CLIENT_ID` /
> `DT_OAUTH_CLIENT_SECRET` / `DT_OAUTH_SCOPES`) — but OAuth-issued tokens are
> short-lived; the server refreshes them automatically.

## 2. Install

Requires **Python 3.9+**.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure (environment variables)

| Variable | Required | Notes |
|----------|----------|-------|
| `DT_ENVIRONMENT_URL` | yes | `https://abc12345.apps.dynatrace.com` or just `abc12345` |
| `DT_PLATFORM_TOKEN` | yes* | Platform token (`dt0s16.…`). *Or use OAuth vars. |
| `DT_OAUTH_CLIENT_ID` / `DT_OAUTH_CLIENT_SECRET` | * | Alternative to platform token |
| `DT_OAUTH_SCOPES` | no | Space-separated; defaults to read-only storage scopes |
| `DT_INSTANCE_TYPE` | no | `saas` (default) or `managed`. Set to `managed` for on-premise instances. |
| `DT_ALLOW_WRITE` | no | `true` to permit non-GET API calls. **Default `false`.** |
| `DT_MAX_POLL_SECONDS` | no | Total DQL wait budget (default 120) |
| `DT_REQUEST_TIMEOUT_MS` | no | Per execute/poll call (default 30000) |
| `DT_HTTP_TIMEOUT` | no | httpx client-level timeout in seconds (default 60) |
| `DT_SSO_TOKEN_URL` | no | OAuth token endpoint — override for on-prem SSO (default `https://sso.dynatrace.com/sso/oauth2/token`) |

## 4. Wire into a client

**VS Code** — `.vscode/mcp.json`:

```json
{
  "servers": {
    "dynatrace-bridge": {
      "command": "/abs/path/venv/bin/python",
      "args": ["/abs/path/dynatrace_bridge_mcp.py"],
      "env": {
        "DT_ENVIRONMENT_URL": "abc12345",
        "DT_PLATFORM_TOKEN": "dt0s16.XXXX"
      }
    }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dynatrace-bridge": {
      "command": "/abs/path/venv/bin/python",
      "args": ["/abs/path/dynatrace_bridge_mcp.py"],
      "env": {
        "DT_ENVIRONMENT_URL": "abc12345",
        "DT_PLATFORM_TOKEN": "dt0s16.XXXX"
      }
    }
  }
}
```

Then ask, e.g.: *"Run DQL: fetch logs | filter loglevel=="ERROR" | limit 20"* or
*"Call GET /platform/classic/environment-api/v2/problems"*.

Pair it with the `dynatrace_mcp_system_prompt.md` system prompt for disciplined,
audit-friendly behaviour.

## Dynatrace Managed (on-premise)

Set `DT_INSTANCE_TYPE=managed` to enable Managed mode. DQL tools (`execute_dql`,
`fetch_logs`, `list_davis_problems`) will return a clear error pointing to the alternatives
— Grail endpoints don't exist on Managed.

**Minimum config for Managed:**

```json
{
  "env": {
    "DT_ENVIRONMENT_URL": "https://dynatrace.mycompany.com/e/ENV_ID",
    "DT_PLATFORM_TOKEN": "dt0s08.XXXX",
    "DT_INSTANCE_TYPE": "managed"
  }
}
```

If using OAuth instead of a platform token, also override the SSO endpoint:

```json
{
  "env": {
    "DT_ENVIRONMENT_URL": "https://dynatrace.mycompany.com/e/ENV_ID",
    "DT_OAUTH_CLIENT_ID": "...",
    "DT_OAUTH_CLIENT_SECRET": "...",
    "DT_SSO_TOKEN_URL": "https://dynatrace.mycompany.com/sso/oauth2/token",
    "DT_INSTANCE_TYPE": "managed"
  }
}
```

On Managed, use `fetch_logs_classic` (query syntax: `status:ERROR AND host:prod-*`) and
`list_problems_classic` instead of their DQL counterparts.

## DQL quick notes

- Timeframe: pass ISO-8601 `from_time`/`to_time`, **or** embed it in the DQL. If
  omitted, the API default applies (≈72h for API context).
- Start narrow (`| limit`, tight filters, short window). The result includes Grail
  `scanned_bytes` so you can watch query cost.
- Async by design: the server submits the query and polls until SUCCEEDED, FAILED, or
  the `DT_MAX_POLL_SECONDS` budget — then it cancels to avoid orphan queries.

## Regulated / banking notes

- **Read-only by default.** `dynatrace_api_request` refuses every non-GET method
  unless you opt in with `DT_ALLOW_WRITE=true`. Keep it off unless a specific,
  reviewed workflow needs writes.
- **Token = blast radius.** The server can do whatever the token's scopes allow. Mint
  a dedicated least-privilege token; never reuse an admin token.
- **No secrets in logs.** The server never prints the token; `whoami` reports the auth
  *mode* only.
- **Per-user accountability.** If multiple people use it, give each their own token so
  Dynatrace audit attributes calls correctly.
- Treat this as **your** code running with **your** credentials — review before
  deploying to a shared/managed runner.

## Schema caveat

`list_davis_problems` uses the documented `events` pattern, but the exact Davis field
names vary by tenant/version. If a field comes back empty, refine it directly with
`execute_dql`.
