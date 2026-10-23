#!/usr/bin/env bash
# Stop / scale down EVERY GPU-consuming workload that is part of this repo's
# deploy manifests — for when the experiment is done and the MIG capacity
# should go back to the pool.
#
# What goes down:
#   - the six a-/b-/c- LLMInferenceServices (deploy/90-phase2-arms.yaml) and
#     the phase-2 base pair (deploy/85-phase2-llmisvc.yaml): DELETED — kserve's
#     controller owns their deployments (ownerRef LLMInferenceService), so
#     scale-to-zero does not stick; deleting the CR tears down the deployments,
#     pods, services and routes and returns the MIG capacity.
#   - arm D's d-dual-vllm raw Deployment (deploy/97-arm-d.yaml): SCALED TO 0 —
#     reversible; the d-model-cache PVC stays so restore needs no model re-pull.
#
# What stays UP: the CPU decisioning path — praxis (the gateway), llm-d-sc
# (the classifier) and the collector (the live dashboard). The Arm comparison
# panel and the routing stats stay live; only inference stops.
#
# Safety: only resources labelled app.kubernetes.io/part-of=beyond-semantic-routing
# are touched — never other projects' workloads sharing the namespace. A
# deployment counts as GPU-consuming iff its pod template requests any
# nvidia.com/* resource — the enumeration is live, not a hardcoded list.
#
# Usage: KUBECONFIG is exported by the Makefile; bare run:
#          bash hack/scale-down-gpu.sh [namespace]   (default user-mhepburn)
# Restore:
#          make deploy                               # everything back (~2 min)
#          oc -n <ns> scale deploy/d-dual-vllm --replicas=1   # arm D alone
set -euo pipefail

NAMESPACE="${1:-user-mhepburn}"
PART_OF="app.kubernetes.io/part-of=beyond-semantic-routing"

echo "namespace: $NAMESPACE"

echo
echo "=== LLMInferenceServices (deleted; the kserve controller tears down the pods) ==="
ISVCS=$(oc -n "$NAMESPACE" get llminferenceservice -l "$PART_OF" -o name 2>/dev/null || true)
if [ -z "$ISVCS" ]; then
  echo "none — already down"
else
  echo "$ISVCS"
  # the CR delete finalizes through the kserve controller; tolerate a slow
  # finalizer rather than aborting (set -e) mid-teardown
  oc -n "$NAMESPACE" delete llminferenceservice -l "$PART_OF" \
    --ignore-not-found --timeout=180s \
    || echo "NOTE: deletion still finalizing — check 'oc get llminferenceservice' in a minute"
fi

echo
echo "=== raw GPU deployments (scaled to 0; the objects and PVCs stay) ==="
for d in $(oc -n "$NAMESPACE" get deployments -o name -l "$PART_OF" 2>/dev/null); do
  reqs=$(oc -n "$NAMESPACE" get "$d" -o jsonpath='{.spec.template.spec.containers[*].resources.requests}')
  if echo "$reqs" | grep -q "nvidia.com"; then
    gpu=$(echo "$reqs" | tr ' ' '\n' | grep nvidia.com | head -1)
    echo "scaling $d to 0  (GPU: $gpu)"
    oc -n "$NAMESPACE" scale "$d" --replicas=0
  fi
done

echo
echo "=== verify: GPU workloads remaining under this repo's label ==="
sleep 5
oc -n "$NAMESPACE" get pods -l "$PART_OF" -o json 2>/dev/null | python3 -c "
import json, sys
pods = json.load(sys.stdin)['items']
left = []
for p in pods:
    for c in p['spec'].get('containers') or []:
        r = (c.get('resources') or {}).get('requests') or {}
        if any('nvidia.com' in k for k in r):
            left.append(f\"{p['metadata']['name']}  ({p['status'].get('phase', '?')})\")
print('\n'.join(left) if left else 'none — all GPU workloads down')
"

echo
echo "=== still up (the CPU decisioning path + dashboard) ==="
oc -n "$NAMESPACE" get deployments -l "$PART_OF" --no-headers 2>/dev/null \
  | awk '{printf "  %-24s replicas=%s\n", $1, $2}'

echo
echo "Restore:"
echo "  make deploy                                              # everything back (~2 min)"
echo "  oc -n $NAMESPACE scale deploy/d-dual-vllm --replicas=1   # arm D alone"
