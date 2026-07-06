#!/usr/bin/env bash
# Store Lark app credentials in Secrets Manager and print the webhook URL to
# register in the Lark developer console. Uses the `lab` profile / us-west-2.
#
# Lark console setup (do this first):
#   1. Create a self-built app; add "Bot" + "Web app" capabilities.
#   2. Permissions: im:message, im:message:send_as_bot, im:message.content:readonly,
#      im:chat:readonly, im:resource, contact:user.base:readonly
#   3. Event subscription: subscribe im.message.receive_v1; set the Request URL
#      to the webhook URL printed below; choose "Encrypt" and note the Encrypt Key.
#   4. Web app: register the SPA URL (printed by deploy.sh) as a redirect/safe domain.
#   5. Publish the app.
set -euo pipefail

PROFILE="${PROFILE:-lab}"
REGION="${REGION:-us-west-2}"
PREFIX="lark-agent"
export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION"
TABLE="$PREFIX-identity"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

WEBHOOK="$(aws cloudformation describe-stacks --stack-name "$PREFIX-router" \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookLarkUrl'].OutputValue" --output text 2>/dev/null || true)"
SITE="$(aws cloudformation describe-stacks --stack-name "$PREFIX-webui" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue" --output text 2>/dev/null || true)"

log "Register these in the Lark developer console:"
echo "  Event Request URL : ${WEBHOOK:-<deploy router first>}"
echo "  Web app URL       : ${SITE:-<deploy webui first>}"
echo

read -r -p "App ID (cli_...): " APP_ID
read -r -p "App Secret: " APP_SECRET
read -r -p "Verification Token: " VTOKEN
read -r -p "Encrypt Key: " EKEY

log "Storing credentials in Secrets Manager ($PREFIX/channels/lark)"
aws secretsmanager put-secret-value \
  --secret-id "$PREFIX/channels/lark" \
  --secret-string "$(printf '{"appId":"%s","appSecret":"%s","verificationToken":"%s","encryptKey":"%s"}' \
      "$APP_ID" "$APP_SECRET" "$VTOKEN" "$EKEY")" >/dev/null
echo "  stored."

echo
read -r -p "Your Lark open_id to allowlist (ou_..., blank to skip): " OPEN_ID
if [ -n "$OPEN_ID" ]; then
  aws dynamodb put-item --table-name "$TABLE" --item "$(cat <<JSON
{"PK":{"S":"ALLOW#lark:$OPEN_ID"},"SK":{"S":"ALLOW"},"channelKey":{"S":"lark:$OPEN_ID"}}
JSON
)"
  echo "  allowlisted lark:$OPEN_ID"
fi

log "Setup complete. Re-run scripts/deploy.sh --frontend to inject the appId into the SPA."
