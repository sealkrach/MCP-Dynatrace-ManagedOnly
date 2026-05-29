# Dynatrace Bridge MCP — Managed edition

A **self-hosted MCP server** that talks directly to the **Dynatrace Managed classic API
(`/api/v2/`)**, making arbitrary authenticated API calls from any MCP client (VS Code +
Copilot Chat, Claude Desktop, etc.).

Every call runs with **your** token, scoped to **your** permissions.

## Tools

| Tool | Purpose |
|------|---------|
| `dynatrace_api_request` | Authenticated call to **any** `/api/v2/` endpoint (GET always; writes gated). |
| `fetch_logs` | Convenience wrapper for `/api/v2/logs/search`. |
| `list_problems` | Convenience wrapper for `/api/v2/problems`. |
| `query_metrics` | Time-series metric query via `/api/v2/metrics/query`. |
| `list_metrics` | Discover available metrics via `/api/v2/metrics`. |
| `list_entities` | Monitored entities (hosts, services, apps…) with selector/type/tag filters. |
| `list_slos` | Service Level Objectives with status and error budget. |
| `get_audit_logs` | Environment audit log — who did what and when. |
| `whoami` | Connectivity / auth sanity check (no secrets printed). |

## 1. Get a token

**Recommended: an API Token** (long-lived, used directly as a Bearer).
In Dynatrace Managed: *Settings → Integration → Dynatrace API → Generate token*.

Grant the scopes you actually need (least privilege). For read-only observability:

```
logs.read
problems.read
entities.read
metrics.read
events.read
```

Add more scopes per use case. For the generic API tool you may need additional scopes
depending on the endpoints you call.

> OAuth client-credentials is also supported — but requires `DT_SSO_TOKEN_URL` pointing
> to your Managed SSO endpoint and `DT_OAUTH_SCOPES` set to your OAuth scope list.
> OAuth-issued tokens are short-lived; the server refreshes them automatically.

## 2. Install

Requires **Python 3.9+**.

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure (environment variables)

| Variable | Required | Notes |
|----------|----------|-------|
| `DT_ENVIRONMENT_URL` | yes | Full Managed URL, e.g. `https://dynatrace.mycompany.com/e/ENV_ID` |
| `DT_API_TOKEN` | yes* | API token. *Or use OAuth vars. Also accepted as `DT_PLATFORM_TOKEN`. |
| `DT_OAUTH_CLIENT_ID` / `DT_OAUTH_CLIENT_SECRET` | * | Alternative to API token |
| `DT_OAUTH_SCOPES` | * | Space-separated OAuth scopes (required with OAuth) |
| `DT_SSO_TOKEN_URL` | * | Your Managed SSO endpoint (required with OAuth), e.g. `https://dynatrace.mycompany.com/sso/oauth2/token` |
| `DT_ALLOW_WRITE` | no | `true` to permit non-GET API calls. **Default `false`.** |
| `DT_HTTP_TIMEOUT` | no | httpx client-level timeout in seconds (default 60) |

## 4. Wire into a client

**VS Code** — `.vscode/mcp.json`:

```json
{
  "servers": {
    "dynatrace-bridge": {
      "command": "/abs/path/venv/bin/python",
      "args": ["/abs/path/dynatrace_bridge_mcp.py"],
      "env": {
        "DT_ENVIRONMENT_URL": "https://dynatrace.mycompany.com/e/ENV_ID",
        "DT_API_TOKEN": "dt0s08.XXXX"
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
        "DT_ENVIRONMENT_URL": "https://dynatrace.mycompany.com/e/ENV_ID",
        "DT_API_TOKEN": "dt0s08.XXXX"
      }
    }
  }
}
```

Then ask, e.g.: *"Fetch logs with status ERROR in namespace payments"* or
*"Call GET /api/v2/entities?entitySelector=type(SERVICE)"*.

Pair it with the `dynatrace_mcp_system_prompt.md` system prompt for disciplined,
audit-friendly behaviour.

## Métriques — API classique vs DQL

L'API `/api/v2/metrics/query` permet d'interroger des séries temporelles, tout comme DQL
`fetch metrics` sur SaaS. La différence réelle :

| Capacité | `/api/v2/metrics/query` | DQL (SaaS/Grail) |
|----------|------------------------|-----------------|
| Séries temporelles | ✅ | ✅ |
| Filtrer par entité / tag / zone | ✅ | ✅ |
| Agrégations (avg, min, max, pct) | ✅ via `metricSelector` | ✅ plus riche |
| Résolution personnalisée | ✅ | ✅ |
| **Corréler métriques + logs + traces** | ❌ appels séparés | ✅ JOIN inline |
| **Calculs cross-métriques** (ratios, taux) | ❌ | ✅ |
| **Fonctions inline** (`toRate()`, `delta()`) | ❌ | ✅ |

**En pratique :** l'API classique couvre 80 % des besoins SRE (dashboards, alertes,
time series par service/host). Ce qui manque : la corrélation multi-signaux en une
seule requête — sur Managed, ça se fait en plusieurs appels.

```bash
# Découvrir les métriques disponibles
list_metrics(text="cpu")
list_metrics(selector="builtin:service.*")

# Requêter une série temporelle
query_metrics("builtin:host.cpu.usage:avg", resolution="5m", from_time="now-2h")
query_metrics(
    "builtin:service.response.time:percentile(95)",
    from_time="now-1h",
    entity_selector="type(SERVICE),tag(prod)"
)
```

## API quick notes

- **Timeframe:** pass ISO-8601 `from_time`/`to_time` to `fetch_logs` and `list_problems`,
  or use `query_params` in `dynatrace_api_request`.
- **Pagination:** `/api/v2/` responses include `nextPageKey`; pass it as a `nextPageKey`
  query parameter to walk pages.
- **Log query syntax:** space-separated key:value pairs, e.g.
  `status:ERROR AND k8s.namespace:payments AND host:prod-*`.

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
