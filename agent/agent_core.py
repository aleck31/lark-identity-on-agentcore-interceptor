"""Core agent: Strands Agent on Bedrock, with AgentCore Memory for session
continuity and an MCP Gateway client for per-user tool identity pass-through.

Strands provides conversation memory (via AgentCoreMemorySessionManager),
streaming, and MCP tool wiring out of the box, instead of a hand-rolled Converse
loop. The project's Memory resource is STM-only, so the session manager persists
raw conversation turns and reloads them each turn — history is keyed by
(actor_id, session_id), remembered across reconnects and both Lark entrypoints.

Identity pass-through is preserved: for each end-user we mint their Cognito access
token and attach it as the Bearer on the MCP (Gateway) connection, so the Gateway
authorizer + interceptor see the real end-user. The agent never holds a downstream
tool credential.

`run_chat` returns the final text; `stream_chat` yields text deltas. Both take an
`actor_id` (e.g. "lark:ou_xxx"); conversation history is managed by Memory.
"""

from __future__ import annotations

import hashlib
import os
import logging
from datetime import timedelta
from typing import Iterator

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

from identity import get_user_jwt

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")
_MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
_SYSTEM = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful assistant embedded in Lark. Be concise. "
    "Use the provided tools when they help answer the user.",
)

_model = BedrockModel(model_id=_MODEL_ID, streaming=True)


def _session_id_for(actor_id: str) -> str:
    """Deterministic per-user session id: one long conversation thread per user,
    shared across reconnects and entrypoints (STM retains it 30 days)."""
    return "sess-" + hashlib.sha256(actor_id.encode()).hexdigest()[:32]


def _session_manager(actor_id: str):
    """AgentCore Memory (STM) session manager for this user, or None if no Memory
    resource is configured (agent still answers, just without recall)."""
    if not _MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    cfg = AgentCoreMemoryConfig(
        memory_id=_MEMORY_ID,
        session_id=_session_id_for(actor_id),
        actor_id=actor_id,
    )
    return AgentCoreMemorySessionManager(cfg, region_name=_REGION)


def _mcp_client(actor_id: str, email: str):
    """MCP client to the AgentCore Gateway, authenticated as the end-user.

    The user's Cognito ACCESS token is the Bearer; the Gateway authorizer
    validates client_id and the interceptor reads identity from it. Returns None
    if no Gateway is configured (agent runs as plain chat)."""
    if not _GATEWAY_URL:
        return None
    token = get_user_jwt(actor_id, email)
    return MCPClient(lambda: streamablehttp_client(
        _GATEWAY_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timedelta(seconds=30),
    ))


def _build_agent(actor_id: str, mcp) -> Agent:
    tools = mcp.list_tools_sync() if mcp else []
    return Agent(
        model=_model,
        system_prompt=_SYSTEM,
        tools=tools,
        session_manager=_session_manager(actor_id),
    )


def run_chat(actor_id: str, message: str, email: str = "") -> str:
    """Non-streaming chat → assistant's final text. History is managed by Memory."""
    mcp = _mcp_client(actor_id, email)
    if mcp:
        with mcp:
            return str(_build_agent(actor_id, mcp)(message))
    return str(_build_agent(actor_id, None)(message))


def stream_chat(actor_id: str, message: str, email: str = "") -> Iterator[str]:
    """Streaming chat for the WebSocket path. Yields text deltas. Strands handles
    tool calls + memory; we forward model text as it streams."""
    import asyncio

    def _drain(agent):
        loop = asyncio.new_event_loop()
        try:
            agen = agent.stream_async(message)
            while True:
                try:
                    event = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
                # Strands emits {"data": "<text chunk>"} for streamed model text.
                if isinstance(event, dict) and "data" in event:
                    yield event["data"]
        finally:
            loop.close()

    mcp = _mcp_client(actor_id, email)
    if mcp:
        with mcp:
            yield from _drain(_build_agent(actor_id, mcp))
    else:
        yield from _drain(_build_agent(actor_id, None))
