"""
W2-D3: Model Serving — AIOps Pipeline as HTTP Service
POST /incident  → correlate alerts → RCA → LLM enrichment → JSON response
GET  /healthz   → liveness probe
GET  /readyz    → readiness probe (check graph + history loaded)
GET  /version   → pipeline config info
GET  /metrics   → Prometheus metrics
"""

import json
import logging
import os
import time
import asyncio
import hashlib
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Optional

import networkx as nx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from cachetools import TTLCache

# ── Prometheus (optional, graceful fallback) ──────────────────────────────────
try:
    from prometheus_client import Counter, Histogram, make_asgi_app
    PROMETHEUS_OK = True
    REQUEST_TOTAL = Counter(
        "aiops_incident_requests_total", "Total requests", ["status"]
    )
    REQUEST_LATENCY = Histogram(
        "aiops_incident_latency_seconds", "Request latency"
    )
    LLM_FAILURES = Counter(
        "aiops_llm_failures_total", "LLM call failures", ["reason"]
    )
    CLUSTERS_PER_REQUEST = Histogram(
        "aiops_clusters_per_request", "Clusters output per request"
    )
except ImportError:
    PROMETHEUS_OK = False

# ── Groq LLM (optional, graceful fallback) ────────────────────────────────────
try:
    from groq import Groq
    GROQ_CLIENT = Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
    GROQ_OK = True
except ImportError:
    GROQ_OK = False
    GROQ_CLIENT = None

# ── Logging JSON ──────────────────────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "extra"):
            log.update(record.extra)
        return json.dumps(log, ensure_ascii=False)

handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("aiops")
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Dataset paths ─────────────────────────────────────────────────────────────
BASE = Path(r"D:\Xbrain\Phase 2 AIops\w2\d2\dataset")
SERVICES_JSON   = BASE / "services.json"
HISTORY_JSON    = BASE / "incidents_history.json"

# ── Module-level state (load once at import) ──────────────────────────────────
def _load_graph() -> nx.DiGraph:
    with open(SERVICES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    service_names = {s["name"] for s in data["services"]}
    g = nx.DiGraph()
    g.add_nodes_from(service_names)
    for edge in data["edges"]:
        src, dst = edge["from"], edge["to"]
        if src in service_names and dst in service_names:
            g.add_edge(src, dst)
    return g

def _load_history():
    with open(HISTORY_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return data["incidents"]

GRAPH: nx.DiGraph = _load_graph()
HISTORY: list     = _load_history()

# LLM response cache: hash(prompt) → response string, TTL 1 hour, max 1000
_LLM_CACHE: TTLCache = TTLCache(maxsize=1000, ttl=3600)

# Feature flag: set AIOPS_USE_LLM=false to bypass LLM
USE_LLM = os.environ.get("AIOPS_USE_LLM", "true").lower() != "false"

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class Alert(BaseModel):
    id:        str
    ts:        str
    service:   str
    metric:    str
    severity:  str
    value:     float
    threshold: float
    labels:    dict = Field(default_factory=dict)

    @field_validator("severity")
    @classmethod
    def severity_valid(cls, v):
        if v not in {"info", "warn", "crit"}:
            raise ValueError(f"severity must be info/warn/crit, got '{v}'")
        return v

class IncidentRequest(BaseModel):
    alerts: list[Alert]

class RootCauseInfo(BaseModel):
    service:    str
    confidence: float
    root_class: str
    method:     str

class IncidentResponse(BaseModel):
    clusters:            list[dict]
    root_cause:          RootCauseInfo
    recommended_actions: list[str]
    similar_incidents:   list[str]
    llm_summary:         Optional[str] = None
    latency_ms:          Optional[float] = None

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="AIOps Incident Pipeline",
    version="1.0.0",
    description="W2-D3: Correlate → RCA → LLM enrichment",
)

# Mount Prometheus /metrics endpoint
if PROMETHEUS_OK:
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

# ── Latency middleware ────────────────────────────────────────────────────────
@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    logger.info(
        "request",
        extra={
            "extra": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 1),
            }
        },
    )
    return response

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1: CORRELATE  (from W2-D1)
# ═══════════════════════════════════════════════════════════════════════════════

SEVERITY_RANK = {"info": 0, "warn": 1, "crit": 2}

def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

def _fingerprint(alert: dict) -> str:
    return f"{alert['service']}|{alert['metric']}|{alert['severity']}"

def _max_severity(group: list) -> str:
    return max(
        (a["severity"] for a in group),
        key=lambda s: SEVERITY_RANK.get(s, 0),
    )

def _session_groups(alerts: list, gap_sec: int = 120) -> list:
    if not alerts:
        return []
    sorted_a = sorted(alerts, key=lambda a: a["ts"])
    groups = [[sorted_a[0]]]
    for alert in sorted_a[1:]:
        last_ts = _parse_ts(groups[-1][-1]["ts"])
        curr_ts = _parse_ts(alert["ts"])
        if (curr_ts - last_ts).total_seconds() <= gap_sec:
            groups[-1].append(alert)
        else:
            groups.append([alert])
    return groups

def _topology_group(alerts: list, graph: nx.DiGraph, max_hop: int = 2) -> list:
    undirected = graph.to_undirected()
    by_service = defaultdict(list)
    for a in alerts:
        by_service[a["service"]].append(a)
    services = list(by_service.keys())
    parent = {s: s for s in services}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i, s1 in enumerate(services):
        for s2 in services[i + 1:]:
            if not graph.has_node(s1) or not graph.has_node(s2):
                continue
            try:
                dist = nx.shortest_path_length(undirected, s1, s2)
                if dist <= max_hop:
                    union(s1, s2)
            except nx.NetworkXNoPath:
                pass

    groups = defaultdict(list)
    for s in services:
        groups[find(s)].extend(by_service[s])
    return list(groups.values())

def correlate(alerts: list, graph: nx.DiGraph, gap_sec: int = 45, max_hop: int = 2) -> list:
    sessions = _session_groups(alerts, gap_sec=gap_sec)
    clusters = []
    for s_idx, session_alerts in enumerate(sessions):
        topo_groups = _topology_group(session_alerts, graph, max_hop=max_hop)
        for g_idx, group in enumerate(topo_groups):
            clusters.append({
                "cluster_id":  f"c-{s_idx:03d}-{g_idx:03d}",
                "alert_count": len(group),
                "services":    sorted({a["service"] for a in group}),
                "time_range": [
                    min(a["ts"] for a in group),
                    max(a["ts"] for a in group),
                ],
                "max_severity": _max_severity(group),
                "fingerprints": sorted({_fingerprint(a) for a in group}),
            })
    return clusters

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2: RCA  (from W2-D2)
# ═══════════════════════════════════════════════════════════════════════════════

VALID_CLASSES = {
    "connection_pool_exhaustion", "slow_query", "memory_leak",
    "rebalance_storm", "deadlock", "network_partition", "bad_deploy",
    "config_push", "tls_expiry", "ddos", "other",
}

def _build_service_first_alert(alerts: list) -> dict:
    sfa = {}
    for alert in alerts:
        svc = alert["service"]
        ts  = _parse_ts(alert["ts"])
        if svc not in sfa or ts < sfa[svc]:
            sfa[svc] = ts
    return sfa

def _graph_rca(cluster: dict, graph: nx.DiGraph, service_first_alert: dict, top_k: int = 3) -> list:
    alert_services = set(cluster["services"])
    subgraph = graph.subgraph(alert_services).copy()

    if len(alert_services) == 1:
        return [[list(alert_services)[0], 1.0]]

    pagerank_scores = nx.pagerank(subgraph, alpha=0.85)
    max_pr = max(pagerank_scores.values()) if pagerank_scores else 1.0
    pagerank_norm = {s: v / max_pr for s, v in pagerank_scores.items()}

    times = {s: service_first_alert[s] for s in alert_services if s in service_first_alert}
    if times:
        t_min, t_max = min(times.values()), max(times.values())
        t_range = (t_max - t_min).total_seconds()
        if t_range == 0:
            ts_score = {s: 1.0 for s in alert_services}
        else:
            ts_score = {
                s: 1.0 - (times[s] - t_min).total_seconds() / t_range
                if s in times else 0.5
                for s in alert_services
            }
    else:
        ts_score = {s: 0.5 for s in alert_services}

    combined = {
        s: round(0.4 * pagerank_norm.get(s, 0.0) + 0.6 * ts_score.get(s, 0.0), 4)
        for s in alert_services
    }
    ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    return [[svc, score] for svc, score in ranked[:top_k]]

def _retrieve_similar(cluster: dict, incidents: list, top_k: int = 3, min_score: float = 0.2) -> list:
    cluster_services = set(cluster["services"])
    sev_map = {"crit": "high", "warn": "medium", "info": "low",
               "high": "high", "medium": "medium", "low": "low", "critical": "high"}
    cluster_sev = sev_map.get(cluster["max_severity"], "medium")

    scored = []
    for inc in incidents:
        score = 0.0
        if inc.get("root_cause_service") in cluster_services:
            score += 0.4
        overlap = len(cluster_services & set(inc.get("services_involved", [])))
        score += min(overlap * 0.2, 0.4)
        if sev_map.get(inc.get("severity", ""), "medium") == cluster_sev:
            score += 0.2
        if score >= min_score:
            scored.append((inc, round(score, 2)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]

def _classify(similar_incidents: list) -> tuple:
    if not similar_incidents:
        return "other", ["Investigate manually"]
    top_inc, _ = similar_incidents[0]
    root_class = top_inc.get("root_cause_class", "other")
    if root_class not in VALID_CLASSES:
        root_class = "other"
    remediation = top_inc.get("remediation", "")
    actions = [remediation] if remediation else ["Investigate manually"]
    return root_class, actions

def run_rca(primary_cluster: dict, alerts: list, graph: nx.DiGraph, history: list) -> dict:
    sfa     = _build_service_first_alert(alerts)
    top3    = _graph_rca(primary_cluster, graph, sfa, top_k=3)
    similar = _retrieve_similar(primary_cluster, history, top_k=3)
    root_class, actions = _classify(similar)

    return {
        "root_cause":        top3[0][0],
        "confidence":        top3[0][1],
        "root_class":        root_class,
        "actions":           actions,
        "similar_incidents": [inc["id"] for inc, _ in similar],
        "method":            "graph+retrieval" if similar else "graph-only-fallback",
    }

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 3: LLM ENRICHMENT  (Groq)
# ═══════════════════════════════════════════════════════════════════════════════

async def call_llm_enrichment(rca: dict, cluster: dict) -> Optional[str]:
    """Call Groq to generate a concise incident summary. Cached + timeout."""
    if not USE_LLM or not GROQ_OK:
        return None

    prompt = (
        f"You are an SRE. Summarize this incident in 2 sentences for an on-call engineer.\n"
        f"Root cause service: {rca['root_cause']}\n"
        f"Root cause class: {rca['root_class']}\n"
        f"Affected services: {', '.join(cluster['services'])}\n"
        f"Severity: {cluster['max_severity']}\n"
        f"Recommended action: {rca['actions'][0] if rca['actions'] else 'N/A'}\n"
        f"Respond in English only. Be concise."
    )

    # Cache hit
    cache_key = hashlib.sha256(prompt.encode()).hexdigest()
    if cache_key in _LLM_CACHE:
        return _LLM_CACHE[cache_key]

    try:
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: GROQ_CLIENT.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=150,
                    temperature=0.3,
                ),
            ),
            timeout=8.0,
        )
        summary = response.choices[0].message.content.strip()
        _LLM_CACHE[cache_key] = summary
        return summary

    except asyncio.TimeoutError:
        if PROMETHEUS_OK:
            LLM_FAILURES.labels(reason="timeout").inc()
        logger.warning("LLM call timed out")
        return None
    except Exception as e:
        if PROMETHEUS_OK:
            LLM_FAILURES.labels(reason="error").inc()
        logger.warning(f"LLM call failed: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

async def process_batch(alerts: list) -> dict:
    """Chain Layer1 → Layer2 → Layer3."""
    alert_dicts = [a.model_dump() for a in alerts]

    # Layer 1: Correlate
    clusters = correlate(alert_dicts, GRAPH, gap_sec=45, max_hop=2)
    if not clusters:
        return {
            "clusters":            [],
            "root_cause":          {"service": "unknown", "confidence": 0.0,
                                    "root_class": "other", "method": "no-clusters"},
            "recommended_actions": ["No clusters formed — check alert data"],
            "similar_incidents":   [],
            "llm_summary":         None,
        }

    # Layer 2: RCA on largest cluster
    primary = max(clusters, key=lambda c: c["alert_count"])
    rca = run_rca(primary, alert_dicts, GRAPH, HISTORY)

    # Layer 3: LLM enrichment (async, skip if flag off or Groq unavailable)
    llm_summary = await call_llm_enrichment(rca, primary)

    if PROMETHEUS_OK:
        CLUSTERS_PER_REQUEST.observe(len(clusters))

    return {
        "clusters":            clusters,
        "root_cause":          {
            "service":    rca["root_cause"],
            "confidence": rca["confidence"],
            "root_class": rca["root_class"],
            "method":     rca["method"],
        },
        "recommended_actions": rca["actions"],
        "similar_incidents":   rca["similar_incidents"],
        "llm_summary":         llm_summary,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/healthz", tags=["ops"])
async def healthz():
    """Liveness probe — process còn sống không."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz():
    """Readiness probe — graph + history đã load chưa."""
    checks = {}
    ok = True

    checks["graph"] = GRAPH.number_of_nodes() > 0
    checks["history"] = len(HISTORY) > 0

    if not all(checks.values()):
        ok = False

    if not ok:
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", "checks": checks}


@app.get("/version", tags=["ops"])
async def version():
    """Pipeline version + config info."""
    return {
        "app": "1.0.0",
        "graph_nodes": GRAPH.number_of_nodes(),
        "graph_edges": GRAPH.number_of_edges(),
        "history_size": len(HISTORY),
        "pipeline_config": {
            "gap_sec":    45,
            "max_hop":    2,
            "rca_method": "pagerank+timestamp",
            "llm_model":  "llama-3.1-8b-instant" if (USE_LLM and GROQ_OK) else "disabled",
        },
        "use_llm": USE_LLM and GROQ_OK,
    }


@app.post("/incident", response_model=IncidentResponse, tags=["pipeline"])
async def post_incident(request: IncidentRequest):
    """
    Main endpoint: nhận batch alerts → trả incident report.
    """
    if not request.alerts:
        raise HTTPException(status_code=400, detail="alerts list is empty")

    t0 = time.perf_counter()
    try:
        result = await process_batch(request.alerts)
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        if PROMETHEUS_OK:
            REQUEST_TOTAL.labels(status="error").inc()
        raise HTTPException(status_code=500, detail="Internal pipeline error")

    latency_ms = (time.perf_counter() - t0) * 1000

    if PROMETHEUS_OK:
        REQUEST_TOTAL.labels(status="success").inc()
        REQUEST_LATENCY.observe(latency_ms / 1000)

    logger.info(
        "Processed incident",
        extra={
            "extra": {
                "cluster_count": len(result["clusters"]),
                "root_cause":    result["root_cause"]["service"],
                "confidence":    result["root_cause"]["confidence"],
                "latency_ms":    round(latency_ms, 1),
            }
        },
    )

    return IncidentResponse(
        clusters=result["clusters"],
        root_cause=RootCauseInfo(**result["root_cause"]),
        recommended_actions=result["recommended_actions"],
        similar_incidents=result["similar_incidents"],
        llm_summary=result["llm_summary"],
        latency_ms=round(latency_ms, 1),
    )