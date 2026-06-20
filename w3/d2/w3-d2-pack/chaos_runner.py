#!/usr/bin/env python3
"""
chaos_runner.py — W3-D2 Chaos Engineering Lab
Inject fault qua HTTP POST /fault + POST /events,
query AIOps pipeline, lưu chaos_results.json, in scoreboard §8.6.

Usage:
    python chaos_runner.py [--dry-run] [--exp-id 1,2,3]
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import yaml
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
PIPELINE_URL = "http://localhost:8000"
COOLDOWN_SEC = 120
RESULTS_FILE = "chaos_results.json"

SVC_PORT = {
    "payment-svc":   8101,
    "inventory-svc": 8102,
    "api-gateway":   8080,
    "payment-db":    8108,
    "auth-svc":      8104,
    "log-collector": 8105,
    "frontend":      8100,
    "checkout-svc":  8103,
    "dns-resolver":  8106,
    "cache-svc":     8107,
}

# fault_type → payload gửi vào POST /fault của service mock
FAULT_PAYLOAD = {
    "latency":           {"type": "latency",        "latency_ms": 500},
    "network_loss":      {"type": "network_loss",   "loss_percent": 30},
    "availability":      {"type": "availability"},
    "cpu_saturation":    {"type": "cpu_saturation"},
    "memory":            {"type": "memory"},
    "disk_fill":         {"type": "disk_fill"},
    "time_skew":         {"type": "time_skew"},
    "network_partition": {"type": "network_partition"},
    "dns_latency":       {"type": "dns_latency",    "latency_ms": 2000},
    "cascade_retry":     {"type": "http_error",     "error_percent": 20},
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _post(url: str, payload: dict, timeout: int = 10) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ── Fault injection ────────────────────────────────────────────────────────────
def _resolve_svc(target: str) -> str:
    """'frontend ↔ api-gateway' → 'frontend'"""
    return target.split("↔")[0].strip() if "↔" in target else target


def inject_fault(exp: dict, dry_run: bool = False) -> float:
    fault_type = exp["fault_type"]
    svc        = _resolve_svc(exp["target"])
    dur        = exp["blast_radius"].get("duration_seconds", 60)
    port       = SVC_PORT.get(svc)

    fault_payload = {**FAULT_PAYLOAD.get(fault_type, {"type": fault_type}),
                     "duration_seconds": dur}

    inject_ts = time.time()

    if dry_run:
        print(f"  [dry] POST localhost:{port}/fault  {fault_payload}")
        print(f"  [dry] POST {PIPELINE_URL}/events")
        return inject_ts

    # 1. Inject vào service
    if port:
        try:
            resp = _post(f"http://localhost:{port}/fault", fault_payload)
            print(f"  [inject] → {svc}:{port}/fault  type={resp.get('fault',{}).get('type')}")
        except Exception as e:
            print(f"  [WARN] /fault failed ({svc}): {e}")
    else:
        print(f"  [WARN] no port for '{svc}'")

    # 2. Báo pipeline biết có event
    event_payload = {
        "id":               exp["id"],
        "name":             exp["name"],
        "fault_type":       fault_type,
        "target":           svc,
        "duration_seconds": dur,
        "start_ts":         int(inject_ts),
    }
    try:
        resp = _post(f"{PIPELINE_URL}/events", event_payload)
        print(f"  [pipeline] /events → event_id={resp.get('event',{}).get('event_id')}")
    except Exception as e:
        print(f"  [WARN] /events failed: {e}")

    return inject_ts


def rollback_fault(exp: dict, dry_run: bool = False) -> None:
    svc  = _resolve_svc(exp["target"])
    port = SVC_PORT.get(svc)
    if not port:
        return
    if dry_run:
        print(f"  [dry] POST localhost:{port}/clear_fault")
        return
    try:
        resp = _post(f"http://localhost:{port}/clear_fault", {})
        print(f"  [rollback] {svc} → {resp.get('status')}")
    except Exception as e:
        print(f"  [WARN] /clear_fault failed: {e}")


# ── Pipeline query ─────────────────────────────────────────────────────────────
def query_pipeline(inject_ts: float, dur: int) -> dict:
    result = {
        "detected":       False,
        "mttd_seconds":   None,
        "rca_service":    None,
        "rca_confidence": None,
        "false_alarms":   0,
    }

    # /alerts — lấy từ 60s trước inject để đếm false alarm baseline
    try:
        alerts = _get(f"{PIPELINE_URL}/alerts?since={int(inject_ts - 60)}")
        if isinstance(alerts, dict):
            alerts = alerts.get("alerts", [])
    except Exception as e:
        print(f"  [pipeline] /alerts error: {e}")
        return result

    fault_alerts    = [a for a in alerts if a.get("fire_ts", 0) >= inject_ts]
    baseline_alerts = [a for a in alerts if a.get("fire_ts", 0) < inject_ts]
    result["false_alarms"] = len(baseline_alerts)

    if not fault_alerts:
        return result  # FN

    result["detected"]     = True
    result["mttd_seconds"] = round(
        min(a.get("fire_ts", inject_ts) for a in fault_alerts) - inject_ts, 1
    )

    # /rca
    try:
        rca = _post(f"{PIPELINE_URL}/rca", {
            "window_start": int(inject_ts - 10),
            "window_end":   int(inject_ts + dur + 30),
        })
        result["rca_service"]    = rca.get("root_service")
        result["rca_confidence"] = rca.get("confidence")
    except Exception as e:
        print(f"  [pipeline] /rca error: {e}")

    return result


# ── Run one experiment ─────────────────────────────────────────────────────────
def run_experiment(exp: dict, dry_run: bool = False) -> dict:
    eid    = exp["id"]
    name   = exp["name"]
    dur    = exp["blast_radius"].get("duration_seconds", 60)
    gt_svc = exp["ground_truth"]["root_service"]

    print(f"\n{'─'*62}")
    print(f"[Exp {eid:>2}] {name}")
    print(f"  fault={exp['fault_type']}  target={exp['target']}  dur={dur}s  gt={gt_svc}")

    result = {
        "id":                eid,
        "name":              name,
        "fault_type":        exp["fault_type"],
        "ground_truth_root": gt_svc,
        "detected":          False,
        "mttd_seconds":      None,
        "rca_service":       None,
        "rca_confidence":    None,
        "false_alarms":      0,
        "error":             None,
    }

    inject_ts = inject_fault(exp, dry_run=dry_run)

    # Đợi đủ lâu để pipeline fire alert (ít nhất 35s)
    wait = max(35, dur // 2)
    print(f"  [wait] {wait}s …")
    if not dry_run:
        time.sleep(wait)

    print("  [query] pipeline …")
    if not dry_run:
        result.update(query_pipeline(inject_ts, dur))
    else:
        import random
        result.update({
            "detected":       random.random() > 0.15,
            "mttd_seconds":   round(random.uniform(3, 35), 1),
            "rca_service":    gt_svc if random.random() > 0.2 else "unknown-svc",
            "rca_confidence": round(random.uniform(0.7, 0.95), 2),
            "false_alarms":   0,
        })

    ok = result.get("rca_service") == gt_svc
    print(f"  [result] detected={result['detected']}  mttd={result['mttd_seconds']}s  "
          f"rca={result['rca_service']} ({'✓' if ok else '✗'} expected={gt_svc})")

    rollback_fault(exp, dry_run=dry_run)
    return result


# ── Scoreboard ─────────────────────────────────────────────────────────────────
def print_scoreboard(results: list[dict]) -> None:
    total    = len(results)
    detected = [r for r in results if r.get("detected")]
    n_det    = len(detected)
    fp_total = sum(r.get("false_alarms", 0) for r in results)
    tp, fn   = n_det, total - n_det
    precision  = tp / (tp + fp_total) if (tp + fp_total) > 0 else 0.0
    recall     = tp / (tp + fn)       if (tp + fn) > 0       else 0.0
    rca_ok     = sum(1 for r in detected if r.get("rca_service") == r.get("ground_truth_root"))
    mttds      = sorted(r["mttd_seconds"] for r in detected if r.get("mttd_seconds") is not None)
    p50 = mttds[len(mttds) // 2]        if mttds else None
    p95 = mttds[int(len(mttds) * .95)]  if mttds else None

    print("\n" + "=" * 64)
    print("==== Chaos Run ====")
    print(f"Total: {total}")
    print(f"Detected: {n_det}/{total}")
    print(f"RCA correct: {rca_ok}/{n_det}")
    print(f"False alarms in baseline windows: {fp_total}")
    print(f"Precision: {precision:.2f}")
    print(f"Recall:    {recall:.2f}")
    print(f"MTTD p50: {p50}s, p95: {p95}s")

    hdr = ("| # | name                                     "
           "| detected | mttd    | rca_service          | rca_correct |")
    sep = ("|---|------------------------------------------|"
           "----------|---------|----------------------|-------------|")
    print("\nPer-experiment:")
    print(hdr)
    print(sep)

    gaps = []
    for r in results:
        det  = "Y" if r.get("detected") else "N"
        mttd = f"{r['mttd_seconds']}s" if r.get("mttd_seconds") is not None else "—"
        rsvc = r.get("rca_service") or "—"
        corr = r.get("rca_service") == r.get("ground_truth_root")
        rc   = "Y" if corr else ("N" if r.get("rca_service") else "—")
        print(f"| {r['id']:>1} | {r['name']:<40} | {det:^8} | {mttd:^7} | {rsvc:<20} | {rc:^11} |")
        if not r.get("detected"):
            gaps.append(f"- Exp {r['id']} ({r['name']}): FN — detector miss")
        elif not corr and r.get("rca_service"):
            gaps.append(f"- Exp {r['id']} ({r['name']}): RCA='{r['rca_service']}' ≠ '{r['ground_truth_root']}'")

    print()
    if gaps:
        print("Gaps identified:")
        for g in gaps:
            print(g)
    else:
        print("Gaps identified: none")
    print("=" * 64 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--exp-id",      default="")
    parser.add_argument("--experiments", default="experiments.yaml")
    args = parser.parse_args()

    exp_file = Path(args.experiments)
    if not exp_file.exists():
        print(f"ERROR: {exp_file} not found.")
        sys.exit(1)

    with open(exp_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    experiments = data.get("experiments", [])

    if args.exp_id:
        ids = {int(x) for x in args.exp_id.split(",")}
        experiments = [e for e in experiments if e["id"] in ids]

    if not experiments:
        print("No experiments to run.")
        sys.exit(0)

    print(f"\n{'='*64}")
    print(f"W3-D2 Chaos Run  dry={args.dry_run}  exps={len(experiments)}  cooldown={COOLDOWN_SEC}s")
    print(f"{'='*64}")

    all_results = []
    for i, exp in enumerate(experiments):
        r = run_experiment(exp, dry_run=args.dry_run)
        all_results.append(r)
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2)

        if i < len(experiments) - 1:
            print(f"\n  [cooldown] {COOLDOWN_SEC}s …")
            if not args.dry_run:
                time.sleep(COOLDOWN_SEC)
            else:
                print("  [dry-run] skip cooldown")

    print_scoreboard(all_results)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    print(f"Results → {RESULTS_FILE}")


if __name__ == "__main__":
    main()