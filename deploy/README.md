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

## Phase 2: the three-arm substrate comparison (Arm A/B/C on the SAME engine)

### The arms (`deploy/85-phase2-llmisvc.yaml` reference + `deploy/90-phase2-arms.yaml` comparison fleet)

Phase 2 compares KV-cache SUBSTRATES, holding everything else constant: all
six comparison LLMISVCs run the SAME engine (vLLM 0.30 from the official
kvcached pairing `ghcr.io/ovg-project/kvcached-vllm:latest` — the thin
`Containerfile.kvcached` layer only adds OpenShift arbitrary-UID cache-dir
fixes), same models, same MIG profiles, same revision pins.

| Arm | LLMISVCs | Substrate | vLLM args / env |
|---|---|---|---|
| **A** (static per-slice cache) | `a-qwen3-06b` (mig-18gb), `a-qwen3-8b` (mig-35gb) | native vLLM prefix caching, kvcached OFF | `--enable-prefix-caching` |
| **B** (elastic, no reuse) | `b-qwen3-06b`, `b-qwen3-8b` | kvcached ON, APC OFF | `ENABLE_KVCACHED=true` `KVCACHED_AUTOPATCH=1` `--no-enable-prefix-caching`, NO `--gpu-memory-utilization` (kvcached manages memory) |
| **C** (elastic + bounded APC) | `c-qwen3-06b`, `c-qwen3-8b` | kvcached ON, APC ON | `ENABLE_KVCACHED=true` `KVCACHED_AUTOPATCH=1` `--enable-prefix-caching` |

Plus the RHOAI-production reference: the original `qwen3-06b`/`qwen3-8b`
LLMISVCs (`deploy/85-phase2-llmisvc.yaml`, rhaiis vLLM 0.13) stay live — the
as-deployed MaaS-tier baseline, NOT the comparison control.

kvcached notes (upstream-confirmed from ovg-project/kvcached):
- kvcached requires vLLM ≥ 0.17 — the rhaiis 0.13 image is below the minimum,
  hence the dedicated kvcached-vllm 0.30 image for all comparison arms.
- **The kvcached image BAKES `ENABLE_KVCACHED=true KVCACHED_AUTOPATCH=1
  KVCACHED_IPC_NAME=VLLM` as env defaults** — Arm A sets
  `ENABLE_KVCACHED=false` + `KVCACHED_AUTOPATCH=0` explicitly or it inherits
  kvcached and the comparison is confounded (k8s container env ADDS to image
  env; absence means inheritance). Image size 8.75 GB compressed (38 layers).
- Activation: `ENABLE_KVCACHED=true` + `KVCACHED_AUTOPATCH=1` → the
  `kvcached_autopatch.pth` hook patches vLLM at import (wrapt `when_imported`).
  Serve-time signature in the pod logs:
  `[kvcached][INFO] Applying 16 patches for vllm ... Successfully patched vllm`.
- `--no-enable-prefix-caching` is the documented disable; with kvcached on,
  DROP `--gpu-memory-utilization` — kvcached manages memory automatically.
- kvcached v0.1.6 SUPPORTS APC with a configurable memory bound (Arm C's
  differentiator; the bound is NOT a native vLLM feature).

**Cluster mechanics (all learned live):**

- KServe renders `--served-model-name` from the LLMInferenceService NAME —
  the arm LLMISVCs serve `a-qwen3-06b`/`b-qwen3-06b`/... (and the full
  `publishers/<ns>/models/<llmisvc-name>` path). The praxis `llm_d_sc` route
  maps must splice the arm-prefixed name into the body, NOT the bare
  `qwen3-06b` — a bare body model gets vLLM 404
  `{"error":"The model \`qwen3-8b\` does not exist."}`.
- praxis `base_path: /v1` prepends on the upstream leg — clients call the
  BARE `/chat/completions` on the arm ports; a client `/v1/chat/completions`
  doubles the prefix and FastAPI answers `{"detail":"Not Found"}`.
- LLMISVC pods land in the `default` Kueue clusterqueue by default, which has
  ZERO mig/gpu quota → SchedulingGated forever. The fix: the
  `kueue.x-k8s.io/queue-name: unreserved` label on the LLMISVC metadata PLUS
  the ODH annotations `opendatahub.io/hardware-profile-name` AND
  `opendatahub.io/hardware-profile-namespace: redhat-ods-applications` (the
  odh-model-controller webhook validates the pair and DENIES the apply
  otherwise). The `unreserved` clusterqueue admits against its cohort's free
  MIG capacity — the booking panel's unreserved slots, no reservation needed.
- KServe's controller propagates the queue label into the Deployment's pod
  template ONLY at Deployment creation — a later LLMISVC label edit does NOT
  roll it; delete the kserve Deployment so the controller recreates it.
- The KServe workload serves vLLM over **HTTPS** on :8000 with per-pod
  self-signed certs — the praxis cluster needs `tls.verify: false` +
  `insecure_options.allow_tls_no_verify`, and direct curls need an unverified
  context.
- The RHEL AI vLLM image does not know `--default-chat-template-kwargs`
  (newer-vLLM flag) — thinking stays client-controlled via
  `chat_template_kwargs` per request.
- The response's `usage.prompt_tokens_details.cached_tokens` stays null in
  this vLLM build even on hits — the ENGINE's
  `vllm:prefix_cache_{queries,hits}_total` metrics on the workload's :8000
  are the authoritative cache-hit measure.

### (Retired) sardeenz Arm B

The original Arm B — one sardeenz pod on a 3g.71gb slice with both models
resident (`hack/deploy-sardeenz.sh` / `hack/patch-sardeenz.py`, kept as
reference) — is RETIRED: sardeenz cannot detect GPUs on a MIG-partitioned
node (the parent GPU's NVML `device.getStatus()` returns code 4
NOT_SUPPORTED under MIG, and `@rh-ai-bu/ts-nvml` exposes no MIG device API).
The kvcached MECHANISM lives on in the comparison fleet above.

### The triple-listener phase-2 config (`deploy/config/praxis-config-phase2.yaml`)

ONE praxis, one port per substrate arm:

```
              ┌─ listener :8080 ─ chain A ─→ a-qwen3-06b, a-qwen3-8b   (native APC, static)
prompt ───────┤  listener :8081 ─ chain B ─→ b-qwen3-06b, b-qwen3-8b   (kvcached, no APC)
              └─ listener :8082 ─ chain C ─→ c-qwen3-06b, c-qwen3-8b   (kvcached + APC)
```

- Cluster names carry the arm prefix (`a-`/`b-`/`c-`), so the access log's
  `cluster` field shows which arm served each turn; each arm's chain routes
  per label: SIMPLE/MEDIUM → the arm's 0.6b, COMPLEX → the arm's 8b, and
  REASONING → **the MaaS gateway's glm-5.3-flash** (`<arm>-maas-reasoning`
  clusters, all pointing at the same MaaS endpoint — identical across arms,
  so per-arm deltas on REASONING turns are the measurement noise floor). The
  arm prefix on the MaaS cluster names keeps the collector's lane grouping
  intact (parse_session derives the arm suffix from the cluster-name prefix).
- The SAME conversation replayed through all three ports is the comparison;
  the substrate is the only difference
- praxis /ready goes 503 while ANY listener's clusters have zero healthy
  endpoints — deploy the LLMISVCs first, roll praxis after

The collector groups by (conversation fingerprint, arm): the same conversation
through the three arms renders as three side-by-side lanes in the flow
graphic (arm badges + per-arm colors).



`collector/` is a zero-dependency Python service (stdlib only, ubi-minimal +
python3 image) deployed as `collector` in the same namespace (60/65/70/75
manifests). It turns the routing chain's per-request decisions into live
state:

- **Ingestion**: follows the praxis pod's access_log through the in-cluster
  K8s API (`pods/log get` via the collector ServiceAccount — deliberately the
  only RBAC the observer needs), parsing each record's decision (the record
  JSON is anchored on its first key `client_ip`; the rest of the line is
  ANSI-decorated). A CloudEvents receiver (`POST /events`) is built in for
  the later flip: when the praxis image gains the off-by-default
  `cloud-events-filter` feature and the config adds a `cloud_events` filter
  (`on: response_complete`, `metadata_fields: [llm_d_sc.*]`,
  `destination: http://collector:8080/events`), that POST feed becomes the
  primary input and the log tail retires (`COLLECTOR_LOG_FOLLOW=0`).
- **Sessions**: turns group by the client-supplied `x-session-id` request
  header (the access-log config carries it via a `request_header.x-session-id`
  token — clients send it, praxis forwards it, the record includes it). The
  opencode provider and the demo send it; curl smoke tests fall back to the
  user-agent token.
- **Surface** (route: `collector-user-mhepburn.apps.ocp.cloud.rhai-tmm.dev`):
  - `/` — the flow graphic: one lane per session, one node per routed turn,
    colored by tier (SIMPLE green / MEDIUM amber / COMPLEX red / REASONING
    purple), node size by score, hover for model+score+latency; live stats
    sidebar (decisions, per-tier counts, models served, classify p50/p95).
  - `/snapshot` — full JSON state · `/events?since=N` — incremental feed.
  - `/metrics` — Prometheus text: `router_decisions_total{label}`,
    `router_model_selections_total{model}`, `router_upstream_status_total`,
    `router_classify_latency_seconds` + `router_request_duration_seconds`
    histograms, session/record counters.
- **Prometheus**: 80-collector-servicemonitor.yaml ships the ServiceMonitor
  but is deliberately NOT in kustomization resources — UWM only watches
  namespaces labelled `openshift.io/cluster-monitoring=true`, and
  user-mhepburn is a pre-existing user sandbox (labelling it is a GitOps
  mutation outside this repo). Until the label lands, `/metrics` is verifiable
  directly and the flow graphic is the live view.

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
Containerfile.kvcached           thin layer FROM the official kvcached-vllm 0.30 image;
                                 OpenShift arbitrary-UID cache-dir fixes only
deploy/85-phase2-llmisvc.yaml    the RHOAI-reference pair (qwen3-06b/8b, rhaiis vLLM)
deploy/90-phase2-arms.yaml       the three-arm comparison fleet (a-/b-/c- × 0.6b/8b,
                                 kvcached image, ENABLE_KVCACHED per arm)
deploy/config/praxis-config-phase2.yaml  the triple-listener arm config (:8080/:8081/:8082)
hack/stage-build-context.sh      assembles the binary-build context
hack/undeploy-sardeenz.sh        name-based sardeenz teardown (the arm is retired)
hack/deploy-sardeenz.sh          (retired, kept as reference) the original Arm B
```
