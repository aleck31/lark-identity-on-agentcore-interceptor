# Lark Identity on AgentCore — Reference Implementation

A reference implementation of enterprise identity on Amazon Bedrock AgentCore, using **Lark (Feishu) as the identity provider**. A simple agent is reachable from **two Lark entrypoints** — chat messages and a desktop-client-embedded web UI — that both resolve to the same `lark:{open_id}` identity, and each end-user's identity is **forwarded to downstream MCP tools** through an AgentCore Gateway Request Interceptor (the agent itself never holds the downstream credential).

> Resources are prefixed `lark-agent-*` in code (the original project name); the repository is `lark-identity-on-agentcore`.

## Architecture

```
  Lark message  ────▶  Router Lambda ─┐        Lark desktop web UI ──▶ SPA (S3/CloudFront)
  (webhook)            verify/decrypt  │          h5sdk requestAccess ─▶ login code
                       resolve user    │                                  │
                                       │                                  ▼
                                       │                        web_api Lambda
                                       │      POST /api/lark/auth  code ─▶ Cognito JWT (Lark is IdP)
                                       │      POST /api/session    JWT  ─▶ presigned WSS URL
                                       │                                  │
             InvokeAgentRuntime (SigV4)│                                  │ browser opens WSS
             payload carries actorId   ▼                                  ▼ (platform bridges to /ws)
                             ┌─────────────────────────────────────────────────────┐
                             │  Agent container (ARM64, AgentCore Runtime)         │
                             │    :8080  /ping  /invocations(POST) /ws(WebSocket)  │
                             │    Bedrock Converse + tool loop                     │
                             └───────────────────────┬─────────────────────────────┘
                                       │ MCP call, Bearer = user's Cognito ACCESS token
                                       ▼
                             ┌───────────────────┐
                             │ AgentCore Gateway │  customJWTAuthorizer (Cognito, allowedClients)
                             │  + Interceptor λ  │  passRequestHeaders=true
                             │                   │  reads identity from JWT → injects per-tenant
                             │                   │  downstream key (Secrets Manager) + end-user id
                             └─────────┬─────────┘  → demo whoami tool (proves pass-through)
                                       ▼
                                  downstream MCP tool (agent never holds the key)

  Identity: both entrypoints resolve to  lark:{open_id}  (shared session/workspace).
  Lark is NOT standard OIDC → web_api exchanges the login code for a Cognito JWT
  the API Gateway + AgentCore Gateway authorizers can verify.
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

Prereqs: `uv`, Docker, the AgentCore CLI (`npm i -g @aws/agentcore`), and AWS
credentials. Scripts default to the `default` profile / `us-west-2`; override
with `PROFILE=... REGION=...`. The AgentCore Runtime and
Gateway have no CloudFormation resources in this region: the **Gateway** is
created by `deploy.sh` via the control-plane CLI (IDs fed back into `cdk.json`),
and the **Runtime image** is built (ARM64, CodeBuild) and deployed with the
AgentCore CLI.

```bash
cp .env.example .env          # fill in Lark appId/appSecret/encryptKey/token + your open_id
scripts/deploy.sh --phase1    # CDK base stacks (security, agentcore, router, gateway, observability)
scripts/deploy.sh --gateway   # create the MCP Gateway + interceptor + demo target

# Runtime: build ARM64 in the cloud + deploy (once configured, re-run deploy to update)
agentcore configure -e agent/server.py -n lark_agent_agent \
  --execution-role "$(aws cloudformation describe-stacks --stack-name lark-agent-agentcore \
    --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" --output text)" \
  -dt container -p HTTP -r us-west-2 --non-interactive
agentcore deploy --auto-update-on-conflict \
  --env BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-5 \
  --env COGNITO_USER_POOL_ID=<pool> --env COGNITO_CLIENT_ID=<client> \
  --env COGNITO_PASSWORD_SECRET_ID=lark-agent/cognito-password-secret \
  --env GATEWAY_URL=<gateway_url>

scripts/setup-lark.sh         # read .env → Secrets Manager; print webhook/SPA URLs; allowlist you
scripts/deploy.sh --frontend  # deploy WebUI stack, inject SPA config, upload SPA
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

All four goals verified end-to-end on an AWS account:

- ✅ **Lark chat**: a real single-chat message → Router (verify + AES decrypt) →
  resolve `lark:{open_id}` → AgentCore Runtime → model reply back in Lark.
  `/whoami` in Lark reports the caller's identity.
- ✅ **Desktop embed**: opening the Web app inside the Lark client runs h5sdk
  免登 → `POST /api/lark/auth` (code → Cognito JWT) → `POST /api/session`
  (presigned WSS) → the AgentCore platform bridges the browser's WSS to the
  agent's `/ws` on port 8080 → streaming reply. Shows the user's display name.
- ✅ **Unified identity**: both entrypoints resolve to the same `lark:{open_id}`
  (session/workspace shared).
- ✅ **MCP identity pass-through**: the agent mints the user's Cognito **access**
  token → AgentCore Gateway (`customJWTAuthorizer`) → Request Interceptor reads
  the identity and injects the per-tenant downstream key → the `whoami` tool
  reports the real end-user id and that a credential was injected, **while the
  agent never holds the key**.

### Build & deploy notes (learned the hard way)

- **Runtime image must be ARM64 and built via CodeBuild.** A QEMU cross-build on
  an x86 host produces an image that fails to start on Graviton (500 with no
  logs). The runtime is built + deployed with the AgentCore CLI
  (`agentcore configure` / `agentcore deploy`), which runs CodeBuild in the
  cloud. The agent image includes `aws-opentelemetry-distro` and runs under
  `opentelemetry-instrument` (required for AgentCore log/trace collection).
- **Runtime container logs**:
  `aws logs tail /aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT --since 15m`.
- **WebSocket endpoint** is `/ws` on **port 8080** (same app as `/ping` +
  `/invocations`), matching the AgentCore SDK contract — not a separate port.
- **Gateway auth**: send the Cognito **access token** (has the `client_id`
  claim that `allowedClients` validates); an ID token 403s with
  `insufficient_scope`.
- **Errors from the agent** are returned as HTTP 200 `{error: ...}` — AgentCore
  wraps non-2xx as `RuntimeClientError` and drops the body.

### Known limitation

- **No conversation memory yet.** Each message is handled statelessly (the agent
  is hand-written on Bedrock `converse`), so it does not remember prior turns.
  A `lark_agent_agent_mem` AgentCore Memory resource exists but is not wired in.
  Planned: refactor the agent onto the Strands framework + AgentCore Memory for
  cross-connection, cross-entrypoint session continuity.
