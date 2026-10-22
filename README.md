# beyond-semantic-routing-classification

A PoC that goes **beyond prompt classification**: choosing the right model for the next turn of a
multi-turn conversation without compromising the rest of the task — and determining where semantic
routing belongs in an existing inference stack.

## The stack

```
opencode / client (multi-turn, full messages[] each turn — no client changes)
   ▼
Praxis edge gateway (this repo's beyond-routing-filter crate)
   llm-d-sc signal (gRPC) + policy → model published as llm_d_sc.model metadata
   json_body rewrites /model → MaaS BODY-BASED routing at one endpoint
   ▼
MaaS (TMM: maas.apps.ocp.cloud.rhai-tmm.dev/v1) · later: sardeenz + kvcached tier
   ▼
Serving: prelude-maas models (llama-32-3b … qwen38-27b, glm-53-flash)
```

## Layout

```
router/    the beyond-routing-filter crate (Praxis HTTP filter, vendored from
           llm-d-sc-praxis-filter v0.1.0 and evolved; praxis git dep v0.7.5)
deploy/    OpenShift manifests for the TMM proving ground (namespace beyond-routing,
           llm-d-sc classifier, praxis BuildConfig/Deployment/Route, body-routing config)
docs/      architecture + findings (see the plan for the full phase structure)
eval/      benchmarks (later phases: model-routing-eval corpus + synthetic conversations)
demo/      live decisioning flow graphic (phase 1)
```

## Phase 0 (current): MaaS body-based routing on TMM

- **Models (live `oc get llmisvc` in prelude-maas, all READY):** llama-32-3b, gemma4,
  nemotron-cascade-2-30b, llama-4-scout-17b-16e-w4a16, qwen38-27b, glm-53-flash (chat) +
  bge-m3 (embedding, not a routing target).
- **Body-based routing** at the single MaaS endpoint `https://maas.apps.ocp.cloud.rhai-tmm.dev/v1`:
  the request body `model` field carries the full internal path
  (`publishers/prelude-maas/models/<name>`), rewritten by a praxis `json_body` filter from the
  `llm_d_sc.model` metadata the filter publishes per classification.
- **Starting tier map** (the tier × model matrix run measures the real table):
  SIMPLE → llama-32-3b · MEDIUM → nemotron-cascade-2-30b · COMPLEX → qwen38-27b ·
  REASONING → glm-53-flash · fail-open default → llama-32-3b.

## Build

```bash
cd router
cargo +1.96.0 test     # in-process stub server, decision table, config validation
```

The filter registers with any praxis server build via
`[package.metadata.praxis-filters]` (build-time auto-discovery — see `Containerfile.praxis`).

## Verify locally

See `deploy/README.md` for the smoke test plan (replicate the verified MaaS curl through praxis,
observe-first posture) and the two open live verifications (MaaS key scoping, per-model tool calling).
