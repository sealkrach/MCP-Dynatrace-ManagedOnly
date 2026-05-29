#!/usr/bin/env python3
"""
Dynatrace Bridge MCP server — Managed (on-premise) edition.

A self-hosted MCP server that talks directly to the Dynatrace Managed classic API
(/api/v2/). It exposes:
  - dynatrace_api_request  : generic authenticated call to ANY /api/v2/ endpoint
  - fetch_logs             : convenience wrapper for /api/v2/logs/search
  - list_problems          : convenience wrapper for /api/v2/problems
  - query_metrics          : time-series metric query via /api/v2/metrics/query
  - list_metrics           : discover available metrics via /api/v2/metrics
  - list_entities          : monitored entities with entitySelector/type/tag filters
  - list_slos              : Service Level Objectives via /api/v2/slo
  - get_audit_logs         : environment audit log via /api/v2/auditlogs
  - whoami                 : connectivity / auth / scope check

Auth: API Token (preferred) OR OAuth client-credentials.
Runs over stdio (standard for local MCP clients: VS Code, Claude Desktop, etc.).

Read the companion README for setup. Designed for regulated/banking use:
write-style HTTP methods are gated behind DT_ALLOW_WRITE.
"""

from __future__ import annotations

import os
import time
import json
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuration (all via environment variables)
# --------------------------------------------------------------------------- #

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


def _normalize_env_url(raw: Optional[str]) -> str:
    """Accept a full Managed URL (with or without trailing slash)."""
    if not raw:
        raise RuntimeError(
            "DT_ENVIRONMENT_URL is required. "
            "Provide the full Managed URL, e.g. https://dynatrace.mycompany.com/e/ENV_ID"
        )
    return raw.strip().rstrip("/")


DT_ENV_URL = _normalize_env_url(_env("DT_ENVIRONMENT_URL"))
DT_API_TOKEN = _env("DT_API_TOKEN") or _env("DT_PLATFORM_TOKEN")  # accept both names

DT_OAUTH_CLIENT_ID = _env("DT_OAUTH_CLIENT_ID")
DT_OAUTH_CLIENT_SECRET = _env("DT_OAUTH_CLIENT_SECRET")
DT_OAUTH_SCOPES = _env("DT_OAUTH_SCOPES", "")  # set per your Managed OAuth config
DT_SSO_TOKEN_URL = _env("DT_SSO_TOKEN_URL", "")  # required for OAuth on Managed

# Safety: anything that is not GET is refused unless this is explicitly "true".
DT_ALLOW_WRITE = (_env("DT_ALLOW_WRITE", "false") or "false").lower() == "true"

# httpx client timeout in seconds.
DT_HTTP_TIMEOUT = float(_env("DT_HTTP_TIMEOUT", "60"))

mcp = FastMCP("dynatrace-bridge")

# --------------------------------------------------------------------------- #
# Auth: resolve a bearer token (API token used directly, or OAuth exchange)
# --------------------------------------------------------------------------- #

_token_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}


async def _get_bearer() -> str:
    """Return a bearer token, preferring an API Token; else OAuth client creds."""
    if DT_API_TOKEN:
        return DT_API_TOKEN

    if not (DT_OAUTH_CLIENT_ID and DT_OAUTH_CLIENT_SECRET):
        raise RuntimeError(
            "No credentials configured. Set DT_API_TOKEN, or "
            "DT_OAUTH_CLIENT_ID + DT_OAUTH_CLIENT_SECRET + DT_SSO_TOKEN_URL."
        )
    if not DT_SSO_TOKEN_URL:
        raise RuntimeError(
            "DT_SSO_TOKEN_URL is required for OAuth on Managed "
            "(e.g. https://dynatrace.mycompany.com/sso/oauth2/token)."
        )

    now = time.time()
    if _token_cache["value"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["value"]

    async with httpx.AsyncClient(timeout=DT_HTTP_TIMEOUT) as client:
        resp = await client.post(
            DT_SSO_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": DT_OAUTH_CLIENT_ID,
                "client_secret": DT_OAUTH_CLIENT_SECRET,
                "scope": DT_OAUTH_SCOPES,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"OAuth token request failed ({resp.status_code}): {resp.text[:500]}")
    payload = resp.json()
    token = payload["access_token"]
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + float(payload.get("expires_in", 300))
    return token


async def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Api-Token {await _get_bearer()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _http_error(where: str, resp: httpx.Response) -> dict[str, Any]:
    hint = ""
    if resp.status_code in (401, 403):
        hint = (" — auth/permission issue. Check the token validity and that it carries "
                "the required API token scopes for this endpoint.")
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:1000]
    return {"ok": False, "state": "HTTP_ERROR", "where": where,
            "status": resp.status_code, "error": body, "hint": hint}


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
async def dynatrace_api_request(
    method: str,
    path: str,
    query_params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> str:
    """Make an authenticated request to ANY Dynatrace Managed /api/v2/ endpoint.

    Generic escape hatch for endpoints not wrapped by a dedicated tool
    (entities, settings, metrics, events, automations, etc.).

    Safety: non-GET methods are refused unless the server is started with
    DT_ALLOW_WRITE=true. Even then, prefer dedicated review for state changes.

    Args:
        method: HTTP method ('GET', 'POST', 'PUT', 'PATCH', 'DELETE').
        path: Path beginning with '/', e.g. '/api/v2/entities'.
        query_params: Optional querystring parameters as a dict.
        body: Optional JSON body for write methods.

    Returns:
        JSON string with ok, status code, and the parsed/raw response.
    """
    method = method.upper().strip()
    if method != "GET" and not DT_ALLOW_WRITE:
        return json.dumps({
            "ok": False,
            "error": f"Write method {method} is disabled. Restart the server with "
                     f"DT_ALLOW_WRITE=true to enable state-changing calls.",
        })
    if not path.startswith("/"):
        path = "/" + path

    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=DT_HTTP_TIMEOUT) as client:
        resp = await client.request(
            method, DT_ENV_URL + path, headers=headers,
            params=query_params or None, json=body if body is not None else None,
        )
    try:
        parsed = resp.json()
    except Exception:
        parsed = resp.text[:5000]
    return json.dumps({"ok": resp.status_code < 400, "status": resp.status_code,
                       "response": parsed}, default=str)


@mcp.tool()
async def fetch_logs(
    query: Optional[str] = None,
    limit: int = 50,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
) -> str:
    """Fetch logs via /api/v2/logs/search.

    Args:
        query: Log search query string (e.g. 'status:ERROR AND k8s.namespace:payments').
            Omit to return all recent logs.
        limit: Max log lines (default 50, max 1000).
        from_time / to_time: Optional ISO-8601 bounds (e.g. '2026-05-29T10:00:00Z').
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 1000)}
    if query:
        params["query"] = query
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    return await dynatrace_api_request("GET", "/api/v2/logs/search", query_params=params)


@mcp.tool()
async def list_problems(
    open_only: bool = True,
    limit: int = 20,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
) -> str:
    """List Davis problems via /api/v2/problems.

    Args:
        open_only: If True, filter to OPEN problems only (default True).
        limit: Max problems to return (default 20, max 50 per page).
        from_time / to_time: Optional ISO-8601 bounds.
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 50)}
    if open_only:
        params["problemSelector"] = "status(OPEN)"
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    return await dynatrace_api_request("GET", "/api/v2/problems", query_params=params)


@mcp.tool()
async def query_metrics(
    metric_selector: str,
    resolution: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    entity_selector: Optional[str] = None,
    mz_selector: Optional[str] = None,
) -> str:
    """Query metric time series via /api/v2/metrics/query.

    Args:
        metric_selector: Metric key with optional aggregation,
            e.g. 'builtin:host.cpu.usage:avg' or
            'builtin:service.response.time:percentile(95)'.
            Use list_metrics() to discover available keys.
        resolution: Time bucket size, e.g. '1m', '5m', '1h', '1d'.
            Omit to use the API default (depends on the timeframe).
        from_time / to_time: ISO-8601 bounds or relative (e.g. 'now-2h').
        entity_selector: Filter by entity, e.g. 'type(SERVICE),tag(prod)'.
        mz_selector: Filter by management zone, e.g. 'name("Production")'.

    Returns:
        JSON with resolution, metric series and data points per entity/dimension.
    """
    params: dict[str, Any] = {"metricSelector": metric_selector}
    if resolution:
        params["resolution"] = resolution
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    if entity_selector:
        params["entitySelector"] = entity_selector
    if mz_selector:
        params["mzSelector"] = mz_selector
    return await dynatrace_api_request("GET", "/api/v2/metrics/query", query_params=params)


@mcp.tool()
async def list_metrics(
    text: Optional[str] = None,
    selector: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Discover available metrics via /api/v2/metrics.

    Use this to find the right metric key before calling query_metrics().

    Args:
        text: Filter by substring in metric name or description (e.g. 'cpu', 'response.time').
        selector: metricSelector pattern (e.g. 'builtin:host.*').
        limit: Max metrics to return (default 50).
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 500)}
    if text:
        params["text"] = text
    if selector:
        params["metricSelector"] = selector
    return await dynatrace_api_request("GET", "/api/v2/metrics", query_params=params)


@mcp.tool()
async def list_entities(
    entity_selector: Optional[str] = None,
    entity_type: Optional[str] = None,
    tag: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List monitored entities via /api/v2/entities.

    Use this to find hosts, services, applications, process groups, etc.
    Use list_entities first to get entity IDs, then pass them to query_metrics
    via entity_selector.

    Args:
        entity_selector: Full entitySelector expression, e.g.
            'type(SERVICE),tag(prod),healthState(UNHEALTHY)'.
            If provided, entity_type and tag are ignored.
        entity_type: Entity type shortcut, e.g. 'HOST', 'SERVICE',
            'APPLICATION', 'PROCESS_GROUP', 'DATABASE'. Ignored if
            entity_selector is set.
        tag: Filter by tag value, e.g. 'prod' or 'env:production'. Ignored if
            entity_selector is set.
        from_time / to_time: Optional ISO-8601 bounds for entity activity window.
        limit: Max entities to return (default 50).
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 500)}
    if entity_selector:
        params["entitySelector"] = entity_selector
    else:
        parts = []
        if entity_type:
            parts.append(f"type({entity_type})")
        if tag:
            parts.append(f"tag({tag})")
        if parts:
            params["entitySelector"] = ",".join(parts)
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    return await dynatrace_api_request("GET", "/api/v2/entities", query_params=params)


@mcp.tool()
async def list_slos(
    name_filter: Optional[str] = None,
    enabled_only: bool = False,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List Service Level Objectives via /api/v2/slo.

    Returns SLO definitions with their current status, error budget, and
    burn rate — useful for SRE reporting and incident context.

    Args:
        name_filter: Case-insensitive substring filter on SLO name.
        enabled_only: If True, return only enabled SLOs.
        from_time / to_time: Evaluation timeframe (ISO-8601 or relative like 'now-7d').
        limit: Max SLOs to return (default 50).
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 10000)}
    if name_filter:
        params["sloSelector"] = f"name(\"{name_filter}\")"
    if enabled_only:
        params["enabledSlos"] = "true"
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    return await dynatrace_api_request("GET", "/api/v2/slo", query_params=params)


@mcp.tool()
async def get_audit_logs(
    filter_expr: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Retrieve environment audit log entries via /api/v2/auditlogs.

    Useful for compliance, change tracking, and incident investigation in
    regulated environments. Records who did what and when.

    Args:
        filter_expr: Filter expression, e.g. 'user("jane.doe@company.com")'
            or 'eventType("CREATE")' or 'category("CONFIG")'.
        from_time / to_time: ISO-8601 bounds (default: last 3 days per API).
        limit: Max log entries to return (default 50).
    """
    params: dict[str, Any] = {"pageSize": min(int(limit), 1000)}
    if filter_expr:
        params["filter"] = filter_expr
    if from_time:
        params["from"] = from_time
    if to_time:
        params["to"] = to_time
    return await dynatrace_api_request("GET", "/api/v2/auditlogs", query_params=params)


@mcp.tool()
async def whoami() -> str:
    """Connectivity + auth check. Probes /api/v2/environments and reports config (no secrets)."""
    info = {
        "environment_url": DT_ENV_URL,
        "auth_mode": "api_token" if DT_API_TOKEN else (
            "oauth_client_credentials" if DT_OAUTH_CLIENT_ID else "none"),
        "write_enabled": DT_ALLOW_WRITE,
    }
    probe = json.loads(await dynatrace_api_request("GET", "/api/v2/environments"))
    info["connectivity"] = "ok" if probe.get("ok") else "failed"
    info["probe"] = probe
    return json.dumps(info, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
