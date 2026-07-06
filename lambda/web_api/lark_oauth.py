"""Lark login-code → user identity exchange (web_api side).

Flow for the Lark-embedded web UI (h5sdk tt.requestAccess yields a short-lived
code):
  1. code + app_access_token  -> authen/v2/oauth/token -> user_access_token
  2. user_access_token         -> authen/v1/user_info   -> open_id/union_id/email

We use app_access_token (app-level) to redeem the code. It is fetched and cached
like the tenant token.
"""

from __future__ import annotations

import json
import os
import time
import logging
import urllib.request

import boto3

log = logging.getLogger("web_api.lark_oauth")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_SECRET_ID = os.environ.get("LARK_SECRET_ID", "")
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")

_secrets = boto3.client("secretsmanager", region_name=_REGION)
_creds_cache: dict | None = None
_app_token_cache = {"token": "", "expires_at": 0.0}


def _creds() -> dict:
    global _creds_cache
    if _creds_cache is None:
        _creds_cache = json.loads(_secrets.get_secret_value(SecretId=_SECRET_ID)["SecretString"])
    return _creds_cache


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _app_access_token() -> str:
    if _app_token_cache["token"] and time.time() < _app_token_cache["expires_at"] - 300:
        return _app_token_cache["token"]
    c = _creds()
    r = _post_json(
        f"{_API_DOMAIN}/open-apis/auth/v3/app_access_token/internal",
        {"app_id": c["appId"], "app_secret": c["appSecret"]},
    )
    if r.get("code") != 0:
        raise RuntimeError(f"app_access_token error: {r}")
    _app_token_cache["token"] = r["app_access_token"]
    _app_token_cache["expires_at"] = time.time() + r.get("expire", 7200)
    return _app_token_cache["token"]


def exchange_code(code: str) -> dict:
    """Redeem a login code. Returns {open_id, union_id, email, name}."""
    c = _creds()
    token_resp = _post_json(
        f"{_API_DOMAIN}/open-apis/authen/v2/oauth/token",
        {"grant_type": "authorization_code", "client_id": c["appId"],
         "client_secret": c["appSecret"], "code": code},
    )
    user_access_token = token_resp.get("access_token")
    if not user_access_token:
        raise RuntimeError(f"oauth token error: {token_resp}")

    info = _get_json(
        f"{_API_DOMAIN}/open-apis/authen/v1/user_info",
        {"Authorization": f"Bearer {user_access_token}"},
    )
    if info.get("code") != 0:
        raise RuntimeError(f"user_info error: {info}")
    data = info["data"]
    return {
        "open_id": data.get("open_id", ""),
        "union_id": data.get("union_id", ""),
        "email": data.get("email", data.get("enterprise_email", "")),
        "name": data.get("name", ""),
    }
