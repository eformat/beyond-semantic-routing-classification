#!/usr/bin/env python3
"""beyond-semantic-routing decision collector (phase 1).

Zero-dependency (Python stdlib only) in-cluster service that turns the
praxis routing chain's per-request decision records into live state:

  ingestion
    * K8s log-follow: streams the praxis pod's access_log records through the
      in-cluster Kubernetes API (ServiceAccount + pods/log RBAC). One follower
      thread per praxis pod; the pod list refreshes so restarts re-attach.
    * POST /events: a CloudEvents (structured_json) receiver. praxis v0.7.5
      ships `cloud_events` behind the off-by-default `cloud-events-filter`
      feature; when the image gains that feature and the config adds the
      filter, this endpoint becomes the primary feed and the log tail can be
      retired (set COLLECTOR_LOG_FOLLOW=0).

  state
    * Decisions are grouped into sessions by the client-supplied
      `x-session-id` request header (the access_log config carries it via a
      `request_header.x-session-id` field token; opencode providers and the
      demo send it). Fallback: the user-agent token, then "unknown".
    * Bounded: the last MAX_SESSIONS sessions, MAX_TURNS turns each.

  surface
    * GET  /            flow.html (the live flow graphic)
    * GET  /snapshot    full JSON state (sessions + turns + stats)
    * GET  /events?since=N   incremental decision events for the graphic
    * GET  /metrics     Prometheus text format (scraped by user-workload
                         Prometheus via the ServiceMonitor)
    * GET  /healthz     liveness
    * POST /events      CloudEvents receiver (see above)

The access_log record line renders with ANSI codes around level/module and
the `record` key itself; the record JSON (first key `client_ip`) is the only
clean span, so ingestion anchors on it and takes everything to end-of-line.
"""

import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -----------------------------------------------------------------------------
# Configuration (env)
# -----------------------------------------------------------------------------

LISTEN_ADDR = os.environ.get("COLLECTOR_ADDR", "0.0.0.0:8080")
PRAXIS_LABEL_SELECTOR = os.environ.get(
    "PRAXIS_LABEL_SELECTOR", "app.kubernetes.io/name=praxis"
)
LOG_FOLLOW = os.environ.get("COLLECTOR_LOG_FOLLOW", "1") == "1"
POD_RELIST_SECS = float(os.environ.get("COLLECTOR_POD_RELIST_SECS", "15"))
MAX_SESSIONS = int(os.environ.get("COLLECTOR_MAX_SESSIONS", "50"))
MAX_TURNS = int(os.environ.get("COLLECTOR_MAX_TURNS", "80"))
# The log follower re-reads the last tailLines on every reconnect (the follow
# stream ends on the socket-idle timeout and the relist loop re-attaches), so
# the same access record re-arrives; this bounds the request_id dedup set.
MAX_SEEN_REQUESTS = int(os.environ.get("COLLECTOR_MAX_SEEN_REQUESTS", "4096"))
ACTIVE_WINDOW_SECS = 300.0  # "active sessions" gauge window

HERE = os.path.dirname(os.path.abspath(__file__))

# In-cluster Kubernetes API access (ServiceAccount projection).
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def _sa_read(name):
    try:
        with open(os.path.join(_SA_DIR, name)) as fh:
            return fh.read().strip()
    except OSError:
        return None


K8S_TOKEN = _sa_read("token")
# ca.crt must stay a PATH: ssl.create_default_context(cafile=...) takes a
# filename, not the PEM content (passing the content itself yields
# "[Errno 36] File name too long").
K8S_CA_PATH = os.path.join(_SA_DIR, "ca.crt")
K8S_CA = K8S_CA_PATH if os.path.isfile(K8S_CA_PATH) else None
K8S_NS = _sa_read("namespace") or os.environ.get("POD_NAMESPACE", "user-mhepburn")
K8S_HOST = "https://{}:{}".format(
    os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
    os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
)

# Classify-latency histogram buckets (seconds). The access record carries
# latency_us; bucket edges chosen around the measured shape (cache hit ~1ms,
# miss ~5-35ms).
LATENCY_BUCKETS = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0]
REQUEST_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]

# -----------------------------------------------------------------------------
# Store: bounded, lock-guarded decision state
# -----------------------------------------------------------------------------


class Store:
    """Session-grouped decision state plus the aggregate counters."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.seq = 0
        self.events = []  # bounded decision events, oldest last
        self.sessions = OrderedDict()  # id -> {"started": ts, "turns": [event]}
        # aggregates
        self.records_total = 0
        self.decisions_total = 0
        self.seen_requests = OrderedDict()  # request_id dedup (bounded)
        self.label_counts = {}
        self.model_counts = {}
        self.status_counts = {}
        self.classify_lat = []  # (monotonic sample list, bounded)
        self.request_durs = []
        self.latency_hist = [0] * (len(LATENCY_BUCKETS) + 1)
        self.request_hist = [0] * (len(REQUEST_BUCKETS) + 1)
        self.sessions_total = 0
        self.recent_session_ts = []  # for the active gauge window
        self.session_event_ts = []  # (ts, session) for per-minute rate
        self.reconnects = 0

    # -- ingestion ------------------------------------------------------------

    def add_decision(self, rec, source):
        """Ingest one access-record/CloudEvent decision mapping."""
        ts = time.time()
        label = rec.get("label") or "-"
        model = rec.get("model") or "-"
        status = rec.get("status") or "-"
        lat_us = rec.get("latency_us")
        with self.lock:
            # Dedup on request_id: the log follower starts at tailLines on
            # every reconnect (the follow stream ends on the socket-idle
            # timeout, the relist loop re-attaches), so the same record
            # re-arrives and would multiply the sessions and aggregates.
            # request_id is unique per request — skip already-ingested ones.
            rid = rec.get("request_id")
            if rid:
                if rid in self.seen_requests:
                    return None
                self.seen_requests[rid] = ts
                while len(self.seen_requests) > MAX_SEEN_REQUESTS:
                    self.seen_requests.popitem(last=False)
            self.seq += 1
            ev = {
                "seq": self.seq,
                "ts": ts,
                "source": source,
                "session": rec.get("session") or "unknown",
                "request_id": rec.get("request_id") or "-",
                "path": rec.get("path") or "-",
                "status": status,
                "label": label,
                "model": model,
                "score": rec.get("score"),
                "latency_us": lat_us,
                "duration_ms": rec.get("duration_ms"),
                "user_agent": rec.get("user_agent"),
                # Session observability: the conversation fingerprint + this
                # turn's prompt preview + the message count (what makes the
                # flow graphic legible).
                "session_fp": rec.get("session_fp"),
                "prompt": rec.get("prompt"),
                "turns": rec.get("turns"),
                # PHASE 2: the substrate arm (a = native prefix caching,
                # b = kvcached with no APC, c = kvcached + APC) — the
                # comparison dimension.
                "arm": rec.get("arm"),
            }
            self.events.append(ev)
            if len(self.events) > MAX_SESSIONS * MAX_TURNS:
                self.events = self.events[-(MAX_SESSIONS * MAX_TURNS) :]

            sid = ev["session"]
            sess = self.sessions.get(sid)
            if sess is None:
                sess = {"started": ts, "turns": []}
                self.sessions[sid] = sess
                self.sessions_total += 1
                # bound the session map (oldest first)
                while len(self.sessions) > MAX_SESSIONS:
                    self.sessions.popitem(last=False)
            sess["turns"].append(ev)
            if len(sess["turns"]) > MAX_TURNS:
                del sess["turns"][: -MAX_TURNS]

            # aggregates
            self.decisions_total += 1
            self.label_counts[label] = self.label_counts.get(label, 0) + 1
            self.model_counts[model] = self.model_counts.get(model, 0) + 1
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            self.recent_session_ts.append(ts)
            self.session_event_ts.append(ts)
            if lat_us is not None:
                secs = lat_us / 1_000_000.0
                self.classify_lat.append(secs)
                if len(self.classify_lat) > 5000:
                    self.classify_lat = self.classify_lat[-2500:]
                idx = len(LATENCY_BUCKETS)
                for i, edge in enumerate(LATENCY_BUCKETS):
                    if secs <= edge:
                        idx = i
                        break
                self.latency_hist[idx] += 1
            d_ms = rec.get("duration_ms")
            if d_ms is not None:
                secs = d_ms / 1000.0
                self.request_durs.append(secs)
                if len(self.request_durs) > 5000:
                    self.request_durs = self.request_durs[-2500:]
                idx = len(REQUEST_BUCKETS)
                for i, edge in enumerate(REQUEST_BUCKETS):
                    if secs <= edge:
                        idx = i
                        break
                self.request_hist[idx] += 1
            return ev

    def add_record_seen(self):
        with self.lock:
            self.records_total += 1

    def add_reconnect(self):
        with self.lock:
            self.reconnects += 1

    # -- queries --------------------------------------------------------------

    def _pct(self, samples, q):
        if not samples:
            return None
        s = sorted(samples)
        return round(s[min(len(s) - 1, int(q * len(s)))] * 1_000_000)  # us

    def snapshot(self):
        now = time.time()
        with self.lock:
            recent = [t for t in self.recent_session_ts if now - t <= ACTIVE_WINDOW_SECS]
            self.recent_session_ts = recent
            rate_events = [t for t in self.session_event_ts if now - t <= 60.0]
            self.session_event_ts = rate_events
            sessions = []
            for sid, sess in self.sessions.items():
                sessions.append(
                    {
                        "id": sid,
                        "started": sess["started"],
                        "turns": list(sess["turns"]),
                    }
                )
            return {
                "now": now,
                "uptime_s": round(now - self.started),
                "stats": {
                    "records_total": self.records_total,
                    "decisions_total": self.decisions_total,
                    "labels": dict(self.label_counts),
                    "models": dict(self.model_counts),
                    "upstream_status": dict(self.status_counts),
                    "classify_us_p50": self._pct(self.classify_lat, 0.50),
                    "classify_us_p95": self._pct(self.classify_lat, 0.95),
                    "decisions_per_min": round(len(rate_events), 2),
                    "active_sessions": len(
                        {sid for sid, s in self.sessions.items()
                         if s["turns"] and now - s["turns"][-1]["ts"] <= ACTIVE_WINDOW_SECS}
                    ),
                    "recent_samples": len(recent),
                    "sessions_total": self.sessions_total,
                    "log_reconnects": self.reconnects,
                },
                "sessions": sessions,
            }

    def events_since(self, since):
        with self.lock:
            return [e for e in self.events if e["seq"] > since]

    def metrics_text(self):
        with self.lock:
            def hist(name, help_text, edges, values, extra_labels=""):
                lines = ["# HELP {} {}".format(name, help_text),
                         "# TYPE {} histogram".format(name)]
                cum = 0
                for i, edge in enumerate(edges):
                    cum += values[i]
                    lines.append('{}_bucket{{le="{}"{}}} {}'.format(
                        name, edge, extra_labels, cum))
                cum += values[-1]
                lines.append('{}{{le="+Inf"{}}} {}'.format(name, extra_labels, cum))
                total = sum(values)
                lines.append("{}_count{} {}".format(name, extra_labels, total))
                lines.append("{}_sum{} {}".format(
                    name, extra_labels, round(sum(self.classify_lat) if name.endswith("classify_latency_seconds") else sum(self.request_durs), 6)))
                return lines

            lines = []
            lines.append("# HELP router_decisions_total Routing decisions ingested by the collector.")
            lines.append("# TYPE router_decisions_total counter")
            for label, n in sorted(self.label_counts.items()):
                lines.append('router_decisions_total{{label="{}"}} {}'.format(label, n))
            lines.append("# HELP router_model_selections_total Upstream model selections observed (short names as served by MaaS).")
            lines.append("# TYPE router_model_selections_total counter")
            for model, n in sorted(self.model_counts.items()):
                lines.append('router_model_selections_total{{model="{}"}} {}'.format(model, n))
            lines.append("# HELP router_upstream_status_total Upstream response statuses.")
            lines.append("# TYPE router_upstream_status_total counter")
            for status, n in sorted(self.status_counts.items()):
                lines.append('router_upstream_status_total{{status="{}"}} {}'.format(status, n))
            lines.append("# HELP router_decision_records_total Access records ingested (with or without a decision).")
            lines.append("# TYPE router_decision_records_total counter")
            lines.append("router_decision_records_total {}".format(self.records_total))
            lines.append("# HELP router_sessions_total Sessions created (x-session-id grouping).")
            lines.append("# TYPE router_sessions_total counter")
            lines.append("router_sessions_total {}".format(self.sessions_total))
            lines.append("# HELP router_log_stream_reconnects_total praxis pod log follower re-attachments.")
            lines.append("# TYPE router_log_stream_reconnects_total counter")
            lines.append("router_log_stream_reconnects_total {}".format(self.reconnects))
            lines += hist(
                "router_classify_latency_seconds",
                "Classifier latency per decision.",
                LATENCY_BUCKETS,
                self.latency_hist,
            )
            lines += hist(
                "router_request_duration_seconds",
                "Full request duration per routed request.",
                REQUEST_BUCKETS,
                self.request_hist,
            )
            return "\n".join(lines) + "\n"


STORE = Store()

# -----------------------------------------------------------------------------
# Record parsing: access_log line -> decision mapping
# -----------------------------------------------------------------------------


def parse_session(rec):
    """Group by the conversation fingerprint when present (the session
    module's stable conversation key — every turn of one conversation shares
    it), then the client's x-session-id, then the user-agent token.

    PHASE 2: the arm suffix — the phase-2 cluster names carry the arm prefix
    (a- = native prefix caching, b- = kvcached with no APC, c- = kvcached +
    APC), so the SAME conversation replayed through the three arms renders as
    three lanes side by side (c:<fp> vs c:<fp>:a / :b / :c) — the substrate
    comparison view.
    """
    cluster = rec.get("cluster") or ""
    arm = ""
    if cluster.startswith("b-"):
        arm = ":b"
    elif cluster.startswith("c-"):
        arm = ":c"
    elif cluster.startswith("a-"):
        arm = ":a"
    fp = _field(rec, "metadata.llm_d_sc.session_fp")
    if fp:
        return "c:" + str(fp)[:24] + arm
    sid = _field(rec, "request_header.x-session-id")
    if sid:
        return sid[:64] + arm
    ua = _field(rec, "request_header.user-agent")
    if ua:
        return "ua:" + re.split(r"[\s/]", ua)[0][:48] + arm
    return "unknown" + arm


def _field(rec, prefix):
    """Exact key first, then any key that starts with the prefix (the access
    record renders header tokens as `request_header.<name>` keys)."""
    if prefix in rec:
        return rec[prefix]
    for key in rec:
        if key.startswith(prefix):
            return rec[key]
    return None


def record_to_decision(rec):
    """Map an access-record dict to the collector's decision mapping.

    The record carries the routing decision under `metadata.llm_d_sc.*` keys
    plus the request dimensions. `model` here is the FULL internal path the
    filter spliced; the snapshot keeps it (the MaaS response `.model` echo is
    the short name — that divergence is a known MaaS behavior).
    """
    if not any(k.startswith("metadata.llm_d_sc.") for k in rec):
        return None  # a record without a decision (e.g. /models passthrough)
    cluster = rec.get("cluster") or ""
    arm = "b" if cluster.startswith("b-") else ("c" if cluster.startswith("c-") else ("a" if cluster.startswith("a-") else None))
    return {
        "session": parse_session(rec),
        "request_id": rec.get("request_id"),
        "path": rec.get("path"),
        "status": rec.get("status"),
        "label": rec.get("metadata.llm_d_sc.label"),
        "model": rec.get("metadata.llm_d_sc.model"),
        "score": rec.get("metadata.llm_d_sc.score"),
        "cluster": cluster,
        "arm": arm,
        "latency_us": _int_or_none(rec.get("metadata.llm_d_sc.latency_us")),
        "duration_ms": _int_or_none(rec.get("duration_ms")),
        "user_agent": _field(rec, "request_header.user-agent"),
        # Session observability: the conversation fingerprint + this turn's
        # prompt preview + the message count so far.
        "session_fp": rec.get("metadata.llm_d_sc.session_fp"),
        "prompt": rec.get("metadata.llm_d_sc.prompt"),
        "turns": _int_or_none(rec.get("metadata.llm_d_sc.turns")),
    }


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_log_line(line):
    """Extract the record JSON from one access_log line, if present.

    Anchors on the record's first key `client_ip` (the only clean span in the
    ANSI-decorated line) and takes everything to end-of-line, where the record
    closes.
    """
    marker = '{"client_ip'
    idx = line.find(marker)
    if idx < 0:
        return None
    span = line[idx:].rstrip()
    try:
        return json.loads(span)
    except json.JSONDecodeError:
        # tolerate a trailing ANSI reset after the closing brace
        m = re.search(r"\}[^}]*$", span)
        if m:
            try:
                return json.loads(span[: m.start() + 1])
            except json.JSONDecodeError:
                return None
        return None


def ingest_record(rec, source):
    STORE.add_record_seen()
    decision = record_to_decision(rec)
    if decision:
        return STORE.add_decision(decision, source)
    return None


# -----------------------------------------------------------------------------
# K8s log follower: one thread per praxis pod, pod list refresh loop
# -----------------------------------------------------------------------------


def _k8s_get(path, timeout=30):
    """GET a K8s API path with the ServiceAccount bearer token.

    `timeout` is the SOCKET read timeout: for a `follow=true` log stream it
    bounds the silence between lines, not the whole stream — an idle gap past
    it ends the stream, which the relist loop recovers from (see
    `log_follow_loop`).
    """
    ctx = ssl.create_default_context(cafile=K8S_CA) if K8S_CA else None
    req = urllib.request.Request(
        K8S_HOST + path,
        headers={"Authorization": "Bearer {}".format(K8S_TOKEN)},
    )
    return urllib.request.urlopen(req, context=ctx, timeout=timeout)


def _praxis_pods():
    sel = urllib.parse.quote(PRAXIS_LABEL_SELECTOR, safe="")
    path = "/api/v1/namespaces/{}/pods?labelSelector={}".format(K8S_NS, sel)
    try:
        with _k8s_get(path) as resp:
            payload = json.load(resp)
        return [
            item["metadata"]["name"]
            for item in payload.get("items", [])
            if item.get("metadata", {}).get("name")
        ]
    except Exception as exc:  # noqa: BLE001 — follower must survive API hiccups
        print("collector: pod list failed: {}".format(exc), flush=True)
        return []


def _follow_pod(pod):
    """Stream one pod's praxis-container logs, ingesting decision records.

    The follow stream's socket read timeout bounds silence between lines
    (10 minutes): a longer idle gap ends the stream and the relist loop
    re-attaches, trading a bounded dead-stream window for live tailing.
    """
    path = "/api/v1/namespaces/{}/pods/{}/log?follow=true&container=praxis&tailLines=40".format(
        K8S_NS, pod
    )
    try:
        with _k8s_get(path, timeout=600) as resp:
            print("collector: following pod {} logs".format(pod), flush=True)
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                rec = parse_log_line(line)
                if rec:
                    ingest_record(rec, "log:{}".format(pod))
    except Exception as exc:  # noqa: BLE001 — follow streams end on pod churn
        print("collector: follow {} ended: {}".format(pod, exc), flush=True)
    finally:
        STORE.add_reconnect()


def log_follow_loop():
    """Re-list praxis pods and keep a follower alive for each.

    Tracks the follower THREAD per pod: a follow stream ends on any socket
    timeout or pod churn, and the loop must re-attach when the thread is
    dead — NOT when the pod leaves the pod list. (The first version tracked
    only the pod set: a pod that stayed put after an idle gap was still
    'known', so its dead follower was never restarted and the collector went
    silent while appearing healthy.)
    """
    followers = {}
    while True:
        pods = _praxis_pods()
        for pod in pods:
            thread = followers.get(pod)
            if thread is None or not thread.is_alive():
                followers[pod] = threading.Thread(
                    target=_follow_pod, args=(pod,), daemon=True
                )
                followers[pod].start()
        for pod in list(followers):
            if pod not in pods:
                del followers[pod]
        time.sleep(POD_RELIST_SECS)


# -----------------------------------------------------------------------------
# CloudEvents receiver (the future primary feed)
# -----------------------------------------------------------------------------


def ingest_cloud_event(payload):
    """Map a structured CloudEvent to a decision, if it carries one.

    The planned praxis config maps `metadata.llm_d_sc.*` into the event `data`
    (llm_d_sc fields) with `subject: request_header.x-session-id`; both the
    direct mapping and a nested `data.llm_d_sc` shape are accepted.
    """
    data = payload.get("data") or {}
    nested = data.get("llm_d_sc") or {}
    subject = payload.get("subject")
    rec = {
        "request_id": payload.get("id"),
        "label": nested.get("label") or data.get("llm_d_sc_label") or data.get("label"),
        "model": nested.get("model") or data.get("llm_d_sc_model") or data.get("model"),
        "score": nested.get("score") or data.get("llm_d_sc_score") or data.get("score"),
        "status": nested.get("status") or data.get("llm_d_sc_status") or data.get("status"),
        "latency_us": nested.get("latency_us") or data.get("llm_d_sc_latency_us"),
    }
    if subject:
        rec["session"] = subject
    if rec.get("label") or rec.get("model"):
        STORE.add_decision(rec, "cloudevent")
        return True
    return False


# -----------------------------------------------------------------------------
# HTTP surface
# -----------------------------------------------------------------------------

FLOW_HTML_PATH = os.path.join(HERE, "flow.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "beyond-routing-collector/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout clean for log parsing
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/" or path == "/flow.html":
            try:
                with open(FLOW_HTML_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._json(404, {"error": "flow.html missing"})
        elif path == "/snapshot":
            self._json(200, STORE.snapshot())
        elif path == "/events":
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        pass
            self._json(200, {"events": STORE.events_since(since)})
        elif path == "/metrics":
            self._send(200, STORE.metrics_text().encode(), "text/plain; version=0.0.4")
        elif path == "/healthz":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/events":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        events = payload if isinstance(payload, list) else [payload]
        accepted = sum(1 for ev in events if ingest_cloud_event(ev))
        self._json(202, {"accepted": accepted, "received": len(events)})


def main():
    addr, port = LISTEN_ADDR.rsplit(":", 1)
    server = ThreadingHTTPServer((addr, int(port)), Handler)
    if LOG_FOLLOW and K8S_TOKEN:
        threading.Thread(target=log_follow_loop, daemon=True).start()
        print(
            "collector: log-follow on (ns={} selector={})".format(K8S_NS, PRAXIS_LABEL_SELECTOR),
            flush=True,
        )
    elif not K8S_TOKEN:
        print("collector: no ServiceAccount token; log-follow disabled", flush=True)
    print(
        "collector: serving on {} (snapshot=/snapshot metrics=/metrics)".format(LISTEN_ADDR),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
