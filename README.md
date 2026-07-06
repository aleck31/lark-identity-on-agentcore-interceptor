# lark-agent on Bedrock AgentCore

A simple agent running on Amazon Bedrock AgentCore Runtime, reachable from **two
Lark (Feishu) entrypoints** — chat messages and a desktop-client-embedded web UI
— with **Lark as the identity provider** and per-user identity **forwarded to
downstream MCP tools** through an AgentCore Gateway Request Interceptor.

## Architecture

```
  Lark message  ─────▶  Router Lambda ─┐         Lark desktop web UI ──▶ SPA (S3/CloudFront)
  (webhook)             verify/decrypt │           h5sdk requestAccess ─▶ code
                        resolve user   │                                   │
                                       ▼                                   ▼
                             ┌───────────────────┐            web_api Lambda
                             │ InvokeAgentRuntime │◀───────────  /api/lark/auth  (code→Cognito JWT)
                             │  (per-user session)│              /api/session    (→ presigned WSS)
                             └─────────┬─────────┘                     │
                                       ▼                                │ browser connects WSS
                             ┌───────────────────┐◀───────────────────┘ (platform bridges to :18789)
                             │  Agent container  │
                             │  /ping /invocations (8080)  +  WS (18789)
                             │  Bedrock Converse (Sonnet 5) + tool loop
                             └─────────┬─────────┘
                                       │ MCP call, Bearer = per-user Cognito JWT
                                       ▼
                             ┌───────────────────┐
                             │ AgentCore Gateway │  customJWTAuthorizer (Cognito)
                             │  + Interceptor λ  │  passRequestHeaders=true
                             │  injects per-user │  → downstream key from Secrets Manager
                             │  credential+ident │  → demo whoami tool (proves pass-through)
                             └───────────────────┘

  Identity: every entrypoint resolves to  lark:{open_id}  (shared session/workspace).
  Lark is NOT standard OIDC → a token-exchange step mints a Cognito JWT the
  authorizers can verify.
```

## Layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app (uv-managed deps) — 6 stacks |
| `stacks/` | security, agentcore, router, webui, gateway, observability |
| `agent/` | Python agent container (HTTP contract + WS + Bedrock + MCP client) |
| `lambda/router/` | Lark webhook: verify/decrypt/tenant-token/send |
| `lambda/web_api/` | Lark login exchange + session bootstrap (presigned WSS) |
| `lambda/interceptor/` | Gateway Request Interceptor (per-user credential injection) |
| `lambda/tools/` | demo `whoami` MCP tool target |
| `web-ui/` | Lark-embedded SPA (no build step) |
| `scripts/` | deploy / setup-lark / manage-allowlist / test |

## Deploy

Prereqs: `uv`, Docker (for the agent image build), AWS `lab` profile.
The AgentCore Runtime and Gateway have no CloudFormation resources in this
region, so `deploy.sh` creates them via the control-plane CLI and feeds their
IDs back into `cdk.json`.

```bash
scripts/deploy.sh            # all phases (base CDK → runtime → gateway → SPA)
scripts/setup-lark.sh        # store Lark creds, print webhook/SPA URLs to register
scripts/deploy.sh --frontend # re-inject SPA config (appId) + re-upload
```

Config in `cdk.json`: `default_model_id` (`global.anthropic.claude-sonnet-5`),
`lark_api_domain` (`https://open.larksuite.com` international / `open.feishu.cn`),
`registration_open`, `presigned_url_expires`.

## Test

```bash
scripts/test.sh              # agent (8) + router (7) + web_api (4)
```

## Known validation point

The desktop WSS path assumes the AgentCore platform auto-bridges a presigned WSS
connection to the agent's port 18789. This is validated for a specific server in
the reference project; the first post-deploy smoke test confirms it for this
agent. If it does not hold, the fallback is Lambda Function URL + SSE (server-side
change only; the SPA already renders streamed deltas).
