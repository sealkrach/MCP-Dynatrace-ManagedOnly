#!/usr/bin/env python3
"""
Dynatrace Bridge MCP server.

A self-hosted MCP server that talks DIRECTLY to the Dynatrace Platform API,
working around managed-MCP limitations around DQL execution and raw API calls.

It exposes:
  - execute_dql            : run any DQL query against Grail (async execute + poll)
  - dynatrace_api_request  : generic authenticated call to ANY Dynatrace platform API
  - fetch_logs             : convenience DQL wrapper for logs
  - list_davis_problems    : convenience DQL wrapper for Davis problems
  - whoami                 : connectivity / auth / scope check

Auth: Platform Token (preferred) OR OAuth client-credentials.
Runs over stdio (standard for local MCP clients: VS Code, Claude Desktop, etc.).

Read the companion README for setup. Designed for regulated/banking use:
write-style HTTP methods are gated behind DT_ALLOW_WRITE.
"""

from __future__ import annotations

import os
import time
import json
import asyncio
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
    """Accept a full URL or a bare environment id and return the apps base URL."""
    if not raw:
        raise RuntimeError(
            "DT_ENVIRONMENT_URL is required, e.g. https://abc12345.apps.dynatrace.com "
            "(or just the environment id 'abc12345')."
        )
    raw = raw.strip().rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    # bare id -> build the canonical apps URL
    return f"https://{raw}.apps.dynatrace.com"


DT_ENV_URL = _normalize_env_url(_env("DT_ENVIRONMENT_URL"))
DT_PLATFORM_TOKEN = _env("DT_PLATFORM_TOKEN")

DT_OAUTH_CLIENT_ID = _env("DT_OAUTH_CLIENT_ID")
DT_OAUTH_CLIENT_SECRET = _env("DT_OAUTH_CLIENT_SECRET")
DT_OAUTH_SCOPES = _env(
    "DT_OAUTH_SCOPES",
    "storage:buckets:read storage:logs:read storage:metrics:read "
    "storage:events:read storage:bizevents:read storage:entities:read "
    "storage:system:read",
)
DT_SSO_TOKEN_URL = _env("DT_SSO_TOKEN_URL", "https://sso.dynatrace.com/sso/oauth2/token")

# Safety: anything that is not GET is refused unless this is explicitly "true".
DT_ALLOW_WRITE = (_env("DT_ALLOW_WRITE", "false") or "false").lower() == "true"

# Polling / timeouts
DT_REQUEST_TIMEOUT_MS = int(_env("DT_REQUEST_TIMEOUT_MS", "30000"))   # per execute/poll call
DT_MAX_POLL_SECONDS = int(_env("DT_MAX_POLL_SECONDS", "120"))         # total wait budget
DT_HTTP_TIMEOUT = float(_env("DT_HTTP_TIMEOUT", "60"))                # httpx client timeout

QUERY_EXECUTE = "/platform/storage/query/v1/query:execute"
QUERY_POLL = "/platform/storage/query/v1/query:poll"
QUERY_CANCEL = "/platform/storage/query/v1/query:cancel"

mcp = FastMCP("dynatrace-bridge")

# --------------------------------------------------------------------------- #
# Auth: resolve a bearer token (platform token used directly, or OAuth exchange)
# --------------------------------------------------------------------------- #

_token_cache: dict[str, Any] = {"value": None, "expires_at": 0.0}


async def _get_bearer() -> str:
    """Return a bearer token, preferring a Platform Token; else OAuth client creds."""
    if DT_PLATFORM_TOKEN:
        # Platform tokens (dt0s16.*) are long-lived and used directly as Bearer.
        return DT_PLATFORM_TOKEN

    if not (DT_OAUTH_CLIENT_ID and DT_OAUTH_CLIENT_SECRET):
        raise RuntimeError(
            "No credentials configured. Set DT_PLATFORM_TOKEN, or "
            "DT_OAUTH_CLIENT_ID + DT_OAUTH_CLIENT_SECRET."
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
        "Authorization": f"Bearer {await _get_bearer()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# --------------------------------------------------------------------------- #
# Core DQL execution (async execute + poll)
# --------------------------------------------------------------------------- #

async def _run_dql(
    query: str,
    from_time: Optional[str],
    to_time: Optional[str],
    max_records: int,
) -> dict[str, Any]:
    headers = await _auth_headers()
    body: dict[str, Any] = {
        "query": query,
        "requestTimeoutMilliseconds": DT_REQUEST_TIMEOUT_MS,
        "maxResultRecords": max_records,
        "fetchTimeoutSeconds": 60,
    }
    if from_time:
        body["defaultTimeframeStart"] = from_time
    if to_time:
        body["defaultTimeframeEnd"] = to_time

    async with httpx.AsyncClient(timeout=DT_HTTP_TIMEOUT) as client:
        resp = await client.post(DT_ENV_URL + QUERY_EXECUTE, headers=headers, json=body)
        if resp.status_code >= 400:
            return _http_error("query:execute", resp)

        data = resp.json()
        state = data.get("state")

        # Fast path: query already finished inside the execute call.
        if state == "SUCCEEDED" and data.get("result") is not None:
            return _shape_result(data)

        request_token = data.get("requestToken")
        if not request_token:
            # Failed / cancelled / unexpected with no token to poll.
            return {"ok": state == "SUCCEEDED", "state": state, "raw": data}

        # Poll loop.
        deadline = time.time() + DT_MAX_POLL_SECONDS
        params = {
            "request-token": request_token,
            "request-timeout-milliseconds": str(DT_REQUEST_TIMEOUT_MS),
        }
        while True:
            poll = await client.get(DT_ENV_URL + QUERY_POLL, headers=headers, params=params)
            if poll.status_code >= 400:
                return _http_error("query:poll", poll)
            pdata = poll.json()
            pstate = pdata.get("state")
            if pstate == "SUCCEEDED":
                return _shape_result(pdata)
            if pstate in ("FAILED", "CANCELLED"):
                return {"ok": False, "state": pstate, "error": pdata.get("error"), "raw": pdata}
            if time.time() > deadline:
                # Best-effort cancel so we don't leave the query running.
                try:
                    await client.post(DT_ENV_URL + QUERY_CANCEL, headers=headers, params=params)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "state": "TIMEOUT",
                    "error": f"Query did not finish within DT_MAX_POLL_SECONDS={DT_MAX_POLL_SECONDS}s. "
                             f"Narrow the timeframe or add a stricter filter/limit.",
                }
            await asyncio.sleep(2)


def _shape_result(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result") or {}
    records = result.get("records", [])
    types = result.get("types")
    meta = result.get("metadata", {})
    grail = (meta or {}).get("grail", {})
    return {
        "ok": True,
        "state": data.get("state", "SUCCEEDED"),
        "record_count": len(records),
        "records": records,
        "types": types,
        "scanned_bytes": grail.get("scannedBytes"),
        "scanned_records": grail.get("scannedRecords"),
    }


def _http_error(where: str, resp: httpx.Response) -> dict[str, Any]:
    hint = ""
    if resp.status_code in (401, 403):
        hint = (" — auth/permission issue. Check the token validity and that it carries the "
                "required storage:* read scopes (and mcp/platform permissions).")
    body = resp.text
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
async def execute_dql(
    query: str,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    max_records: int = 1000,
) -> str:
    """Execute a Dynatrace Query Language (DQL) query against Grail and return the records.

    This is the primary workaround for managed MCPs that cannot execute DQL.

    Args:
        query: The DQL query, e.g. 'fetch logs | filter loglevel == "ERROR" | limit 50'.
        from_time: Optional ISO-8601 start (e.g. '2026-05-29T12:00:00Z'). Omit to use the
            API default timeframe. You may also set the timeframe inside the DQL itself.
        to_time: Optional ISO-8601 end.
        max_records: Cap on returned records (default 1000). Keep small for exploration.

    Returns:
        JSON string with ok/state/record_count/records/types and Grail scan stats.
    """
    out = await _run_dql(query, from_time, to_time, max_records)
    return json.dumps(out, default=str)


@mcp.tool()
async def dynatrace_api_request(
    method: str,
    path: str,
    query_params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> str:
    """Make an authenticated request to ANY Dynatrace platform API endpoint.

    Generic escape hatch for endpoints not wrapped by a dedicated tool
    (problems, entities, settings, automations, etc.).

    Safety: non-GET methods are refused unless the server is started with
    DT_ALLOW_WRITE=true. Even then, prefer dedicated review for state changes.

    Args:
        method: HTTP method ('GET', 'POST', 'PUT', 'PATCH', 'DELETE').
        path: Path beginning with '/', e.g. '/platform/classic/environment-api/v2/problems'.
        query_params: Optional querystring parameters.
        body: Optional JSON body for write methods.

    Returns:
        JSON string with status code and the parsed/raw response.
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
    filter_dql: Optional[str] = None,
    limit: int = 50,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
) -> str:
    """Convenience wrapper to fetch recent logs via DQL.

    Args:
        filter_dql: Optional DQL filter expression WITHOUT the leading 'filter',
            e.g. 'loglevel == "ERROR" and k8s.namespace.name == "payments"'.
        limit: Max log lines (default 50).
        from_time / to_time: Optional ISO-8601 timeframe bounds.
    """
    q = "fetch logs"
    if filter_dql:
        q += f" | filter {filter_dql}"
    q += f" | sort timestamp desc | limit {int(limit)}"
    out = await _run_dql(q, from_time, to_time, max_records=limit)
    out["dql"] = q
    return json.dumps(out, default=str)


@mcp.tool()
async def list_davis_problems(
    open_only: bool = True,
    limit: int = 20,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
) -> str:
    """List Davis problems via DQL (events table).

    Note: the exact Davis schema can vary by tenant/version; if fields are missing,
    adjust the DQL with execute_dql. This wrapper uses the documented events pattern.

    Args:
        open_only: If True, exclude CLOSED problems.
        limit: Max problems to return.
        from_time / to_time: Optional ISO-8601 timeframe bounds.
    """
    q = 'fetch events | filter event.kind == "DAVIS_PROBLEM"'
    if open_only:
        q += ' and event.status != "CLOSED"'
    q += (' | fields timestamp, event.id, display_id, event.status, '
          'event.category, dt.davis.event_ids, affected_entity_ids, '
          'event.name | sort timestamp desc | limit ' + str(int(limit)))
    out = await _run_dql(q, from_time, to_time, max_records=limit)
    out["dql"] = q
    return json.dumps(out, default=str)


@mcp.tool()
async def whoami() -> str:
    """Connectivity + auth check. Runs a trivial DQL and reports config (no secrets)."""
    info = {
        "environment_url": DT_ENV_URL,
        "auth_mode": "platform_token" if DT_PLATFORM_TOKEN else (
            "oauth_client_credentials" if DT_OAUTH_CLIENT_ID else "none"),
        "write_enabled": DT_ALLOW_WRITE,
        "max_poll_seconds": DT_MAX_POLL_SECONDS,
    }
    probe = await _run_dql("data record(probe = 1)", None, None, 1)
    info["connectivity"] = "ok" if probe.get("ok") else "failed"
    info["probe"] = probe
    return json.dumps(info, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
