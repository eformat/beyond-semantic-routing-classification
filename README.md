# beyond-semantic-routing-classification

A PoC that goes **beyond prompt classification**: choosing the right model for the next turn of a
multi-turn conversation without compromising the rest of the task — and determining where semantic
routing belongs in an existing inference stack. **The finding so far:** the routing policy matters
less than the KV-cache substrate behind it — the same router over four substrates that differ only
in cache management swings multi-turn TTFT by 26% and total conversation cost by 40%
(→ [docs/EXPERIMENT.md](docs/EXPERIMENT.md)).

## The stack (current: the 4-arm substrate comparison)

```
opencode / client (multi-turn, full messages[] each turn — no client changes)
   ▼
Praxis edge gateway (this repo's beyond-routing-filter crate)
   llm-d-sc signal (gRPC) → tier splice → llm_d_sc.model metadata per classification
   ▼
Inference substrate — the ONLY experimental variable, four arms:
   :8080  arm A  native vLLM APC, two MIG slices (18GB + 35GB)
   :8081  arm B  kvcached, APC disabled — re-prefill every turn (the counterfactual)
   :8082  arm C  kvcached, bounded APC, two MIG slices
   :8083  arm D  kvcached, bounded APC, both models on one 71GB slice
   ▼
REASONING tier escalates to the MaaS gateway's glm-53-flash — identical on every
arm, the built-in noise floor
```

Routing: SIMPLE/MEDIUM → Qwen3-0.6B · COMPLEX → Qwen3-8B · REASONING → MaaS. Same engine
(vLLM 0.30), same models, same node, same prompts on every arm.

## Layout

```
router/      the beyond-routing-filter crate (Praxis HTTP filter, vendored from
             llm-d-sc-praxis-filter v0.1.0 and evolved; praxis git dep v0.7.5)
deploy/      OpenShift manifests: llm-d-sc classifier, praxis + the four arm
             listeners, the phase-2 LLMInferenceServices, the collector stack
collector/   live decisioning dashboard (flow.html) — lane-merged per-arm
             comparison, routing stats, /snapshot · /events · /metrics, /reset
hack/        experiment tooling: the matrix harness (matrix-arms.py), the results
             site builder (build_matrix_site.py), the 4-arm replay (replay-arms.sh)
eval/        benchmarks (model-routing-eval corpus + synthetic conversations)
results/     run records (matrix-*.json) + the results site (site/index.html) —
             one self-contained HTML per run
docs/        architecture.md, EXPERIMENT.md (the full experiment write-up)
```

## The experiment

Four arms on the same physical node, same engine, same models, same prompts — **the only variable
is the KV-cache substrate**. 16 conversations × 12 turns × 4 arms = 768 turns per run.

Headline (Σ TTFT per conversation, median): **A 21.2s · B 26.0s · C 21.2s · D 20.7s** —
no-reuse loses +23%, the kvcached VMM is cost-free with APC, and the single shared slice wins
every metric (2× decode throughput, near-zero variance, lowest total cost).

Full setup, results, conclusions and the routing-seam implications:
**[docs/EXPERIMENT.md](docs/EXPERIMENT.md)** · the rendered narrative:
**[results/site/index.html](results/site/index.html)**.

## Build

```bash
cd router
cargo +1.96.0 test     # in-process stub server, decision table, config validation
```

The filter registers with any praxis server build via
`[package.metadata.praxis-filters]` (build-time auto-discovery — see `Containerfile.praxis`).

## Run

```bash
make help              # the target list (images, deploy, status, smoke)
make deploy            # apply all PoC resources into user-mhepburn
hack/replay-arms.sh    # replay one fixture through all four arms — the
                       # collector's Arm comparison renders the 4 lanes
hack/matrix-arms.py    # full matrix run — writes results/matrix-*.json
hack/build_matrix_site.py  # render the run into the self-contained results site
```

See `deploy/README.md` for the deployment details.
