// Runtime config — overwritten at deploy time by scripts/deploy.sh with the
// real API base URL and Lark appId. Committed with placeholders so the SPA
// loads locally.
window.LARK_AGENT_CONFIG = {
  apiBase: "REPLACE_API_BASE",   // e.g. https://abc.execute-api.us-west-2.amazonaws.com
  larkAppId: "REPLACE_LARK_APP_ID",
};
