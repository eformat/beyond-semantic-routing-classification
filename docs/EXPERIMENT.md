# The KV-cache substrate experiment

**Question:** does the gateway's choice of KV-cache substrate (and caching strategy) change multi-turn LLM economics, and where does the semantic-routing seam sit?

**Answer:** yes — dramatically — and the seam sits *behind* the router. The routing policy is identical on every arm; what changes multi-turn economics is whether the substrate reuses KV state across turns.

## Setup

Four arms on the same physical node (ocp-jfns-gpu-1, MIG-partitioned H200), same engine (vLLM 0.30), same models (Qwen3 0.6B `c1899de` + Qwen3 8B `b968826`), same router path (`client → praxis → llm-d-sc gRPC classifier → tier splice → substrate`), same conversations. **The only variable is the KV-cache substrate.**

| arm | substrate | KV reuse | layout |
|---|---|---|---|
| A | native vLLM APC | on (static, per-slice) | 2 slices: 18GB (0.6B) + 35GB (8B) |
| B | kvcached VMM | **off** — re-prefill every turn | 2 slices: 18GB + 35GB |
| C | kvcached VMM | on (bounded APC) | 2 slices: 18GB + 35GB |
| D | kvcached VMM | on (elastic) | **one 71GB slice, both models** |

The kvcached *image* runs on every arm (the engine is held constant); the VMM only takes over KV management where enabled. Routing: SIMPLE/MEDIUM → 0.6B, COMPLEX → 8B on every arm; REASONING turns escalate to the MaaS gateway's glm-5.3-flash — identical across arms, the built-in noise floor.

**Run:** 16 conversations × 12 turns × 4 arms = 768 turns (2026-10-23, `results/2026-10-23-matrix-16conv.json`). Turn 1 prefills; turns 2–12 resend the growing history — where prefix caching pays.

## Results

| metric | A (native APC) | B (no reuse) | C (kvcached APC) | D (one slice) |
|---|---|---|---|---|
| Σ TTFT / conv **median** | 21.21s | 25.99s | 21.24s | **20.69s** |
| Σ TTFT / conv mean | 23.01s | 26.14s | 22.32s | 20.53s |
| Σ total / conv (mean) | 41.67s | 42.92s | 40.02s | **30.67s** |
| decode ms/token p50 | 1238 | 1253 | 1249 | **632** |
| prefix-cache hits (Δ over run) | 208,128 / 208,384 | **0** | 208,128 / 208,384 | 208,128 / 208,384 |
| TTFT stalls (>20s) | 1 / 192 | 0 / 192 | 0 / 192 | 0 / 192 |

**Medians are the headline** — one ~31–60s TTFT stall inflates a whole conversation's mean (arm A's mean 23.01 vs median 21.21); B's mean ≈ median (26.14 / 25.99) shows its penalty is real, not stall artifact.

## Conclusions

1. **Reuse is the strategy.** Arm B — the counterfactual, every turn re-prefilled from scratch — loses **+4.8s median TTFT per conversation (+23%)** and the penalty grows linearly with history length. The reuse arms hit **~99.9% prefix-cache hit rate** on the multi-turn workloads (208,128 of 208,384 queries).
2. **The kvcached substrate is cost-free when APC is on.** C matches native A on TTFT (21.24 vs 21.21 median): the VMM layer adds no measurable latency over vLLM's own prefix cache. Elastic KV management does not cost multi-turn performance.
3. **One slice beats two.** Arm D — both models co-located on a single 71GB MIG device — wins on *every* metric: lowest TTFT (20.69 median), tightest distribution (max 21.29s — essentially zero variance), **2× decode throughput** (632 vs ~1240 ms/token; the single device exposes more SMs to the 8B than the 35GB slices), and lowest total conversation cost (30.67s vs 40–43s). The two-slice layout buys nothing.
4. **TTFT stalls are not substrate-specific.** One stall in 768 turns — on arm A, the *native* baseline (~0.1–0.5% rate; ~31–60s, then recovers). Not a kvcached artifact; consistent with a transient platform stall.
5. **The noise floor works.** REASONING turns (16 per arm) escalate to the MaaS glm tier with identical timing across arms (±0.03s) — a live control that the measurement itself is stable.

## Implications for the routing seam

The substrate choice changes multi-turn economics by more than any per-turn routing decision could: B vs D is **+26% TTFT and +40% total conversation cost** with identical routing. Two design consequences for the policy layer:

- **Stay/switch economics move with the substrate.** Under a re-prefill substrate (B), staying buys nothing cache-wise — escalate freely. Under a reuse substrate (A/C/D), staying saves the full prefix re-computation — the optimal stay/switch threshold shifts.
- **kvcached breadth vs prefix hits is the real tension.** kvcached packs many models per GPU but (without its bounded APC) forfeits cross-turn reuse; a policy that keeps sticky multi-turn work on reuse-capable substrates and farms cheap/simple turns to the breadth tier is the shape the numbers support.

## Caveats

- **vLLM 0.30.0** is the tested ceiling for kvcached (upstream `ghcr.io/ovg-project/kvcached-vllm:latest` = `kvcached-v0.1.6-vllm-v0.30.0`); no v0.31 pairing exists yet, so the rerun on RHOAI 3.6's default vLLM is blocked until upstream ships one. The v0.30 final results are preserved in `results/backup-vllm-0.30/`.
- One replica per model per arm; single node; max_tokens 40/60 (short completions — TTFT dominates total latency by design).
- REASONING turns ride the MaaS gateway (shared, outside the namespace) — their timing is a control, not an arm measurement.
