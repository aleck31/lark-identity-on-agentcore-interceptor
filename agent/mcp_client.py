"""Minimal MCP client for calling tools through the AgentCore Gateway.

The Gateway speaks MCP (JSON-RPC 2.0 over Streamable HTTP). We attach the
end-user's Cognito JWT as ``Authorization: Bearer``; the Gateway authorizer
verifies it and, with ``passRequestHeaders=true``, the Request Interceptor reads
its claims to inject the correct downstream credential. The agent itself holds
no downstream tool key.

Kept dependency-free (urllib) and synchronous — tool calls run in the agent's
tool loop. If GATEWAY_URL is unset, tool discovery returns empty and the agent
runs as a plain chat model.
"""

from __future__ import annotations

import json
import os
import logging
import urllib.request
import urllib.error

log = logging.getLogger("agent.mcp")

_GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")
_TIMEOUT = float(os.environ.get("MCP_TIMEOUT_SECONDS", "30"))


def _rpc(method: str, params: dict, jwt: str, req_id: int = 1) -> dict:
    """One JSON-RPC call to the Gateway MCP endpoint. Raises on transport/HTTP error."""
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        _GATEWAY_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {jwt}",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read().decode()
    # Streamable HTTP may frame the JSON as an SSE "data:" line.
    if raw.lstrip().startswith("data:"):
        raw = "\n".join(
            line[len("data:"):].strip()
            for line in raw.splitlines()
            if line.strip().startswith("data:")
        )
    return json.loads(raw)


def list_tools(jwt: str) -> list[dict]:
    """Return the Gateway tool catalog as Bedrock Converse toolSpec dicts.

    Returns [] if no Gateway is configured or discovery fails (agent degrades to
    plain chat rather than erroring).
    """
    if not _GATEWAY_URL:
        return []
    try:
        result = _rpc("tools/list", {}, jwt).get("result", {})
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
        log.warning("tools/list failed, running without tools: %s", e)
        return []

    specs = []
    for t in result.get("tools", []):
        specs.append({
            "toolSpec": {
                "name": t["name"],
                "description": t.get("description", t["name"]),
                "inputSchema": {"json": t.get("inputSchema", {"type": "object", "properties": {}})},
            }
        })
    return specs


def call_tool(name: str, arguments: dict, jwt: str) -> str:
    """Invoke a Gateway tool; return a text result (or an error string)."""
    if not _GATEWAY_URL:
        return f"[tool {name} unavailable: no gateway configured]"
    try:
        resp = _rpc("tools/call", {"name": name, "arguments": arguments}, jwt)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as e:
        log.warning("tools/call %s failed: %s", name, e)
        return f"[tool {name} error: {e}]"

    if "error" in resp:
        return f"[tool {name} error: {resp['error'].get('message', resp['error'])}]"

    content = resp.get("result", {}).get("content", [])
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    return "\n".join(texts) or json.dumps(resp.get("result", {}))
