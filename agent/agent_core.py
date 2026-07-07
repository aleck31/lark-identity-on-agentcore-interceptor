"""Core agent: Bedrock Converse with a tool loop that calls the AgentCore Gateway.

`run_chat` returns the final text (used by webhook + sync paths). `stream_chat`
yields text deltas (used by the WebSocket desktop path). Both resolve the
end-user's Cognito JWT once and thread it through every tool call so downstream
MCP tools receive the user's identity.
"""

from __future__ import annotations

import os
import logging
from typing import Iterator

import boto3

from identity import get_user_jwt
import mcp_client

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_MAX_TOOL_TURNS = int(os.environ.get("MAX_TOOL_TURNS", "6"))
_SYSTEM = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful assistant embedded in Lark. Be concise. "
    "Use the provided tools when they help answer the user.",
)

_bedrock = boto3.client("bedrock-runtime", region_name=_REGION)


def _jwt_for(actor_id: str, email: str = "") -> str:
    """actor_id is the stable identity, e.g. 'lark:ou_xxx' → Cognito username."""
    return get_user_jwt(actor_id, email)


def _run_tool_loop(messages: list[dict], jwt: str) -> list[dict]:
    """Drive Converse until the model stops requesting tools. Mutates+returns messages."""
    tools = mcp_client.list_tools(jwt)
    tool_config = {"tools": tools} if tools else None

    for _ in range(_MAX_TOOL_TURNS):
        kwargs = dict(
            modelId=_MODEL_ID,
            messages=messages,
            system=[{"text": _SYSTEM}],
            inferenceConfig={"maxTokens": 4096},
        )
        if tool_config:
            kwargs["toolConfig"] = tool_config

        resp = _bedrock.converse(**kwargs)
        out_msg = resp["output"]["message"]
        messages.append(out_msg)

        if resp.get("stopReason") != "tool_use":
            return messages

        tool_results = []
        for block in out_msg["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            result_text = mcp_client.call_tool(tu["name"], tu.get("input", {}), jwt)
            tool_results.append({
                "toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": result_text}],
                }
            })
        messages.append({"role": "user", "content": tool_results})

    return messages


def _final_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            texts = [b["text"] for b in msg.get("content", []) if "text" in b]
            if texts:
                return "\n".join(texts)
    return ""


def run_chat(actor_id: str, message: str, email: str = "", history: list[dict] | None = None) -> str:
    """Non-streaming chat. Returns the assistant's final text."""
    jwt = _jwt_for(actor_id, email)
    messages = (history or []) + [{"role": "user", "content": [{"text": message}]}]
    messages = _run_tool_loop(messages, jwt)
    return _final_text(messages)


def stream_chat(actor_id: str, message: str, email: str = "", history: list[dict] | None = None) -> Iterator[str]:
    """Streaming chat for the WebSocket path.

    Tool turns (if any) are resolved non-streaming first; the final answer is
    then streamed token-by-token. Simple and correct for a PoC — avoids
    interleaving tool calls with a live token stream.
    """
    jwt = _jwt_for(actor_id, email)
    messages = (history or []) + [{"role": "user", "content": [{"text": message}]}]
    tools = mcp_client.list_tools(jwt)

    # Resolve any tool turns first (non-streaming), leaving a tool-free final turn.
    if tools:
        messages = _run_tool_loop(messages, jwt)
        # _run_tool_loop already produced the final assistant text; stream it out.
        yield _final_text(messages)
        return

    resp = _bedrock.converse_stream(
        modelId=_MODEL_ID,
        messages=messages,
        system=[{"text": _SYSTEM}],
        inferenceConfig={"maxTokens": 4096},
    )
    for event in resp["stream"]:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                yield delta["text"]
