"""
Layer 1 — Feature extraction.

Converts a raw incident JSON into a compact incident_vector that draws signal
from BOTH logs and traces (plus metrics as secondary signal).

Design philosophy:
  - Small corpus (~29 items) → over-engineered embeddings will overfit.
    We use a ~20-dim hand-engineered feature set that generalises well.
  - Log signal: bag-of-keywords mapped to known error-class keywords, plus
    log-level distribution and raw ERROR/WARN counts.
  - Trace signal: max p99_deviation_ratio, max error_rate, count of
    service-pairs with errors, topology depth of alerting service.
  - Metric signal (tertiary): magnitude of delta for services that are
    primary suspects.
  - Service-fingerprint: a one-hot over the 14 known services (affected).
"""

from __future__ import annotations
import re
from typing import Any

# ── Known services (for one-hot) ───────────────────────────────────────────
KNOWN_SERVICES = [
    "edge-lb", "auth-svc", "checkout-svc", "payment-svc", "cart-svc",
    "catalog-svc", "recommender-svc", "inventory-svc", "notification-svc",
    "search-svc", "payments-db", "catalog-db", "cart-redis", "kafka-events",
]
SVC_INDEX = {s: i for i, s in enumerate(KNOWN_SERVICES)}

# ── Log keyword clusters → numeric signals ──────────────────────────────────
# Each tuple: (keyword_patterns, feature_name)
LOG_CLUSTERS = [
    (["connectionpool", "pool exhausted", "timeout acquiring connection",
      "connection refused", "connection reset"],
     "log_conn_pool"),
    (["outofmemoryerror", "java heap", "gc pause", "oomkill",
      "out of memory", "cgroup oom", "pod evicted"],
     "log_oom"),
    (["deadlock", "lock timeout", "lock wait", "deadlock detected"],
     "log_deadlock"),
    (["slow query", "query took longer", "db query latency",
      "query timeout", "statement timeout"],
     "log_slow_query"),
    (["tls", "certificate", "x509", "handshake failed", "ssl"],
     "log_tls"),
    (["rate limit", "429", "throttl"],
     "log_rate_limit"),
    (["retry exhausted", "fallback failed", "retrying request"],
     "log_retry"),
    (["consumer rebalance", "partition reassignment"],
     "log_kafka"),
    (["feature distribution drift", "model inference confidence"],
     "log_model_drift"),
    (["degraded behavior", "service error rate elevated"],
     "log_generic_degraded"),
]

LOG_FEATURE_NAMES = [c[1] for c in LOG_CLUSTERS]

# ── Outcome weights for history voting ─────────────────────────────────────
OUTCOME_WEIGHTS = {"success": 1.0, "partial": 0.4, "failed": 0.0}


def _cluster_score(text: str, patterns: list[str]) -> float:
    """Returns a normalised hit-rate for a cluster against a text blob."""
    if not text:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for p in patterns if p in text_lower)
    return min(1.0, hits / max(1, len(patterns)))


def _service_onehot(services: list[str]) -> list[float]:
    vec = [0.0] * len(KNOWN_SERVICES)
    for s in services:
        if s in SVC_INDEX:
            vec[SVC_INDEX[s]] = 1.0
    return vec


def _parse_delta(delta_str: str) -> tuple[float, float]:
    """'30 -> 99' → (30.0, 99.0)"""
    parts = re.split(r"\s*->\s*", delta_str.strip())
    if len(parts) == 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    return 0.0, 0.0


def _safe(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_features(incident: dict) -> dict:
    """
    Returns an incident_vector dict with keys:

    Log features  (direct signal from log lines):
      log_conn_pool, log_oom, log_deadlock, log_slow_query, log_tls,
      log_rate_limit, log_retry, log_kafka, log_model_drift,
      log_generic_degraded,
      log_error_count_norm, log_warn_count_norm

    Trace features (service-to-service signal):
      trace_max_p99_ratio, trace_max_error_rate,
      trace_error_pair_count_norm, trace_has_data (0/1)

    Metric features (secondary):
      metric_max_delta_ratio

    Topology features:
      topo_alerting_service_depth

    Service one-hot (14 dims):
      svc_<name>  for each known service

    Raw references (not used in similarity math, used for evidence):
      _alerting_service, _affected_services, _log_lines, _trace_pairs
    """
    vec: dict[str, Any] = {}

    # ── 1. Log features ──────────────────────────────────────────────────
    logs = incident.get("logs", [])
    log_blob = " ".join(entry.get("msg", "") for entry in logs).lower()

    for patterns, fname in LOG_CLUSTERS:
        vec[fname] = _cluster_score(log_blob, patterns)

    error_count = sum(1 for e in logs if e.get("level") == "ERROR")
    warn_count  = sum(1 for e in logs if e.get("level") == "WARN")
    total_logs  = max(1, len(logs))
    vec["log_error_count_norm"] = min(1.0, error_count / total_logs)
    vec["log_warn_count_norm"]  = min(1.0, warn_count  / total_logs)

    # ── 2. Trace features ────────────────────────────────────────────────
    traces = incident.get("traces", [])
    if traces:
        p99_ratios = []
        error_rates = []
        error_pairs = 0
        for t in traces:
            # p99_ms → deviation ratio: compare to a 100ms baseline
            p99 = _safe(t.get("p99_ms"))
            p50 = _safe(t.get("p50_ms"))
            if p99 > 0:
                ratio = p99 / max(100.0, p50)  # relative deviation
                p99_ratios.append(ratio)
            ec = _safe(t.get("error_count", 0))
            cnt = _safe(t.get("count", 1))
            er = ec / max(1.0, cnt)
            error_rates.append(er)
            if er > 0.05:
                error_pairs += 1

        vec["trace_max_p99_ratio"]       = max(p99_ratios, default=0.0)
        vec["trace_max_error_rate"]      = max(error_rates, default=0.0)
        vec["trace_error_pair_count_norm"] = min(1.0, error_pairs / 5)
        vec["trace_has_data"]            = 1.0
    else:
        vec["trace_max_p99_ratio"]        = 0.0
        vec["trace_max_error_rate"]       = 0.0
        vec["trace_error_pair_count_norm"]= 0.0
        vec["trace_has_data"]             = 0.0

    # ── 3. Metric features ───────────────────────────────────────────────
    metrics_window = incident.get("metrics_window", {})
    samples = metrics_window.get("samples", {})
    max_delta_ratio = 0.0
    for key, series in samples.items():
        if not series:
            continue
        vals = [v for _, v in series]
        if len(vals) >= 2:
            baseline = vals[0]
            peak     = max(vals)
            if baseline and baseline != 0:
                ratio = abs(peak - baseline) / abs(baseline)
                max_delta_ratio = max(max_delta_ratio, ratio)
    vec["metric_max_delta_ratio"] = min(10.0, max_delta_ratio) / 10.0  # norm to [0,1]

    # ── 4. Topology features ─────────────────────────────────────────────
    alerting_svc = incident.get("trigger_alert", {}).get("service", "")
    topo = incident.get("topology", {})
    depth = _compute_depth(alerting_svc, topo)
    vec["topo_alerting_service_depth"] = min(1.0, depth / 4.0)

    # ── 5. Affected-service one-hot ──────────────────────────────────────
    # Derive affected services from traces (services appearing in errors)
    affected_from_traces = set()
    for t in traces:
        if t.get("error_count", 0) > 0:
            affected_from_traces.add(t.get("from", ""))
            affected_from_traces.add(t.get("to", ""))
    if alerting_svc:
        affected_from_traces.add(alerting_svc)

    for svc, idx in SVC_INDEX.items():
        vec[f"svc_{svc}"] = 1.0 if svc in affected_from_traces else 0.0

    # ── 6. Raw references for evidence chain ─────────────────────────────
    vec["_alerting_service"]  = alerting_svc
    vec["_affected_services"] = list(affected_from_traces)
    vec["_log_lines"]         = [e.get("msg", "") for e in logs[:10]]
    vec["_trace_pairs"]       = [
        {"from": t.get("from"), "to": t.get("to"),
         "error_rate": round(_safe(t.get("error_count",0))/max(1,_safe(t.get("count",1))),3),
         "p99_ms": t.get("p99_ms")}
        for t in traces[:5]
    ]

    return vec


def extract_history_features(hist: dict) -> dict:
    """
    Extract a comparable feature vector from a historical incident entry.
    Uses the same dimensions as extract_features so similarity can be computed.
    """
    vec: dict[str, Any] = {}

    # Log features from log_signatures
    log_blob = " ".join(hist.get("log_signatures", [])).lower()
    for patterns, fname in LOG_CLUSTERS:
        vec[fname] = _cluster_score(log_blob, patterns)

    # No raw log level counts available — set neutral
    vec["log_error_count_norm"] = 0.3
    vec["log_warn_count_norm"]  = 0.1

    # Trace features from trace_signatures
    tsigs = hist.get("trace_signatures", [])
    if tsigs:
        p99_ratios  = [_safe(t.get("p99_deviation_ratio", 0)) for t in tsigs]
        error_rates = [_safe(t.get("error_rate", 0))           for t in tsigs]
        error_pairs = sum(1 for er in error_rates if er > 0.05)
        vec["trace_max_p99_ratio"]        = max(p99_ratios, default=0.0) / 5.0  # norm
        vec["trace_max_error_rate"]       = max(error_rates, default=0.0)
        vec["trace_error_pair_count_norm"]= min(1.0, error_pairs / 5)
        vec["trace_has_data"]             = 1.0
    else:
        vec["trace_max_p99_ratio"]        = 0.0
        vec["trace_max_error_rate"]       = 0.0
        vec["trace_error_pair_count_norm"]= 0.0
        vec["trace_has_data"]             = 0.0

    # Metric features from metric_signatures
    deltas = []
    for m in hist.get("metric_signatures", []):
        before, after = _parse_delta(m.get("delta", "0 -> 0"))
        if before != 0:
            deltas.append(abs(after - before) / abs(before))
    vec["metric_max_delta_ratio"] = min(10.0, max(deltas, default=0.0)) / 10.0

    # Topology depth: use affected services
    affected = hist.get("affected_services", [])
    # Heuristic: stores are deeper (depth 3), api is depth 2, edge is 1
    depth = 1.0
    for s in affected:
        if s in ("payments-db", "catalog-db", "cart-redis", "kafka-events"):
            depth = max(depth, 3.0)
        elif s in ("edge-lb",):
            depth = 1.0
        else:
            depth = max(depth, 2.0)
    vec["topo_alerting_service_depth"] = min(1.0, depth / 4.0)

    # Service one-hot
    aff_set = set(affected)
    for svc, idx in SVC_INDEX.items():
        vec[f"svc_{svc}"] = 1.0 if svc in aff_set else 0.0

    # Raw refs
    vec["_alerting_service"]  = affected[0] if affected else ""
    vec["_affected_services"] = affected
    vec["_log_lines"]         = hist.get("log_signatures", [])
    vec["_trace_pairs"]       = hist.get("trace_signatures", [])

    return vec


def _compute_depth(service: str, topo: dict) -> int:
    """BFS depth from edge-lb to service in topology graph."""
    edges = topo.get("edges", [])
    if not edges or not service:
        return 2  # default mid-depth

    graph: dict[str, list[str]] = {}
    for e in edges:
        graph.setdefault(e["from"], []).append(e["to"])

    visited = {service: 0}
    queue = [service]
    # reverse BFS: find path from any root to service
    # build reverse graph
    rev: dict[str, list[str]] = {}
    for e in edges:
        rev.setdefault(e["to"], []).append(e["from"])

    from collections import deque
    q = deque([(service, 0)])
    seen = {service}
    while q:
        node, d = q.popleft()
        parents = rev.get(node, [])
        if not parents:
            return d  # reached a root
        for p in parents:
            if p not in seen:
                seen.add(p)
                q.append((p, d + 1))
    return 2


def feature_vector(vec: dict) -> list[float]:
    """Return only the numeric dimensions (skip _ prefixed raw refs)."""
    return [v for k, v in vec.items() if not k.startswith("_")]


def feature_keys(vec: dict) -> list[str]:
    return [k for k in vec.keys() if not k.startswith("_")]
