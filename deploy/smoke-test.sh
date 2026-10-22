#!/usr/bin/env bash
# Smoke test for the beyond-semantic-routing PoC, phase 0.
#
# Verifies MaaS BODY-BASED routing end to end:
#   1. DIRECT: the verified MaaS curl (single /v1 endpoint, model = full
#      internal path) — proves the cluster-side contract before the gateway
#      is in the path.
#   2. VIA PRAXIS, per tier: the prompts are PRINTED so the decision is
#      obvious. A simple prompt must route to the SIMPLE tier model and a
#      complex prompt to the COMPLEX tier model — the gateway classifies the
#      prompt, and the filter splices the winning tier's full internal path
#      into the body's model field. The check is the ROUTED model (the
#      response's .model is what MaaS served), not just any response.
#   3. DECISIONS: the praxis pod's access-log records for the requests — each
#      carries metadata.llm_d_sc.{status,label,model,score,cluster,latency_us}
#      (one JSON record per request; the primary live observability). The
#      labels here (SIMPLE / COMPLEX) correspond to the prompts printed above.
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

# The tier map under test (deploy/config/praxis-config.yaml is the source of
# truth; these mirror the SIMPLE and COMPLEX rows for the assertions).
SIMPLE_MODEL="publishers/prelude-maas/models/llama-32-3b"
COMPLEX_MODEL="publishers/prelude-maas/models/qwen38-27b"

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
  # $1 base URL, $2 path, $3 model, $4 prompt — prints the response body.
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
echo "  prompt: \"Why is the sky blue?\"  →  direct to MaaS with model=$DIRECT_MODEL"
RESP=$(chat "$MAAS_BASE_URL" "/chat/completions" "$DIRECT_MODEL" "Why is the sky blue?")
echo "  served: $(echo "$RESP" | jq -r '.model // .error.message // "NO MODEL FIELD"')"
[ "$(echo "$RESP" | jq -r '.model // empty')" != "" ]
check $? "direct MaaS chat/completions with publishers/... model path returns a response"

echo
echo "=== 2. VIA PRAXIS: the prompt decides the model (tier map) ==="
echo "  The client sends model=some-model every time; the gateway classifies the"
echo "  prompt and splices the winning tier's model into the body."

echo
echo "  prompt: \"Why is the sky blue?\"  (trivially simple)"
RESP=$(chat "https://$PRAXIS_ROUTE" "/chat/completions" "some-model" "Why is the sky blue?")
SERVED=$(echo "$RESP" | jq -r '.model // empty')
echo "  served: ${SERVED:-NO MODEL FIELD}"
[ -n "$SERVED" ]
check $? "simple prompt gets a real completion through the gateway"
# MaaS echoes a SHORT model name in .model, not the full publisher path we
# spliced — compare the last path segment.
[ "$SERVED" = "${SIMPLE_MODEL##*/}" ]
check $? "simple prompt routes to the SIMPLE tier model (llama-32-3b)"

echo
echo "  prompt: \"Design a fault-tolerant multi-region payment platform...\" (multi-step reasoning)"
COMPLEX_PROMPT='Design a fault-tolerant multi-region payment platform: choose an event-sourcing vs CQRS split, reason through exactly-once semantics under network partitions, idempotency keys at every write path, leader election, and a reconciliation strategy for cross-ledger drift. Justify each trade-off.'
RESP=$(chat "https://$PRAXIS_ROUTE" "/chat/completions" "some-model" "$COMPLEX_PROMPT")
SERVED=$(echo "$RESP" | jq -r '.model // empty')
echo "  served: ${SERVED:-NO MODEL FIELD}"
[ -n "$SERVED" ]
check $? "complex prompt gets a real completion through the gateway"
[ "$SERVED" = "${COMPLEX_MODEL##*/}" ]
check $? "complex prompt routes to the COMPLEX tier model (qwen38-27b)"

echo
echo "=== 3. DECISIONS: access-log records carry the routing decision ==="
echo "Last access-log records from the praxis pod (metadata.* fields) —"
echo "labels correspond to the prompts printed above:"
# The access_log line renders with ANSI codes around the level, module and
# even the `record` key; the record JSON (first key `client_ip`) is the only
# clean span. Capture ONCE into a variable: a `grep -q` at the end of a pipe
# exits at the first match while `oc logs` is still streaming (200 ANSI-heavy
# lines far exceed the 64K pipe buffer), SIGPIPE-ing the upstream writer —
# pipeline exit 141 under pipefail, which set -e turns into a spurious abort.
# A captured variable has no writer left to SIGPIPE, and the check below runs
# in an if-condition so set -e never aborts on a no-match (FAIL must print).
RECORDS=$(oc logs -n "$PRAXIS_NAMESPACE" -l app.kubernetes.io/name=praxis --tail=200 2>/dev/null \
  | grep 'client_ip' | sed 's/^.*{"client_ip/{"client_ip/' || true)
if [ -n "$RECORDS" ]; then
  echo "$RECORDS" | tail -4 \
    | jq -r '{path, status,
              label:  ."metadata.llm_d_sc.label",
              model:  ."metadata.llm_d_sc.model",
              score:  ."metadata.llm_d_sc.score",
              classify_us: ."metadata.llm_d_sc.latency_us"}' 2>/dev/null || true
fi
if echo "$RECORDS" | grep -q 'llm_d_sc.model'; then
  check 0 "access-log records contain llm_d_sc decision metadata"
else
  check 1 "access-log records contain llm_d_sc decision metadata"
fi

echo
echo "=== smoke test: $pass passed, $fail failed ==="
[ "$fail" = "0" ]
