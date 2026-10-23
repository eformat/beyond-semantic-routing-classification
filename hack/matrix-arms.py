#!/usr/bin/env python3
"""The phase-2 matrix run: a batched, controlled measurement across the three
substrate arms.

Design (the article's paired-conversation methodology):
  - N paired synthetic conversations, each 4 turns with a shared long system
    prefix (the cacheable block) and turns of varying complexity so the tier
    map is exercised: SIMPLE / MEDIUM / COMPLEX (REASONING escalates to the
    MaaS glm tier on every arm — identical, the noise floor).
  - Every conversation is replayed through ALL THREE arms (same prompts, same
    order, same x-session-id) — the collector groups by (fingerprint, arm), so
    the run renders as N x three side-by-side lanes.
  - Per turn: the request latency, the routed model, the status.
  - Per arm: the engine's prefix_cache counters BEFORE and AFTER the run —
    the cache-hit contrast at scale (arms A/C should accrue hits, arm B zero
    by design).

Output: the summary table (per-arm per-tier p50/p95 latency + the cache
deltas) and the run record at results/<date>-matrix.json (the
model-routing-eval results convention) for the results site.

Usage: hack/matrix-arms.py [--conversations 5] [--repeat 1] [--out results/...-matrix.json]
"""

import argparse
import json
import ssl
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

ARMS = {
    "A": "https://praxis-user-mhepburn.apps.ocp.cloud.rhai-tmm.dev",
    "B": "https://praxis-arm-b-user-mhepburn.apps.ocp.cloud.rhai-tmm.dev",
    "C": "https://praxis-arm-c-user-mhepburn.apps.ocp.cloud.rhai-tmm.dev",
    "D": "https://praxis-arm-d-user-mhepburn.apps.ocp.cloud.rhai-tmm.dev",
}
NAMESPACE = "user-mhepburn"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# The shared cacheable prefix: a LONG synthetic document (~15K tokens) — the
# article's "long context" tier. The KV-cache effect lives in the prefill of
# this block: arms A/C cache it after turn 1, arm B re-prefills it every
# turn. At this scale the delta is 1-3s per turn on the 8B.
MODULE_TMPL = (
    "module {name} (v0.{i}.3): pub struct {Name}Config {{ pub retries: u8, pub timeout_ms: u32, "
    "pub backoff: BackoffPolicy, pub tls: TlsMode }}\n"
    "impl {Name}Config {{ pub fn default_for(env: Env) -> Self {{ let retries = match env "
    "{{ Env::Prod => 5, Env::Staging => 3, Env::Dev => 1 }}; Self {{ retries, timeout_ms: "
    "{timeout}, backoff: BackoffPolicy::Exponential {{ base_ms: {base}, jitter: true }}, "
    "tls: TlsMode::PlatformCa }} }} pub fn validate(&self) -> Result<(), ConfigError> {{ "
    "if self.timeout_ms < 100 {{ return Err(ConfigError::TooFast) }} if self.retries > 9 "
    "{{ return Err(ConfigError::TooManyRetries) }} Ok(()) }} }}\n"
    "// NOTE(review): the retry budget interacts with the upstream rate limiter; the "
    "proxy must not retry a 429 beyond the limiter's window or the client sees a "
    "stamped. The timeout floor guards against the connect-phase dead zone.\n\n")

_long_ctx_cache = None


def long_context():
    """The shared prefix: the REAL beyond-routing router source (filter.rs,
    config.rs, testing.rs, prompt.rs — ~14K tokens of the actual code being
    routed through), read from the local repo at run time."""
    global _long_ctx_cache
    if _long_ctx_cache is None:
        root = Path(__file__).resolve().parent.parent
        files = ["router/src/filter.rs", "router/src/config.rs",
                 "router/src/testing.rs", "router/src/prompt.rs"]
        parts = []
        for rel in files:
            p = root / rel
            if p.exists():
                parts.append(f"// ===== {rel} =====\n" + p.read_text())
        _long_ctx_cache = ("You are a code-review assistant for a Rust HTTP proxy "
                           "(praxis). The following is the router crate's REAL source "
                           "— the request filter that classifies prompts via a gRPC "
                           "classifier, splices the resolved model into the JSON body, "
                           "and publishes filter metadata. Answer questions about this "
                           "code concisely.\n\n" + "\n\n".join(parts))
    return _long_ctx_cache


def approx_tokens(text):
    return max(1, len(text) // 4)

# The turn matrix for the real-scale run: 12 turns of mixed complexity, the
# way a real client drives a session — the full history resends every turn,
# so the prompt grows and the shared prefix compounds.
TURNS = [
    ("SIMPLE", "Reply with exactly one word: ok"),
    ("MEDIUM", "In one sentence: what does the request_id filter do before the classifier runs?"),
    ("SIMPLE", "What is 2+2? One word."),
    ("COMPLEX", "Design a fault-tolerant multi-region payment platform: choose an "
                "event-sourcing vs CQRS split, reason through exactly-once semantics "
                "under network partitions, idempotency keys at every write path, "
                "leader election, and a reconciliation strategy for cross-ledger "
                "drift. Justify each trade-off."),
    ("MEDIUM", "Why does the body hook queue headers via filter_state instead of "
               "request_headers_to_set, given the pre-read copy-back?"),
    ("SIMPLE", "Name the gRPC endpoint the llm_d_sc filter calls. One word."),
    ("COMPLEX", "Reason through the proxy's failure modes: the classifier unavailable, "
                "a stalled upstream gateway, and a non-JSON body reaching the splice. "
                "For each, derive the fail-open behaviour the code actually implements "
                "and the one it still needs."),
    ("MEDIUM", "Why is the in-chain json_body replace skipped when the classifier "
               "metadata is absent, per the code?"),
    ("SIMPLE", "Which crate file holds the access-log field list? One word."),
    ("MEDIUM", "What does filter_state survive that a queued header does not, and why?"),
    ("REASONING", "Prove or disprove: for every positive integer n, the number n^7 - n "
                  "is divisible by 42. Construct a rigorous number-theoretic argument, "
                  "considering both cases where n is prime and composite, and derive "
                  "the minimal divisibility set from first principles."),
    ("COMPLEX", "Propose the minimal change to this filter that would let the body-phase "
                "classification escalate mid-conversation without a second gateway hop, "
                "and reason through what breaks in the pre-read copy-back."),
]

def chat(base, session, messages, max_tokens=200):
    """One streaming turn: TTFT (time to the FIRST delta chunk of any kind —
    the model's first token; the prefill + queue is what the cache saves) +
    the total. With --reasoning-parser the think block streams as
    reasoning_content BEFORE content, so the first-token metric must count
    reasoning deltas too."""
    body = json.dumps({
        "model": "router", "max_tokens": max_tokens, "stream": True,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "x-session-id": session})
    t0 = time.time()
    ttft = None
    gen_chars = 0
    served = None
    with urllib.request.urlopen(req, context=CTX, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if served is None:
                served = chunk.get("model")
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            content = delta.get("content") or delta.get("reasoning_content") or ""
            if delta and ttft is None:
                ttft = time.time() - t0
            if content:
                gen_chars += len(content)
    total = time.time() - t0
    return {
        "served": served,
        "ttft_s": round(ttft, 3) if ttft is not None else None,
        "latency_s": round(total, 3),
        "gen_chars": gen_chars,
        "prompt_tokens": None,   # streaming usage lands in the final chunk if at all
        "completion_tokens": None,
    }


WORKLOAD_LABELS = {
    "A": ("a-qwen3-06b", "a-qwen3-8b"),
    "B": ("b-qwen3-06b", "b-qwen3-8b"),
    "C": ("c-qwen3-06b", "c-qwen3-8b"),
}


def cache_counters(arm):
    """The 8B engine's prefix-cache counters + the gpu cache usage, per arm.
    Arms A/B/C: the kserve workload pods (the label selector, the metrics on
    :8000 over HTTPS with the per-pod KServe certs). Arm D: the dual-engine
    plain Deployment (the 8B engine on :8001, PLAIN HTTP — no KServe TLS)."""
    if arm == "D":
        label = "app.kubernetes.io/name=d-dual-vllm"
        port = 8001
        conn = "http.client.HTTPConnection('localhost', port)"
    else:
        isvc = WORKLOAD_LABELS[arm][1]
        label = f"app.kubernetes.io/name={isvc},kserve.io/component=workload"
        port = 8000
        conn = ("http.client.HTTPSConnection('localhost', port, context=ctx)")
    pod = subprocess.run(
        ["oc", "-n", NAMESPACE, "get", "pod", "-l", label,
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, timeout=60).stdout.strip()
    out = subprocess.run(
        ["oc", "-n", NAMESPACE, "exec", pod, "--", "python3", "-c",
         "import http.client, ssl, sys\n"
         "port = int(sys.argv[1])\n"
         "ctx = ssl.create_default_context(); ctx.check_hostname=False; "
         "ctx.verify_mode=ssl.CERT_NONE\n"
         "conn = " + conn + "\n"
         "conn.request('GET', '/metrics')\n"
         "for line in conn.getresponse().read().decode().splitlines():\n"
         "    if line.startswith('vllm:prefix_cache_queries_total') or "
         "line.startswith('vllm:prefix_cache_hits_total'):\n"
         "        print(line.split()[-1])\n"
         "    elif line.startswith('vllm:gpu_cache_usage_perc') and not "
         "line.startswith('#'):\n"
         "        print(line.split()[-1])",
         str(port)],
        capture_output=True, text=True, timeout=120).stdout.split()
    if len(out) >= 3:
        return {"queries": float(out[0]), "hits": float(out[1]),
                "gpu_cache_usage": float(out[2])}
    if len(out) >= 2:
        return {"queries": float(out[0]), "hits": float(out[1]), "gpu_cache_usage": None}
    return {"queries": None, "hits": None, "gpu_cache_usage": None}


def pctl(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=1,
                    help="passes over the whole corpus (prefix hits compound)")
    ap.add_argument("--out", default=None,
                    help="run record path (default results/<date>-matrix.json)")
    args = ap.parse_args()

    out_path = args.out
    if out_path is None:
        results = Path(__file__).resolve().parent.parent / "results"
        results.mkdir(exist_ok=True)
        out_path = str(results / f"{time.strftime('%Y-%m-%d')}-{int(time.time())}-matrix.json")

    rows = []
    cache = {}
    ts = int(time.time())
    sys_ctx = long_context()
    print(f"shared prefix: {len(sys_ctx)} chars (~{approx_tokens(sys_ctx)} tokens)")

    def gen_tokens(r):
        return max(1, (r.get("gen_chars") or 0) // 4)

    for arm, base in ARMS.items():
        cache[arm] = {"before": cache_counters(arm), "after": None}
        print(f"--- arm {arm} ({base}) cache before: {cache[arm]['before']}")

        for conv in range(args.conversations):
            for rep in range(args.repeat):
                session = f"matrix-{ts}-c{conv}-r{rep}"
                history = []
                for ti, (intent, prompt) in enumerate(TURNS):
                    history.append({"role": "user", "content": prompt})
                    # real-scale: the FULL history resends every turn (a real
                    # client resends everything; the prompt grows per turn)
                    messages = [{"role": "system", "content": sys_ctx}] + history
                    try:
                        m = chat(base, session, messages)
                        status = 200
                    except Exception as exc:  # noqa: BLE001
                        m = {"served": "-", "ttft_s": None, "latency_s": None,
                             "gen_chars": None, "prompt_tokens": None,
                             "completion_tokens": None}
                        status = f"ERR {exc}"[:60]
                    history.append({"role": "assistant", "content": "(reply)"})
                    row = {
                        "arm": arm, "conversation": conv, "repeat": rep,
                        "turn": ti, "tier_intent": intent, "status": str(status),
                        **m,
                    }
                    # the decode-path metric: (total - TTFT) per generated token —
                    # isolates the decode cost from the response-length variance
                    if row.get("ttft_s") is not None and row.get("latency_s") is not None:
                        decode_s = row["latency_s"] - row["ttft_s"]
                        row["decode_ms_per_tok"] = round(decode_s * 1000 / gen_tokens(row), 1) if decode_s > 0 else None
                    rows.append(row)
                    print(f"  [{arm}] conv{conv} r{rep} turn{ti} ({intent}) -> "
                          f"{m['served']} ttft={m['ttft_s']}s total={m['latency_s']}s {status}")

        cache[arm]["after"] = cache_counters(arm)
        print(f"--- arm {arm} cache after: {cache[arm]['after']}")

    # -- summary ---------------------------------------------------------------
    print("\n=== per-arm per-tier TTFT (s, the prefill-dominated metric) ===")
    print(f"{'arm':4} {'tier':8} {'n':>3} {'p50':>7} {'p95':>7} {'models served'}")
    summary = {}
    for arm in ARMS:
        for tier in ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING"):
            lats = [r["ttft_s"] for r in rows
                    if r["arm"] == arm and r["tier_intent"] == tier
                    and r.get("ttft_s") is not None]
            models = sorted({r["served"] for r in rows
                             if r["arm"] == arm and r["tier_intent"] == tier
                             and r.get("served")})
            summary[f"{arm}/{tier}"] = {"n": len(lats), "p50": pctl(lats, .5),
                                        "p95": pctl(lats, .95), "models": models}
            if lats:
                print(f"{arm:4} {tier:8} {len(lats):>3} "
                      f"{pctl(lats, .5) or 0:>7} {pctl(lats, .95) or 0:>7} {','.join(models)}")

    print("\n=== prefix-cache delta on the 8B workloads (the substrate proof) ===")
    for arm in ARMS:
        b, a = cache[arm]["before"], cache[arm]["after"]
        if b["hits"] is not None and a["hits"] is not None:
            print(f"arm {arm}: queries {b['queries']:.0f} -> {a['queries']:.0f} "
                  f"(+{a['queries']-b['queries']:.0f}) | hits {b['hits']:.0f} -> "
                  f"{a['hits']:.0f} (+{a['hits']-b['hits']:.0f})"
                  + (f" | gpu cache usage {b['gpu_cache_usage']:.2f} -> "
                     f"{a['gpu_cache_usage']:.2f}" if b.get("gpu_cache_usage") is not None
                     and a.get("gpu_cache_usage") is not None else ""))
        else:
            print(f"arm {arm}: counters unavailable")

    # -- the WHAT-WINS answer: the cumulative cost of one real conversation ---
    print("\n=== per-conversation cumulative cost (the strategy answer) ===")
    print(f"{'arm':4} {'n':>3} {'Σ TTFT / conv (s)':>18} {'Σ total / conv (s)':>19}")
    conv_summary = {}
    for arm in ARMS:
        sums = []
        sums_total = []
        for conv in range(args.conversations):
            ttfts = [r["ttft_s"] for r in rows if r["arm"] == arm
                     and r["conversation"] == conv and r.get("ttft_s") is not None]
            tots = [r["latency_s"] for r in rows if r["arm"] == arm
                    and r["conversation"] == conv and r.get("latency_s") is not None]
            if ttfts:
                sums.append(sum(ttfts))
                sums_total.append(sum(tots))
        # the stall accounting: the turns whose TTFT stalled (>20s — the 60s
        # pattern is the known transient); the median is the outlier-resistant
        # per-conversation headline (one 60s stall in a conversation inflates
        # that conversation's mean by ~5s per turn)
        stalls = [r for r in rows if r["arm"] == arm and (r.get("ttft_s") or 0) > 20]
        conv_summary[arm] = {
            "conversations": len(sums),
            "sum_ttft_mean": round(statistics.mean(sums), 2) if sums else None,
            "sum_ttft_median": round(statistics.median(sums), 2) if sums else None,
            "sum_ttft_min": round(min(sums), 2) if sums else None,
            "sum_ttft_max": round(max(sums), 2) if sums else None,
            "sum_total_mean": round(statistics.mean(sums_total), 2) if sums_total else None,
            "sum_total_median": round(statistics.median(sums_total), 2) if sums_total else None,
            "stall_turns": len(stalls),
            "turns_total": len([r for r in rows if r["arm"] == arm]),
        }
        if sums:
            print(f"{arm:4} {len(sums):>3} {statistics.mean(sums):>18.2f} "
                  f"{statistics.mean(sums_total):>19.2f}")

    # -- the decode-path metric: the C-total caveat resolver -------------------
    print("\n=== decode latency per generated token (the C-total caveat resolver) ===")
    print(f"{'arm':4} {'n':>3} {'decode ms/token p50':>20}")
    for arm in ARMS:
        dpts = [r["decode_ms_per_tok"] for r in rows
                if r["arm"] == arm and r.get("decode_ms_per_tok") is not None]
        if dpts:
            s = sorted(dpts)
            print(f"{arm:4} {len(dpts):>3} {s[min(len(s)-1, int(.5*len(s)))]:>20.1f}")

    # -- the stall rate: the 60s TTFT pattern ----------------------------------
    print("\n=== TTFT stalls (>20s, the ~60s transient) ===")
    for arm in ARMS:
        cs = conv_summary.get(arm) or {}
        if cs:
            print(f"arm {arm}: {cs['stall_turns']} stalls / {cs['turns_total']} turns "
                  f"({cs['stall_turns']/max(1, cs['turns_total'])*100:.1f}%)")

    record = {
        "run": {
            "ts": ts,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "conversations": args.conversations,
            "repeat": args.repeat,
            "turns_per_conversation": len(TURNS),
            "arms": list(ARMS),
        },
        "rows": rows,
        "summary": summary,
        "conv_summary": conv_summary,
        "cache": cache,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(record, indent=1))
    print(f"\nrun record -> {out_path}")


if __name__ == "__main__":
    main()
