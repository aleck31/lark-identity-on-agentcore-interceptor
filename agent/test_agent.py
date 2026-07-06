"""Unit tests for agent logic that doesn't require live AWS.

Run: cd agent && uv run --with boto3 --with aiohttp python -m pytest test_agent.py -v
(or from repo root: uv run python -m pytest agent/test_agent.py)
"""

import base64
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ------------------------------- identity -----------------------------------

def test_derive_password_deterministic_and_complex():
    import identity
    with mock.patch.object(identity, "_get_salt", return_value="test-salt"):
        p1 = identity._derive_password("lark:ou_abc")
        p2 = identity._derive_password("lark:ou_abc")
        p3 = identity._derive_password("lark:ou_xyz")
    assert p1 == p2                       # deterministic
    assert p1 != p3                       # per-user
    assert p1.endswith("Aa1!")            # complexity suffix
    assert len(p1) == 36                  # 32 hex + 4 suffix


def test_jwt_exp_parses_unverified():
    import identity
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234567890}).encode()).decode().rstrip("=")
    token = f"header.{payload}.sig"
    assert identity._jwt_exp(token) == 1234567890.0


def test_jwt_exp_bad_token_returns_zero():
    import identity
    assert identity._jwt_exp("not-a-jwt") == 0.0


# ------------------------------- mcp_client ---------------------------------

def test_list_tools_no_gateway_returns_empty():
    import mcp_client
    with mock.patch.object(mcp_client, "_GATEWAY_URL", ""):
        assert mcp_client.list_tools("jwt") == []


def test_list_tools_maps_to_bedrock_toolspec():
    import mcp_client
    fake = {"result": {"tools": [
        {"name": "whoami", "description": "who", "inputSchema": {"type": "object", "properties": {}}},
    ]}}
    with mock.patch.object(mcp_client, "_GATEWAY_URL", "https://gw"), \
         mock.patch.object(mcp_client, "_rpc", return_value=fake):
        specs = mcp_client.list_tools("jwt")
    assert specs[0]["toolSpec"]["name"] == "whoami"
    assert specs[0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"


def test_call_tool_extracts_text_content():
    import mcp_client
    fake = {"result": {"content": [{"type": "text", "text": "you are lark:ou_x"}]}}
    with mock.patch.object(mcp_client, "_GATEWAY_URL", "https://gw"), \
         mock.patch.object(mcp_client, "_rpc", return_value=fake):
        out = mcp_client.call_tool("whoami", {}, "jwt")
    assert out == "you are lark:ou_x"


def test_call_tool_surfaces_rpc_error():
    import mcp_client
    fake = {"error": {"message": "bad key"}}
    with mock.patch.object(mcp_client, "_GATEWAY_URL", "https://gw"), \
         mock.patch.object(mcp_client, "_rpc", return_value=fake):
        out = mcp_client.call_tool("whoami", {}, "jwt")
    assert "bad key" in out


# ------------------------------- agent_core ---------------------------------

def test_final_text_picks_last_assistant():
    import agent_core
    msgs = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "first"}]},
        {"role": "user", "content": [{"toolResult": {}}]},
        {"role": "assistant", "content": [{"text": "final answer"}]},
    ]
    assert agent_core._final_text(msgs) == "final answer"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
