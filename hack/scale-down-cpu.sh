#!/usr/bin/env bash
# Scale down the remaining CPU deployments of this repo's PoC — the complement
# of scale-down-gpu.sh: praxis (the gateway), llm-d-sc (the classifier) and
# the collector (the live dashboard).
#
# SCALED TO 0 (reversible; the objects, services, routes and configmaps stay —
# only the pods go). NOTE: the collector's dashboard URL goes dark with it —
# that is the point; the routing stats return on restore.
#
# Safety: only resources labelled app.kubernetes.io/part-of=beyond-semantic-routing
# are touched — never other projects' workloads sharing the namespace. A
# deployment counts as CPU here iff its pod template requests NO nvidia.com/*
# resource (the GPU ones are scale-down-gpu.sh's business).
#
# Usage: KUBECONFIG is exported by the Makefile; bare run:
#          bash hack/scale-down-cpu.sh [namespace]   (default user-mhepburn)
# Restore:
#          oc -n <ns> scale deploy/praxis deploy/llm-d-sc deploy/collector --replicas=1
#        or make deploy (also re-creates the LLMInferenceServices — the full stack)
set -euo pipefail

NAMESPACE="${1:-user-mhepburn}"
PART_OF="app.kubernetes.io/part-of=beyond-semantic-routing"

echo "namespace: $NAMESPACE"

echo
echo "=== CPU deployments (scaled to 0; the objects and services stay) ==="
for d in $(oc -n "$NAMESPACE" get deployments -o name -l "$PART_OF" 2>/dev/null); do
  reqs=$(oc -n "$NAMESPACE" get "$d" -o jsonpath='{.spec.template.spec.containers[*].resources.requests}')
  if echo "$reqs" | grep -q "nvidia.com"; then
    echo "skipping $d (GPU — scale-down-gpu.sh's business)"
    continue
  fi
  echo "scaling $d to 0"
  oc -n "$NAMESPACE" scale "$d" --replicas=0
done

echo
echo "=== verify: pods remaining under this repo's label ==="
sleep 5
oc -n "$NAMESPACE" get pods -l "$PART_OF" -o json 2>/dev/null | python3 -c "
import json, sys
pods = json.load(sys.stdin)['items']
left = [f\"{p['metadata']['name']}  ({p['status'].get('phase', '?')})\" for p in pods]
print('\n'.join(left) if left else 'none — the PoC is fully down')
"

echo
echo "Restore:"
echo "  oc -n $NAMESPACE scale deploy/praxis deploy/llm-d-sc deploy/collector --replicas=1"
echo "  (or make deploy — re-creates the LLMInferenceServices too, the full stack)"
