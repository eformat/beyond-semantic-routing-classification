#!/usr/bin/env bash
# Smoke test for the beyond-semantic-routing PoC, phase 0.
#
# Verifies MaaS BODY-BASED routing end to end:
#   1. DIRECT: the verified MaaS curl (single /v1 endpoint, model = full
#      internal path) — proves the cluster-side contract before the gateway
#      is in the path.
#   2. VIA PRAXIS: the same request through the praxis Route (/chat/completions),
#      where the llm_d_sc filter classifies the prompt, publishes
#      llm_d_sc.model metadata, and the json_body filter rewrites /model to the
#      full internal path for the winning label.
#   3. DECISIONS: the praxis pod's access-log records for the requests — each
#      carries metadata.llm_d_sc.{status,label,model,score,cluster,latency_us}
#      (one JSON record per request; the primary live observability).
#
# Required env:
#   MAAS_API_KEY   a MaaS API key for the prelude-maas models
#   PRAXIS_ROUTE   the praxis Route host, e.g. beyond-routing-praxis.apps.ocp.cloud.rhai-tmm.dev
#
# Optional env:
#   PRAXIS_NAMESPACE  (default beyond-routing)
#   MAAS_BASE_URL     (default https://maas.apps.ocp.cloud.rhai-tmm.dev/v1)

set -euo pipefail

MAAS_API_KEY="${MAAS_API_KEY:?set MAAS_API_KEY}"
PRAXIS_ROUTE="${PRAXIS_ROUTE:?set PRAXIS_ROUTE (the praxis Route host)}"
PRAXIS_NAMESPACE="${PRAXIS_NAMESPACE:-beyond-routing}"
MAAS_BASE_URL="${MAAS_BASE_URL:-https://maas.apps.ocp.cloud.rhai-tmm.dev/v1}"

pass=0
fail=0

check() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"
    pass=$((pass + 1))
  else
    echo "  FAIL: $2"
    fail=$((fail + 1))
  fi
}

chat() {
  # $1 base URL, $2 path, $3 model, $4 prompt
  curl -sS --max-time 120 -H 'Authorization: Bearer '"$MAAS_API_KEY" "$1$2" \
    -H "Content-Type: application/json" \
    -d '{
          "model": "'"$3"'",
          "max_tokens": 100,
          "messages": [
          {
            "role": "user",
            "content": "'"$4"'"
          }],
          "stream": false,
          "chat_template_kwargs": {"enable_thinking": false}
        }'
}

echo "=== 1. DIRECT: MaaS body-based routing (verified baseline) ==="
DIRECT_MODEL="publishers/prelude-maas/models/glm-53-flash"
RESP=$(chat "$MAAS_BASE_URL" "/chat/completions" "$DIRECT_MODEL" "Why is the sky blue?")
echo "$RESP" | jq -r '.model // .error.message // "NO MODEL FIELD"'
[ "$(echo "$RESP" | jq -r '.model // empty')" != "" ]
check $? "direct MaaS chat/completions with publishers/... model path returns a response"

echo
echo "=== 2. VIA PRAXIS: body rewritten by the gateway ==="
# The client sends ANY model value (or none) — the gateway classifies the prompt
# and rewrites /model to the full internal path for the winning label.
RESP=$(chat "https://$PRAXIS_ROUTE" "/chat/completions" "some-model" "Why is the sky blue?")
echo "$RESP" | jq -r '.model // .error.message // "NO MODEL FIELD"'
[ "$(echo "$RESP" | jq -r '.model // empty')" != "" ]
check $? "praxis /chat/completions classifies and routes to a real MaaS model"

echo
echo "=== 3. DECISIONS: access-log records carry the routing decision ==="
echo "Last access-log records from the praxis pod (metadata.* fields):"
oc logs -n "$PRAXIS_NAMESPACE" -l app.kubernetes.io/name=praxis --tail=200 2>/dev/null \
  | grep '"access"' | tail -4 \
  | jq -r '{path, status,
            label:  ."metadata.llm_d_sc.label",
            model:  ."metadata.llm_d_sc.model",
            score:  ."metadata.llm_d_sc.score",
            classify_us: ."metadata.llm_d_sc.latency_us"}' 2>/dev/null \
  || oc logs -n "$PRAXIS_NAMESPACE" -l app.kubernetes.io/name=praxis --tail=50 | tail -10
oc logs -n "$PRAXIS_NAMESPACE" -l app.kubernetes.io/name=praxis --tail=200 2>/dev/null \
  | grep '"access"' | grep -q 'metadata.llm_d_sc.model'
check $? "access-log records contain llm_d_sc decision metadata"

echo
echo "=== smoke test: $pass passed, $fail failed ==="
[ "$fail" = "0" ]
