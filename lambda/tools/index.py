"""Demo MCP tool target behind the Gateway — proves identity pass-through.

Exposes one tool, `whoami`, which reports the end-user identity and downstream
credential that the Request Interceptor injected. This is the end-to-end proof
that (a) the agent holds no downstream key, and (b) the tool still learns which
end-user is calling.

AgentCore Gateway invokes a Lambda target with the tool name in the client
context (bedrockAgentCoreToolName) and the arguments as the event. The injected
headers/identity arrive via the interceptor's transformed request; for a Lambda
target the Gateway forwards them in `event` under a conventional key. We read
identity defensively from a few likely locations so the PoC is robust to the
exact envelope shape.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _tool_name(context) -> str:
    try:
        cc = context.client_context
        if cc and cc.custom:
            return cc.custom.get("bedrockAgentCoreToolName", "") or cc.custom.get("toolName", "")
    except Exception:
        pass
    return ""


def _extract_identity(event: dict) -> dict:
    """Find the injected end-user identity headers wherever the Gateway put them."""
    # common shapes: event["headers"], event["requestHeaders"], event["__injectedHeaders"]
    for key in ("headers", "requestHeaders", "__injectedHeaders"):
        h = event.get(key)
        if isinstance(h, dict):
            lower = {k.lower(): v for k, v in h.items()}
            if "x-end-user-id" in lower:
                return {
                    "endUserId": lower.get("x-end-user-id", ""),
                    "endUserTenant": lower.get("x-end-user-tenant", ""),
                    "authorizationReceived": bool(lower.get("authorization")),
                }
    return {"endUserId": event.get("endUserId", "unknown"),
            "endUserTenant": event.get("endUserTenant", "unknown"),
            "authorizationReceived": False}


def handler(event, context):
    cc_custom = {}
    try:
        if context.client_context and context.client_context.custom:
            cc_custom = dict(context.client_context.custom)
    except Exception:
        pass
    logger.info("tool invoked: name=%s event=%s clientContext=%s",
                _tool_name(context), json.dumps(event)[:300], json.dumps(cc_custom)[:500])

    ident = _extract_identity(event if isinstance(event, dict) else {})
    # The interceptor forwards identity inside the tool arguments (Lambda
    # targets receive tools/call arguments as the event; custom headers are
    # dropped). The injected Authorization survives via clientContext
    # bedrockAgentCorePropagatedHeaders.
    if ident["endUserId"] == "unknown" and isinstance(event, dict):
        propagated = {k.lower(): v for k, v in
                      (cc_custom.get("bedrockAgentCorePropagatedHeaders") or {}).items()}
        ident = {
            "endUserId": event.get("_endUserId", "unknown"),
            "endUserTenant": event.get("_endUserTenant", "unknown"),
            "authorizationReceived": bool(propagated.get("authorization")),
        }
    result_text = (
        f"whoami: you are '{ident['endUserId']}' (tenant '{ident['endUserTenant']}'). "
        f"The gateway injected a downstream credential: {ident['authorizationReceived']}. "
        f"The agent itself never saw this credential."
    )

    # MCP tool result shape
    return {
        "content": [{"type": "text", "text": result_text}],
        "isError": False,
    }
