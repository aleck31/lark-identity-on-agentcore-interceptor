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

Prereqs: `uv`, Docker, AWS `lab` profile (all scripts pin `--profile lab` /
`us-west-2`). The AgentCore Runtime and Gateway have no CloudFormation resources
in this region, so `deploy.sh` creates them via the control-plane CLI and feeds
their IDs back into `cdk.json`.

```bash
cp .env.example .env          # fill in Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh             # all phases (base CDK → runtime → gateway → SPA)
scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook/SPA URLs; allowlist you
scripts/deploy.sh --frontend  # re-inject SPA config (appId) + re-upload SPA
```

Config in `cdk.json`: `default_model_id` (`global.anthropic.claude-sonnet-5`),
`lark_api_domain` (`https://open.larksuite.com` international / `open.feishu.cn`),
`registration_open` (false = allowlist only), `presigned_url_expires`.

## Lark console setup (do this once, in order)

The webhook/SPA URLs come from `deploy.sh` output. In the Lark developer console:

1. **Add features**: enable **Bot** + **Web app**.
2. **Permissions & Scopes** — add all of these, then note that **subscribing to
   the message event requires the p2p scope** or single-chat messages are never
   pushed:
   - `im:message`, `im:message:readonly`
   - **`im:message.p2p_msg:readonly`** ← required for **single-chat** messages
   - `im:message:send_as_bot` (reply), `im:resource` (images)
   - `contact:user.base:readonly` (login → open_id)
3. **Events & Callbacks**: subscription mode = *Send to developer's server*; set
   Request URL to the webhook URL; enable **Encryption** (note the Encrypt Key);
   add event **`im.message.receive_v1`**.
4. **Security Settings**: add the SPA URL to **Redirect URLs** and **H5 trusted
   domains**. Leave IP allowlist empty (Lambda egress IPs are dynamic).
5. **Web app**: set Desktop + Mobile homepage to the SPA URL.
6. **Version Management & Release**: create a version and **publish**. Any change
   to scopes/events requires a **re-publish** to take effect.

Then `scripts/setup-lark.sh` stores credentials and allowlists your open_id. New
users: they message the bot, get a rejection with their `lark:ou_...` id, and an
admin runs `scripts/manage-allowlist.sh add lark:ou_...` (or set
`registration_open: true` to let anyone in).

## Test

```bash
scripts/test.sh              # agent (8) + router (7) + web_api (4)
```

## Deployment status

Verified end-to-end on the `lab` account (us-west-2):

- ✅ All 6 CDK stacks deploy; Cognito, Secrets, DynamoDB, API Gateways, Gateway +
  Interceptor + demo tool, CloudFront SPA.
- ✅ Lark **inbound**: webhook signature verify + AES decrypt + `url_verification`
  challenge (smoke-tested with the real Encrypt Key).
- ✅ Lark **round-trip**: a real single-chat message reaches the Router, resolves
  the user (`lark:{open_id}`), and the Router replies back into Lark.
- ✅ IAM: `InvokeAgentRuntime` **and** `InvokeAgentRuntimeForUser` (both required
  when passing a runtime user id).

Open / in progress:

- ⏳ **Agent container returns 500 with no runtime logs.** Root cause: AgentCore
  requires **ARM64** containers, and AWS only supports ARM64 images built **on an
  ARM64 host or via CodeBuild** — a QEMU cross-build on an x86 host is unreliable.
  Fix in progress: switch the build to the AgentCore Starter Toolkit's CodeBuild
  path (see `scripts/deploy.sh` runtime phase). The agent's `Dockerfile` also now
  includes `aws-opentelemetry-distro` and runs under `opentelemetry-instrument`
  (required for AgentCore log/trace collection).
- ⏳ **Desktop WSS bridge** (presigned WSS → agent :18789) is unverified pending
  a working container. Fallback if it doesn't hold: Lambda Function URL + SSE
  (server-side only; the SPA already renders streamed deltas).
