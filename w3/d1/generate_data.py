"""Generate 3-day synthetic logs for W3-D1 SLO assignment.

Sampled traffic (1-15 req/s, sampled from real ~50-500 req/s) to keep files small.

Output (~40 MB total):
  data/access_log.jsonl       — ~1.5M sampled API events
  data/db_query_log.jsonl     — ~170k DB query samples
  data/frontend_rum.jsonl     — ~260k RUM page-load events
  data/topology.yaml          — service map
  data/incident_window.csv    — 5 ground-truth incidents
"""
import json
import random
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta

random.seed(42)

OUT = Path(__file__).parent / "data"
OUT.mkdir(exist_ok=True)

START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
DAYS = 3
SECONDS = DAYS * 86400

# Sampled traffic shape: diurnal — peak 15 evt/s at hour 14, low 1 evt/s at hour 3
def rps_at(ts: datetime) -> float:
    h = ts.hour + ts.minute / 60
    diurnal = 0.5 + 0.5 * math.sin((h - 8) * math.pi / 12)  # peak around hour 14
    return 1 + 14 * max(0, diurnal)


# Ground-truth incidents (in 3-day window)
INCIDENTS = [
    # (start_offset_minutes, duration_minutes, layer, severity, fail_rate_multiplier)
    (180,   8,   "api",       "tier1",  100),   # day 1 hour 3:00 — short total outage
    (1490,  35,  "db",        "tier2",  20),    # day 2 hour 0:50 — slow DB queries
    (2820,  12,  "api",       "tier1",  50),    # day 2 hour 23:00 — partial 5xx burst
    (3120,  90,  "frontend",  "tier2",  8),     # day 3 hour 4:00 — CDN slow region
    (4100,  20,  "api",       "tier2",  10),    # day 3 hour 20:20 — moderate 5xx
]

# Convert to absolute UTC start/end and write ground truth
def write_incident_csv():
    path = OUT / "incident_window.csv"
    with path.open("w") as f:
        f.write("incident_id,layer,severity,start_utc,end_utc,fail_rate_multiplier\n")
        for i, (offset, dur, layer, sev, mult) in enumerate(INCIDENTS, 1):
            s = START + timedelta(minutes=offset)
            e = s + timedelta(minutes=dur)
            f.write(f"{i},{layer},{sev},{s.isoformat()},{e.isoformat()},{mult}\n")

def in_incident(ts: datetime, layer: str) -> int:
    """Return fail-rate multiplier if ts is inside an incident for this layer, else 1."""
    for offset, dur, l, sev, mult in INCIDENTS:
        s = START + timedelta(minutes=offset)
        e = s + timedelta(minutes=dur)
        if l == layer and s <= ts < e:
            return mult
    return 1

def gen_access_log():
    """Nginx access log — service = api."""
    path = OUT / "access_log.jsonl"
    paths = ["/api/orders", "/api/cart", "/api/user", "/api/products", "/api/checkout"]
    methods = ["GET", "POST", "PUT", "DELETE"]
    count = 0
    with path.open("w") as f:
        t = 0
        while t < SECONDS:
            ts = START + timedelta(seconds=t)
            rate = rps_at(ts)
            mult = in_incident(ts, "api")
            base_5xx = 0.001          # 0.1% baseline 5xx
            base_429 = 0.0005
            base_4xx = 0.02           # bots/scrapers
            base_p99_latency = 180
            # During incident: replace baseline with elevated rate (clamped)
            if mult > 1:
                err_rate = min(0.95, mult * 0.02)   # mult=100 → 95%, mult=50 → 95%, mult=10 → 20%
                latency_p99 = base_p99_latency * (1 + min(mult, 30) * 0.3)
            else:
                err_rate = base_5xx
                latency_p99 = base_p99_latency
            n_this_sec = int(round(rate))
            for _ in range(n_this_sec):
                r = random.random()
                if r < err_rate:
                    status = 500 + random.choice([0, 2, 3])
                elif r < err_rate + base_429:
                    status = 429
                elif r < err_rate + base_429 + base_4xx:
                    status = random.choice([400, 401, 403, 404])
                else:
                    status = random.choice([200, 200, 200, 200, 201, 204, 301, 302])
                latency_ms = int(random.lognormvariate(math.log(max(latency_p99 / 4, 30)), 0.5))
                f.write(json.dumps({
                    "ts": ts.isoformat(),
                    "method": random.choice(methods),
                    "path": random.choice(paths),
                    "status": status,
                    "latency_ms": latency_ms,
                }) + "\n")
                count += 1
            t += 1
    print(f"access_log.jsonl: {count} events")

def gen_db_query_log():
    """Postgres query log sample — ~1 sample per 1.5s."""
    path = OUT / "db_query_log.jsonl"
    queries = [
        "SELECT * FROM orders WHERE user_id = $1",
        "SELECT COUNT(*) FROM cart",
        "UPDATE inventory SET stock = stock - 1 WHERE id = $1",
        "INSERT INTO events (...) VALUES (...)",
        "SELECT p.* FROM products p JOIN cat c ON ...",
    ]
    count = 0
    with path.open("w") as f:
        t = 0
        while t < SECONDS:
            ts = START + timedelta(seconds=t)
            mult = in_incident(ts, "db")
            base_p99 = 45
            base_fail = 0.0001
            fail_rate = min(0.5, base_fail * mult)
            p99 = base_p99 * (1 + (mult - 1) * 0.5)
            # 1 sample per ~1.5s
            r = random.random()
            if r < 1 / 1.5:
                dur = int(random.lognormvariate(math.log(max(p99 / 4, 5)), 0.6))
                success = random.random() > fail_rate
                f.write(json.dumps({
                    "ts": ts.isoformat(),
                    "query": random.choice(queries),
                    "duration_ms": dur,
                    "success": success,
                    "rows": random.randint(0, 1000) if success else 0,
                }) + "\n")
                count += 1
            t += 1
    print(f"db_query_log.jsonl: {count} events")

def gen_frontend_rum():
    """RUM page-load events — 1 per ~0.5s."""
    path = OUT / "frontend_rum.jsonl"
    pages = ["/", "/products", "/cart", "/checkout", "/order/confirm"]
    count = 0
    with path.open("w") as f:
        t = 0
        while t < SECONDS:
            ts = START + timedelta(seconds=t)
            mult = in_incident(ts, "frontend")
            base_dom_ready = 1200
            base_js_err = 0.008
            js_err_rate = min(0.4, base_js_err * mult)
            dom_p99 = base_dom_ready * (1 + (mult - 1) * 0.2)
            # 2 events per second avg
            for _ in range(2):
                dom_ready = int(random.lognormvariate(math.log(max(dom_p99 / 3, 200)), 0.5))
                js_error = random.random() < js_err_rate
                network_error = random.random() < (js_err_rate * 0.5)
                f.write(json.dumps({
                    "ts": ts.isoformat(),
                    "page": random.choice(pages),
                    "dom_ready_ms": dom_ready,
                    "js_error": js_error,
                    "network_error": network_error,
                }) + "\n")
                count += 1
            t += 1
    print(f"frontend_rum.jsonl: {count} events")

def gen_topology():
    path = OUT / "topology.yaml"
    path.write_text("""version: 1
services:
  - name: frontend
    type: cdn_spa
    depends_on: [api]
  - name: api
    type: rest_api
    depends_on: [db]
    instances: 4
  - name: db
    type: postgres
    depends_on: []
    primary_replica: true
""")

def main():
    print(f"Generating W3-D1 synthetic data ({DAYS} days, sampled traffic)...")
    write_incident_csv()
    print(f"incident_window.csv: {len(INCIDENTS)} ground-truth incidents")
    gen_topology()
    gen_access_log()
    gen_db_query_log()
    gen_frontend_rum()
    print(f"\nAll files written to {OUT.resolve()}")

if __name__ == "__main__":
    main()
