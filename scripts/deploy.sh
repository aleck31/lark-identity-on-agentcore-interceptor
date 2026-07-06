#!/usr/bin/env bash
# Deploy lark-agent end to end. Always uses the `lab` profile / us-west-2.
#
# Phases (idempotent — re-runnable):
#   1. CDK base stacks (security, agentcore, router, webui, gateway, observability)
#   2. Create/update the AgentCore Runtime from the built image  (control-plane CLI)
#   3. Create/update the MCP Gateway + interceptor + demo target  (control-plane CLI)
#   4. Re-deploy CDK so lambdas get the real runtime ARN; inject SPA config; upload SPA
#
# Usage: scripts/deploy.sh [--phase1|--runtime|--gateway|--frontend]
set -euo pipefail

PROFILE="${PROFILE:-lab}"
REGION="${REGION:-us-west-2}"
PREFIX="lark-agent"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION" UV_LINK_MODE=copy
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION"
CDK="npx --yes aws-cdk@2"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

cfn_out() { # stack, output-key
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null
}

ctx_set() { # key value  — persist an id back into cdk.json context
  uv run python - "$1" "$2" <<'PY'
import json, sys
k, v = sys.argv[1], sys.argv[2]
with open("cdk.json") as f: d = json.load(f)
d["context"][k] = v
with open("cdk.json", "w") as f: json.dump(d, f, indent=2); f.write("\n")
print(f"cdk.json: {k} = {v}")
PY
}

phase1_cdk_base() {
  log "Phase 1 — CDK base stacks"
  $CDK deploy "$PREFIX-security" "$PREFIX-agentcore" "$PREFIX-router" \
             "$PREFIX-gateway" "$PREFIX-observability" \
             --require-approval never --outputs-file cdk.out/outputs.json
}

phase2_runtime() {
  log "Phase 2 — AgentCore Runtime"
  local image role
  image="$(cfn_out "$PREFIX-agentcore" AgentImageUri)"
  role="$(cfn_out "$PREFIX-agentcore" ExecutionRoleArn)"
  [ -n "$image" ] && [ -n "$role" ] || { echo "missing image/role outputs"; exit 1; }

  local model existing
  model="$(uv run python -c "import json;print(json.load(open('cdk.json'))['context']['default_model_id'])")"
  existing="$(aws bedrock-agentcore-control list-agent-runtimes \
    --query "agentRuntimes[?agentRuntimeName=='${PREFIX//-/_}_agent'].agentRuntimeId" --output text 2>/dev/null || true)"

  local env_json
  env_json="$(cat <<JSON
{
  "BEDROCK_MODEL_ID": "$model",
  "COGNITO_USER_POOL_ID": "$(cfn_out "$PREFIX-security" UserPoolId)",
  "COGNITO_CLIENT_ID": "$(cfn_out "$PREFIX-security" UserPoolClientId)",
  "COGNITO_PASSWORD_SECRET_ID": "$PREFIX/cognito-password-secret",
  "GATEWAY_URL": "$(uv run python -c "import json;print(json.load(open('cdk.json'))['context'].get('gateway_url',''))")"
}
JSON
)"

  if [ -n "$existing" ] && [ "$existing" != "None" ]; then
    log "updating runtime $existing"
    aws bedrock-agentcore-control update-agent-runtime \
      --agent-runtime-id "$existing" \
      --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$image\"}}" \
      --role-arn "$role" \
      --network-configuration '{"networkMode":"PUBLIC"}' \
      --environment-variables "$env_json" >/dev/null
    ctx_set runtime_id "$existing"
  else
    log "creating runtime"
    local rid
    rid="$(aws bedrock-agentcore-control create-agent-runtime \
      --agent-runtime-name "${PREFIX//-/_}_agent" \
      --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$image\"}}" \
      --role-arn "$role" \
      --network-configuration '{"networkMode":"PUBLIC"}' \
      --protocol-configuration '{"serverProtocol":"HTTP"}' \
      --environment-variables "$env_json" \
      --query agentRuntimeId --output text)"
    ctx_set runtime_id "$rid"
  fi
}

phase3_gateway() {
  log "Phase 3 — MCP Gateway + interceptor + demo target"
  local issuer client interceptor tool grole gid
  issuer="$(cfn_out "$PREFIX-security" CognitoIssuerUrl)"
  client="$(cfn_out "$PREFIX-security" UserPoolClientId)"
  interceptor="$(cfn_out "$PREFIX-gateway" InterceptorFnArn)"
  tool="$(cfn_out "$PREFIX-gateway" ToolFnArn)"
  grole="$(cfn_out "$PREFIX-gateway" GatewayRoleArn)"

  gid="$(aws bedrock-agentcore-control list-gateways \
    --query "items[?name=='${PREFIX//-/_}_gw'].gatewayId" --output text 2>/dev/null || true)"

  if [ -z "$gid" ] || [ "$gid" = "None" ]; then
    log "creating gateway"
    gid="$(aws bedrock-agentcore-control create-gateway \
      --name "${PREFIX//-/_}_gw" \
      --protocol-type MCP \
      --role-arn "$grole" \
      --authorizer-type CUSTOM_JWT \
      --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"$issuer/.well-known/openid-configuration\",\"allowedClients\":[\"$client\"]}}" \
      --interceptor-configurations "[{\"interceptor\":{\"lambda\":{\"arn\":\"$interceptor\"}},\"interceptionPoints\":[\"REQUEST\"],\"inputConfiguration\":{\"passRequestHeaders\":true}}]" \
      --query gatewayId --output text)"
  fi
  ctx_set gateway_id "$gid"

  local gurl
  gurl="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$gid" \
    --query gatewayUrl --output text 2>/dev/null || true)"
  [ -n "$gurl" ] && ctx_set gateway_url "$gurl"

  # demo tool target (idempotent create)
  if ! aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$gid" \
       --query "items[?name=='demo_whoami']" --output text 2>/dev/null | grep -q demo_whoami; then
    log "creating demo tool target"
    aws bedrock-agentcore-control create-gateway-target \
      --gateway-identifier "$gid" \
      --name demo_whoami \
      --target-configuration "{\"mcp\":{\"lambda\":{\"lambdaArn\":\"$tool\",\"toolSchema\":{\"inlinePayload\":[{\"name\":\"whoami\",\"description\":\"Report the calling end-user identity injected by the gateway\",\"inputSchema\":{\"type\":\"object\",\"properties\":{}}}]}}}" \
      >/dev/null || echo "(target create returned non-zero — may already exist)"
  fi
}

phase4_frontend() {
  log "Phase 4 — re-deploy dependent stacks + publish SPA"
  # webui depends on runtime ARN (from runtime_id) — deploy now that it's set
  $CDK deploy "$PREFIX-webui" --require-approval never --outputs-file cdk.out/outputs.json

  local api_base app_id bucket dist
  api_base="$(cfn_out "$PREFIX-webui" ApiUrl)"
  bucket="$(cfn_out "$PREFIX-webui" SiteBucketName)"
  app_id="$(aws secretsmanager get-secret-value --secret-id "$PREFIX/channels/lark" \
    --query SecretString --output text 2>/dev/null | uv run python -c "import json,sys;print(json.load(sys.stdin).get('appId',''))" 2>/dev/null || echo "")"

  log "injecting SPA config (apiBase=$api_base)"
  cat > web-ui/config.js <<JS
window.LARK_AGENT_CONFIG = {
  apiBase: "${api_base%/}",
  larkAppId: "$app_id",
};
JS

  log "uploading SPA to s3://$bucket"
  aws s3 sync web-ui/ "s3://$bucket/" --delete \
    --exclude "*.md" --cache-control "no-cache"

  log "Done. Site: $(cfn_out "$PREFIX-webui" SiteUrl)"
  log "Webhook URL (register in Lark): $(cfn_out "$PREFIX-router" WebhookLarkUrl)"
}

case "${1:-all}" in
  --phase1)   phase1_cdk_base ;;
  --runtime)  phase2_runtime ;;
  --gateway)  phase3_gateway ;;
  --frontend) phase4_frontend ;;
  all|"")     phase1_cdk_base; phase2_runtime; phase3_gateway; phase4_frontend ;;
  *) echo "usage: $0 [--phase1|--runtime|--gateway|--frontend]"; exit 1 ;;
esac
