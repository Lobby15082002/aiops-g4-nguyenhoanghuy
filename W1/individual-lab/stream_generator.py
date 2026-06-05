#!/usr/bin/env python3
"""AIOps W1 Individual Lab — Streaming Data Generator.

Continuously POSTs metrics + logs to a student's HTTP endpoint.
Birthday seed determines fault type and timing.

Usage:
    uv run python tooling/stream_generator.py --birthday 1999-05-15 --target http://localhost:8000/ingest
"""

import argparse
import hashlib
import json
import math
import random
import sys
import time
from datetime import datetime, timezone

import requests

# --- Seed logic -----------------------------------------------------------

FAULT_TYPES = ["memory_leak", "traffic_spike", "dependency_timeout"]


def seed_from_birthday(birthday: str) -> int:
    return int(hashlib.sha256(birthday.encode()).hexdigest(), 16)


def compute_fault_params(birthday: str) -> dict:
    """Deterministic fault type + start time from birthday seed."""
    s = seed_from_birthday(birthday)
    rng = random.Random(s)
    fault_type = FAULT_TYPES[s % 3]
    # Fault starts between 30min and 2.5h real-time after generator start
    fault_start_real_seconds = rng.uniform(30 * 60, 150 * 60)
    return {"fault_type": fault_type, "fault_start_real_seconds": fault_start_real_seconds}


# --- Baseline generators --------------------------------------------------

def _noise(rng, scale=1.0):
    return rng.gauss(0, scale)


def generate_baseline(rng, t_prod_hours):
    """Generate normal baseline metrics for production hour t."""
    # Diurnal pattern: traffic peaks at 10h and 20h
    diurnal = 1.0 + 0.4 * math.sin(2 * math.pi * (t_prod_hours - 6) / 24)
    base_rps = 120 * diurnal

    return {
        "memory_usage_bytes": int(800_000_000 + _noise(rng, 20_000_000)),
        "memory_limit_bytes": 2_000_000_000,
        "cpu_usage_percent": round(25 + 15 * diurnal + _noise(rng, 3), 1),
        "http_requests_per_sec": round(base_rps + _noise(rng, 10), 1),
        "http_p99_latency_ms": round(45 + 10 * diurnal + _noise(rng, 5), 1),
        "http_5xx_rate": round(max(0, 0.3 + _noise(rng, 0.2)), 2),
        "jvm_gc_pause_ms_avg": round(max(1, 12 + _noise(rng, 3)), 1),
        "queue_depth": max(0, int(5 + _noise(rng, 2))),
        "upstream_timeout_rate": round(max(0, 0.1 + _noise(rng, 0.1)), 2),
    }


# --- Fault injection -------------------------------------------------------

def inject_memory_leak(metrics, rng, t_since_fault_hours):
    """Gradual memory growth + GC pressure. Detectable within ~1h prod time."""
    progress = min(t_since_fault_hours / 2.0, 1.0)  # full severity at 2h prod
    leak_bytes = int(progress * 1_100_000_000)  # grows toward limit
    metrics["memory_usage_bytes"] = min(
        metrics["memory_usage_bytes"] + leak_bytes,
        metrics["memory_limit_bytes"] - int(abs(_noise(rng, 5_000_000)))
    )
    metrics["jvm_gc_pause_ms_avg"] = round(
        max(1, metrics["jvm_gc_pause_ms_avg"] + progress * 200 + _noise(rng, 8)), 1
    )
    metrics["cpu_usage_percent"] = round(
        min(95, metrics["cpu_usage_percent"] + progress * 35), 1
    )
    if progress > 0.5:
        metrics["http_5xx_rate"] = round(min(50, 3 + progress * 40 + _noise(rng, 3)), 2)
        metrics["http_p99_latency_ms"] = round(metrics["http_p99_latency_ms"] + progress * 900, 1)
    return metrics


def inject_traffic_spike(metrics, rng, t_since_fault_hours):
    """Sudden traffic surge causing latency + queue buildup."""
    # Spike ramps up over 10 min prod time, sustains
    ramp = min(t_since_fault_hours / 0.17, 1.0)  # ~10 min ramp
    multiplier = 1.0 + ramp * 7  # up to 8x traffic
    metrics["http_requests_per_sec"] = round(metrics["http_requests_per_sec"] * multiplier, 1)
    metrics["queue_depth"] = int(metrics["queue_depth"] + ramp * 200 + _noise(rng, 20))
    metrics["http_p99_latency_ms"] = round(
        metrics["http_p99_latency_ms"] + ramp * 1200 + _noise(rng, 50), 1
    )
    metrics["cpu_usage_percent"] = round(min(98, metrics["cpu_usage_percent"] + ramp * 40), 1)
    if ramp > 0.5:
        metrics["http_5xx_rate"] = round(min(60, 3 + ramp * 35 + _noise(rng, 4)), 2)
    return metrics


def inject_dependency_timeout(metrics, rng, t_since_fault_hours):
    """Upstream dependency starts timing out → 5xx cascade + retries."""
    ramp = min(t_since_fault_hours / 0.25, 1.0)  # ~15 min ramp
    metrics["upstream_timeout_rate"] = round(min(80, 5 + ramp * 70 + _noise(rng, 5)), 2)
    metrics["http_5xx_rate"] = round(min(45, 2 + ramp * 35 + _noise(rng, 3)), 2)
    metrics["http_p99_latency_ms"] = round(
        metrics["http_p99_latency_ms"] + ramp * 2500 + _noise(rng, 100), 1
    )
    # Retries cause extra load
    metrics["http_requests_per_sec"] = round(
        metrics["http_requests_per_sec"] * (1 + ramp * 1.5), 1
    )
    metrics["queue_depth"] = int(metrics["queue_depth"] + ramp * 80 + _noise(rng, 10))
    return metrics


FAULT_INJECTORS = {
    "memory_leak": inject_memory_leak,
    "traffic_spike": inject_traffic_spike,
    "dependency_timeout": inject_dependency_timeout,
}


# --- Log generation --------------------------------------------------------

def generate_logs(rng, t_prod_hours, metrics, fault_active, fault_type, timestamp_str):
    """Generate 0-3 log lines per tick."""
    logs = []
    # Always some INFO noise
    if rng.random() < 0.3:
        logs.append({
            "timestamp": timestamp_str,
            "level": "INFO",
            "service": "cart-service",
            "pod": f"cart-service-{rng.randint(1000,9999)}",
            "message": rng.choice([
                "Request processed successfully",
                "Cache hit ratio: {:.0f}%".format(70 + _noise(rng, 10)),
                "Health check OK",
                "Connection pool stats: active={}/50".format(rng.randint(5, 20)),
            ]),
        })

    if not fault_active:
        # Occasional baseline WARN
        if rng.random() < 0.05:
            logs.append({
                "timestamp": timestamp_str,
                "level": "WARN",
                "service": "cart-service",
                "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": rng.choice([
                    "Connection pool nearing limit connections=42/50",
                    "Slow query detected duration_ms=230",
                    "Cache warm-up slower than expected",
                ]),
            })
        return logs

    # Fault-specific logs
    if fault_type == "memory_leak":
        if metrics["jvm_gc_pause_ms_avg"] > 50:
            logs.append({
                "timestamp": timestamp_str, "level": "WARN",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": f"GC pause exceeded threshold pause_ms={metrics['jvm_gc_pause_ms_avg']:.0f}",
            })
        if metrics["memory_usage_bytes"] > 1_600_000_000:
            logs.append({
                "timestamp": timestamp_str, "level": "ERROR",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": "OutOfMemoryWarning: heap usage at {:.0f}%".format(
                    metrics["memory_usage_bytes"] / metrics["memory_limit_bytes"] * 100
                ),
            })
    elif fault_type == "traffic_spike":
        if metrics["queue_depth"] > 50:
            logs.append({
                "timestamp": timestamp_str, "level": "WARN",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": f"Queue depth high depth={metrics['queue_depth']}",
            })
        if metrics["http_5xx_rate"] > 10:
            logs.append({
                "timestamp": timestamp_str, "level": "ERROR",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": "Request rejected: server overloaded",
            })
    elif fault_type == "dependency_timeout":
        if metrics["upstream_timeout_rate"] > 10:
            logs.append({
                "timestamp": timestamp_str, "level": "WARN",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": f"Upstream timeout rate={metrics['upstream_timeout_rate']:.1f}%",
            })
        if metrics["upstream_timeout_rate"] > 40:
            logs.append({
                "timestamp": timestamp_str, "level": "ERROR",
                "service": "cart-service", "pod": f"cart-service-{rng.randint(1000,9999)}",
                "message": "Circuit breaker OPEN for payment-service",
            })

    return logs


# --- Main loop -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AIOps streaming data generator")
    parser.add_argument("--birthday", required=True, help="Student birthday YYYY-MM-DD")
    parser.add_argument("--target", default="http://localhost:8000/ingest",
                        help="Student endpoint URL")
    parser.add_argument("--speed", type=int, default=10,
                        help="Time multiplier (10 = 1 datapoint per 3s = 30s production)")
    args = parser.parse_args()

    params = compute_fault_params(args.birthday)
    fault_type = params["fault_type"]
    fault_start_real = params["fault_start_real_seconds"]

    print(f"[GENERATOR] Birthday: {args.birthday}")
    print(f"[GENERATOR] Fault type: {fault_type}")
    print(f"[GENERATOR] Fault starts at: {fault_start_real/60:.1f} min real-time")
    print(f"[GENERATOR] Target: {args.target}")
    print(f"[GENERATOR] Speed: {args.speed}x (1 POST every {30/args.speed:.1f}s)")
    print("---")

    rng = random.Random(seed_from_birthday(args.birthday))
    interval = 30.0 / args.speed  # seconds between POSTs
    start_real = time.time()
    tick = 0
    fault_injected = False
    fault_announced = False

    while True:
        elapsed_real = time.time() - start_real
        t_prod_seconds = elapsed_real * args.speed
        t_prod_hours = t_prod_seconds / 3600.0

        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        metrics = generate_baseline(rng, t_prod_hours)

        # Check if fault should start
        fault_active = elapsed_real >= fault_start_real
        if fault_active and not fault_announced:
            fault_announced = True
            print(f"\n[FAULT INJECTED] type={fault_type} at t_real={elapsed_real/60:.1f}min "
                  f"t_prod={t_prod_hours:.2f}h")

        if fault_active:
            t_since_fault = (elapsed_real - fault_start_real) * args.speed / 3600.0
            metrics = FAULT_INJECTORS[fault_type](metrics, rng, t_since_fault)

        logs = generate_logs(rng, t_prod_hours, metrics, fault_active, fault_type, timestamp)

        payload = {"timestamp": timestamp, "metrics": metrics, "logs": logs}

        try:
            resp = requests.post(args.target, json=payload, timeout=2)
            if resp.status_code != 200:
                print(f"[WARN] Endpoint returned {resp.status_code}", file=sys.stderr)
        except requests.exceptions.RequestException as e:
            print(f"[WARN] POST failed: {e}", file=sys.stderr)

        tick += 1
        if tick % 20 == 0:
            print(f"[HEARTBEAT] Sent {tick} datapoints | t_prod={t_prod_hours:.2f}h | "
                  f"fault_injected={fault_active}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
