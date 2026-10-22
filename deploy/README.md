# Deploy: beyond-routing on OpenShift (TMM)

Kubernetes/OpenShift manifests for the `beyond-semantic-routing` PoC: smart
multi-turn LLM routing through a Praxis proxy that classifies every request
and routes it to the right model on the shared TMM MaaS gateway
(`maas.apps.ocp.cloud.rhai-tmm.dev`).

**Deploy target: the `user-mhepburn` namespace** (kustomization namespace
transformer; `KUBECONFIG` baked to `~/.kube/config.ibm-tmm` in the Makefile).
Images: `quay.io/eformat/praxis-beyond-routing`, `quay.io/eformat/llm-d-sc`,
`quay.io/eformat/llm-d-sc-modelcar`.

## Topology

```
laptop / OpenCode
   │  https://praxis-beyond-routing.apps.ocp.cloud.rhai-tmm.dev
   ▼
Route (edge TLS) → Service :8080 → praxis pod (beyond-routing ns)
                                     │  filter chain (below)
                                     │  classify gRPC :50051
                                     ├──→ llm-d-sc pod (classifier)
                                     │  https :443, model rewritten in body
                                     ▼
                    MaaS gateway (maas.apps.ocp.cloud.rhai-tmm.dev)
                                     └──→ prelude-maas ISVC-backed models
```

All resources live in namespace `beyond-routing` (the blast radius; the whole
thing is removable with `oc delete namespace beyond-routing`).

## The body-routing chain

`deploy/config/praxis-config.yaml` is the single source of truth (the same
file is validated inside the image build by `praxis --validate`). One chain,
one endpoint, model selection by **rewriting the request body**:

| # | Filter | What it does |
|---|--------|--------------|
| 1 | `request_id` | Sets `x-request-id`, reused as the classify `request_id` for log correlation |
| 2 | `access_log` | Records the `llm_d_sc.*` metadata per request (primary live observability) |
| 3 | `llm_d_sc` | Classifies the prompt via the llm-d-sc gRPC service **in the body phase** (see below); publishes the label + resolved model as filter_metadata; **splices the resolved model path into the body's `model` field in place** (ReadWrite) |
| 4 | `json_body` | Belt-and-braces: replaces the body's `/model` with the filter_metadata key `llm_d_sc.model` (idempotent after #3) |
| 5 | `credential_injection` | Injects `Authorization: Bearer <MAAS_API_KEY>` on requests routed to `maas`, reading the key from the `MAAS_API_KEY` env var (Secret-provided) |
| 6 | `load_balancer` | Forwards to the single cluster `maas` → `maas.apps.ocp.cloud.rhai-tmm.dev:443`, TLS, `base_path: /v1`, Host rewritten to the MaaS host |

Fail-open posture: if the classifier is unavailable (or the label metadata is
absent), the json_body replace is **skipped** and the client's original model
value passes through; traffic still reaches MaaS with the injected credential.

## Model rewrite map

Body model selection rewrites `/model` to the full internal MaaS path
(`publishers/prelude-maas/models/<name>` — the form the MaaS model-provider
resolver expects for ISVC-backed models; verified live with the glm-53-flash
curl below). The map lives in the beyond-routing filter (it publishes
`llm_d_sc.model` from the winning label):

| Label | Model | Full body `model` value |
|-------|-------|------------------------|
| `SIMPLE` | llama-32-3b | `publishers/prelude-maas/models/llama-32-3b` |
| `MEDIUM` | nemotron-cascade-2-30b | `publishers/prelude-maas/models/nemotron-cascade-2-30b` |
| `COMPLEX` | qwen38-27b | `publishers/prelude-maas/models/qwen38-27b` |
| `REASONING` | glm-53-flash | `publishers/prelude-maas/models/glm-53-flash` |
| (spare) | gemma4 | `publishers/prelude-maas/models/gemma4` |
| (spare) | llama-4-scout-17b-16e-w4a16 | `publishers/prelude-maas/models/llama-4-scout-17b-16e-w4a16` |

The two spare models are prelude-maas residents the policy can map later
(e.g. tool-heavy turns onto the scout MoE). Thinking-capable models
(qwen38-27b, glm-53-flash) also accept `chat_template_kwargs.enable_thinking`,
an in-place escalation lever that switches quality without switching models.

## LIMITATION: json_body cannot be conditioned on llm_d_sc results (v0.7.5)

> **This is the load-bearing design note.** The natural shape — one json_body
> filter per label, each gated by a condition of the form
> `when: {filter: llm_d_sc, key: label, result: SIMPLE}` — **is not
> expressible in praxis v0.7.5**. Verified against the v0.7.5 source, three
> separate mechanisms close it:
>
> 1. **Filter `conditions` (`when:`/`unless:`)** have no filter-results or
>    filter-metadata matcher at all. The supported predicates are `grpc`,
>    `path`, `path_prefix`, `methods`, `headers`, `headers_present` and
>    `selected_upstream` (docs/filters/README.md, Conditional Execution), and
>    `should_execute` reads the **original downstream request** only
>    (`crates/filter/src/condition/request.rs`). Headers queued by earlier
>    filters (`request_headers_to_set`, which is how llm_d_sc emits
>    `x-llm-d-sc-label`) are applied to the outgoing request only **after the
>    whole filter phase** (`crates/protocol/.../pipeline.rs`), so
>    `when: headers:` on a later filter cannot see them either.
> 2. **`branch_chains` (`on_result`)** can read filter results, but a branch
>    only ever sees its **host filter's own** results: "a condition naming a
>    different filter could never match and is rejected at startup"
>    (docs/filters/branch-chains.md). A branch attached to a json_body filter
>    cannot reference llm_d_sc's results.
> 3. **Body-transforming filters are rejected inside branch chains outright**:
>    "body-transforming filters must be in the main pipeline path. This is
>    enforced at build time: a filter that declares body access inside a
>    branch chain is rejected as a pipeline ordering error"
>    (docs/filters/branch-chains.md, Body hooks).
>
> **The shipped mechanism (v0.2.0 filter)**: the beyond-routing filter
> declares `BodyAccess::ReadWrite` and does EVERYTHING in the body phase:
> classify (the prompt is in the body hook's EOS payload), decide, splice the
> resolved model path into the body's `model` field in place, and publish the
> `llm_d_sc.model` metadata. The decision travels to `on_request` via
> `filter_state` (which survives the pre-read copy-back) for the provenance
> headers — headers queued in the body phase are dropped by the copy-back.
> The in-chain `json_body` filter below is belt-and-braces: it now sees the
> metadata (set by the body phase, which runs before later body hooks in
> pipeline order) and applies the same splice idempotently.
>
> **AND the metadata-sourced json_body replace has a phase-ordering trap**:
> praxis runs the body hooks in the **pre-read pass** (at end-of-stream),
> BEFORE the main request phase where `on_request` runs
> (`crates/protocol/.../stream_buffer.rs` `pre_read_body` → then
> `run_pipeline`). A json_body replace sourced from `llm_d_sc.model` resolves
> its payload during the pre-read — **before any on_request classification
> ran** — so a replace fed from metadata set in `on_request` is skipped and
> the client's original model passes through. This was verified live on TMM:
> the access log showed the classification metadata, yet MaaS returned 404
> for the unrewritten client model. The in-filter splice (body phase) is the
> fix; the in-chain json_body filter still works because the body phase sets
> the metadata BEFORE later body hooks run in pipeline order.

## Open live verifications

1. **One MaaS key across all prelude-maas models vs per-model keys.** The
   config assumes one key works for every model in the map. If MaaS scopes
   keys per model/subscription, the fallback is **N clusters sharing the same
   `/v1` URL**, differing only in `credential_injection` credentials —
   `clusters: [maas-llama32, maas-qwen38, ...]` all pointing at
   `maas.apps.ocp.cloud.rhai-tmm.dev:443` with one credential each, and
   llm_d_sc's label→cluster map selecting between them (its existing
   cluster-selection validates exactly that shape).
2. **Per-model tool-calling support.** Unverified per model. The scout MoE is
   the likely tool-calling candidate; tool-heavy turns must not be routed to a
   model that drops tool calls. Verify with one tool-call curl per model
   before enabling the policy's tool-loop escalation.

## Smoke test plan

Baseline (verified direct against MaaS, replicate through praxis):

```sh
# 0. Probes, from inside the cluster
oc -n beyond-routing exec deploy/praxis -- \
  curl -s http://localhost:9901/healthy   # 200
oc -n beyond-routing exec deploy/praxis -- \
  curl -s http://localhost:9901/ready     # 200 (no health_check blocks configured)

# 1. OBSERVE first: with the classifier unavailable (scale llm-d-sc to 0), the
#    llm_d_sc filter fails open to default_cluster and publishes NO label
#    metadata, so json_body does NOT rewrite. This proves the full proxy path
#    (TLS, Host rewrite, /v1 base_path, credential injection) with the
#    client-supplied model value — no routing behaviour yet.
# 2. ENFORCE: classifier back up; verify the label metadata lands in the
#    access log (request_id → llm_d_sc.label) and that the response's `model`
#    field is the rewritten full path once `llm_d_sc.model` is published.

ROUTE=$(oc -n beyond-routing get route praxis -o jsonpath='{.spec.host}')
KEY=$(oc -n beyond-routing get secret maas-api-key -o jsonpath='{.data.key}' | base64 -d)

curl -sS "https://$ROUTE/chat/completions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "publishers/prelude-maas/models/glm-53-flash",
    "messages": [{"role": "user", "content": "Reply with one word: ok"}],
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

Checks in order: HTTP 200; `response.model` is the full rewritten path; the
praxis pod's access-log line carries the matching `llm_d_sc.*` metadata; an
invalid key returns 401 (judge by status — the MaaS 401 body is empty). Note
the client calls `/chat/completions` on the praxis Route: `base_path: /v1`
prepends the prefix on the upstream leg only, so a client already sending
`/v1/...` would get a doubled prefix at MaaS. For OpenCode's custom provider,
set `baseURL` to the bare Route host.

## Build and deploy

The iterate loop is **local podman → quay → oc apply** (see the Makefile):

```sh
# 1. Build + push the praxis image (praxis v0.7.5 cloned inside the Containerfile):
make image image-push          # -> quay.io/eformat/praxis-beyond-routing:latest

# 2. Build + push the llm-d-sc classifier + ModelCar (from its own repo):
make llmd-sc-image llmd-sc-image-push llmd-sc-modelcar llmd-sc-modelcar-push

# 3. Secret (imperative — never in git):
make secret MAAS_API_KEY=sk-oai-...

# 4. Apply everything into user-mhepburn (KUBECONFIG config.ibm-tmm baked in):
make deploy
make status                    # rollout status + resolved Route
make smoke                     # direct-MaaS baseline, via-praxis, access-log decisions

# 5. Tear down (label-selected resources ONLY — never the namespace):
make undeploy
```

`KUBECONFIG` is unconditionally set to `~/.kube/config.ibm-tmm` in the Makefile (an ambient
KUBECONFIG must never silently point the deploy at a different cluster); `NAMESPACE` is
`user-mhepburn` via the kustomization namespace transformer, and `00-namespace.yaml` is
deliberately excluded from `deploy/kustomization.yaml` resources so no Namespace object is ever
applied into the pre-existing sandbox.

In-cluster BuildConfig alternative (kept for the GitOps wiring):

```sh
# Build the image in-cluster (context staged from this repo; praxis is
# cloned at tag v0.7.5 inside the Containerfile):
CTX="$(hack/stage-build-context.sh)"
oc start-build praxis-beyond-routing --from-dir="$CTX" --follow
```

## GitOps discipline

**These manifests are staging copies.** The TMM cluster
(`rhai-tmm.dev`) is ACM + ArgoCD GitOps controlled from the
`rhoai-cluster-pool` repo — never patch live cluster resources directly with
`oc`. Real changes go: edit the GitOps source in rhoai-cluster-pool → Mike
commits + pushes → ArgoCD/ACM syncs (e.g. `make sync` / `make sync-app` in
rhoai-nightly, or annotate the Application with
`argocd.argoproj.io/refresh=hard`). Use the commands above only against a
scratch/dev namespace for the PoC's own bring-up, then port the manifests into
the GitOps repo.

## Files

```
deploy/
  00-namespace.yaml              Namespace beyond-routing
  10-imagestream.yaml            ImageStream praxis-beyond-routing (BuildConfig output)
  15-llm-d-sc-deployment.yaml    llm-d-sc classifier (ModelCar initContainer, gRPC :50051)
  16-llm-d-sc-service.yaml       ClusterIP :50051 (grpc)
  20-buildconfig.yaml            Docker-strategy binary build (praxis + filter)
  30-deployment.yaml             praxis (initContainer render, admin-port probes)
  40-service.yaml                ClusterIP :8080 (data plane only; admin not published)
  50-route.yaml                  edge TLS
  secret.example.yaml            maas-api-key SHAPE only — placeholder, never real
  config/praxis-config.yaml      THE body-routing chain (source of truth + build gate)
  runtime/render-config.sh       initContainer render (verbatim copy for this config)
  kustomization.yaml             ties it together (disableNameSuffixHash, Secret excluded)
Containerfile.praxis             multi-stage build; clones praxis v0.7.5, wires the
                                 filter crate, [patch] unification, --validate gate
hack/stage-build-context.sh      assembles the binary-build context
```
