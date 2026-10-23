#!/usr/bin/env bash
# Replay the SAME multi-turn conversation fixture through all four phase-2
# substrate arms, for the A/B/C/D comparison.
#
#   :8080  arm A — native vLLM prefix caching (kvcached off)
#   :8081  arm B — kvcached, --no-enable-prefix-caching
#   :8082  arm C — kvcached + APC
#   :8083  arm D — kvcached + APC, both models on one shared slice
#
# The fixture is a 3-turn conversation with a shared long prefix (every turn
# resends the full history, as a chat client does) so the KV-cache semantics
# differ per arm: A/C should show prefix-cache HITS on turns 2-3, B shows the
# full re-prefill cost (no reuse by design).
#
# The same x-session-id is used on all four arms — the collector groups by
# (conversation fingerprint, arm), so the graphic renders the same conversation
# as four side-by-side lanes.
#
# Usage: hack/replay-arms.sh [namespace]   (KUBECONFIG inherited; praxis svc
#          ports are reached via oc port-forward from localhost)
set -euo pipefail

NAMESPACE="${1:-user-mhepburn}"
PF_DIR="$(mktemp -d /tmp/opencode/replay-arms.XXXXXX)"
SESSION="arm-replay-$(date +%s)"
PLOG="$PF_DIR/last-response.json"

cleanup() {
  [ -n "${PF_PID:-}" ] && kill "$PF_PID" 2>/dev/null || true
  rm -rf "$PF_DIR"
}
trap cleanup EXIT

echo "session id: $SESSION (collector lane key)"
echo "port-forwarding svc/praxis 8080/8081/8082/8083 ..."
oc -n "$NAMESPACE" port-forward svc/praxis 8080 8081 8082 8083 >"$PF_DIR/pf.log" 2>&1 &
PF_PID=$!
for i in $(seq 1 30); do
  grep -q "Forwarding from" "$PF_DIR/pf.log" 2>/dev/null && break
  sleep 0.5
done

# The shared context prefix (identical every turn — the cacheable block).
CONTEXT='You are a code-review assistant for a Rust HTTP proxy. Consider this filter implementation sketch: a request-body hook classifies the prompt via a gRPC classifier, splices the resolved model into the JSON body in place, and publishes filter metadata for later filters. Body hooks run in a pre-read pass at end-of-stream, BEFORE the main request phase where on_request runs, so any header queued inside the body hook is dropped by the pre-read copy-back and must instead travel via filter_state, which survives the copy-back. The body splice must be idempotent: an in-chain json_body filter repeats it from the published metadata. Answer questions about this design concisely.'

turn() {
  # $1 port, $2 turn number, $3 question
  local port="$1" n="$2" q="$3"
  local msgs="[{\"role\": \"system\", \"content\": \"$CONTEXT\"}, {\"role\": \"user\", \"content\": \"$q\"}]"
  # Later turns resend the same context (the client resends history); keep the
  # fixture simple: the system context IS the shared prefix every turn.
  # NOTE: the bare path — praxis's base_path: /v1 prepends the prefix on the
  # upstream leg, so a client /v1/... would double it.
  local body="{\"model\": \"some-model\", \"max_tokens\": 60, \"stream\": false,
    \"messages\": $msgs}"
  local t0 t1 resp
  t0=$(date +%s.%N)
  resp=$(curl -sS --max-time 120 "http://localhost:$port/chat/completions" \
    -H "Content-Type: application/json" \
    -H "x-session-id: $SESSION" \
    -d "$body") || resp='{"error":"request failed"}'
  t1=$(date +%s.%N)
  echo "$resp" >"$PLOG"
  local served dur
  served=$(echo "$resp" | jq -r '.model // .error.message // "NO MODEL FIELD"' 2>/dev/null || echo "?")
  dur=$(echo "$t1 $t0" | awk '{printf "%.1fs", $1-$2}')
  printf '  turn %s → %-16s (%s)\n' "$n" "$served" "$dur"
}

arm() {
  # $1 port, $2 label
  echo
  echo "=== arm $2 (port $1) ==="
  turn "$1" 1 "What is the core ordering trap in this design?"
  turn "$1" 2 "Why does the idempotent json_body repeat not double-apply the splice?"
  turn "$1" 3 "How should filter_state carry the decision to on_request?"
}

arm 8080 A
arm 8081 B
arm 8082 C
arm 8083 D

echo
echo "Replay complete. Session lane key: $SESSION"
echo "Next:"
echo "  - collector graphic (four side-by-side lanes for $SESSION)"
echo "  - kvcached signature in the b-*/c-* pods:"
echo "      oc -n $NAMESPACE logs -l app.kubernetes.io/name=b-qwen3-06b | grep -i kvcached | head"
echo "  - prefix-cache hits (arms A/C/D) vs zero hits (arm B):"
echo "      oc -n $NAMESPACE exec \$(oc -n $NAMESPACE get pod -l app.kubernetes.io/name=a-qwen3-06b,kserve.io/component=workload -o name | head -1) -- curl -sk https://localhost:8000/metrics | grep prefix_cache"
