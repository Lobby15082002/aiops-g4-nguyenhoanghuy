"""Validate alert rules against ground-truth incidents.

Approach: replay 7-day log, compute per-minute fail rate, simulate alert firing
under both static baseline rule and student's MWMBR rules, score precision/recall.

Usage:
  uv run python scripts/validate.py \
    --rules burn_rate_alerts.yaml \
    --truth data/incident_window.csv \
    --baseline-rule "error_rate > 0.01 for 5m" \
    --slo-spec slo_spec.yaml \
    --out validation_report.json
"""
import json
import csv
import argparse
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone

START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
SECONDS_PER_BUCKET = 60  # 1-minute buckets


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_incidents(truth_path: Path) -> list:
    out = []
    with truth_path.open() as f:
        r = csv.DictReader(f)
        for row in r:
            out.append({
                "id": int(row["incident_id"]),
                "layer": row["layer"],
                "severity": row["severity"],
                "start": parse_iso(row["start_utc"]),
                "end": parse_iso(row["end_utc"]),
            })
    return out


def build_per_minute_counts(log_path: Path, status_key: str = "status",
                             latency_key: str = "latency_ms") -> dict:
    """Returns {minute_index: {"total": N, "fail": N}}."""
    buckets = {}
    with log_path.open() as f:
        for line in f:
            ev = json.loads(line)
            ts = parse_iso(ev["ts"])
            m = int((ts - START).total_seconds() // 60)
            b = buckets.setdefault(m, {"total": 0, "fail": 0})
            b["total"] += 1
            status = ev.get(status_key)
            if status is not None and (status >= 500 or status == 429):
                b["fail"] += 1
    return buckets


def fail_rate_over_window(buckets: dict, end_minute: int, window_minutes: int) -> float:
    total = 0
    fail = 0
    for m in range(end_minute - window_minutes + 1, end_minute + 1):
        b = buckets.get(m)
        if b:
            total += b["total"]
            fail += b["fail"]
    return fail / total if total else 0.0


def simulate_static_rule(buckets: dict, threshold: float, window_minutes: int,
                         total_minutes: int) -> list:
    """Return list of (start_minute, end_minute) alert firing intervals."""
    intervals = []
    in_alert = False
    start_m = None
    for m in range(window_minutes, total_minutes):
        rate = fail_rate_over_window(buckets, m, window_minutes)
        if rate > threshold:
            if not in_alert:
                start_m = m
                in_alert = True
        else:
            if in_alert:
                intervals.append((start_m, m))
                in_alert = False
    if in_alert:
        intervals.append((start_m, total_minutes))
    return intervals


def parse_mwmbr_rules(rules_yaml: Path, slo_spec_yaml: Path) -> list:
    """Extract simplified MWMBR rules from student YAML.

    Expects each rule to have an `expr` containing structure:
       (fail/total over LONG) / (1 - SLO) >= T
       AND
       (fail/total over SHORT) / (1 - SLO) >= T

    Returns list of dicts: {alert, long_min, short_min, threshold, slo, service}
    """
    rules = yaml.safe_load(rules_yaml.read_text())
    spec = yaml.safe_load(slo_spec_yaml.read_text())
    # Map service name -> SLO target
    slo_by_service = {s["name"]: s["slo"]["target"] for s in spec["services"]}
    parsed = []
    import re

    def to_minutes(s: str) -> int:
        s = s.strip()
        if s.endswith("h"):
            return int(s[:-1]) * 60
        if s.endswith("m"):
            return int(s[:-1])
        if s.endswith("d"):
            return int(s[:-1]) * 1440
        return int(s)

    for group in rules.get("groups", []):
        for rule in group.get("rules", []):
            expr = rule.get("expr", "")
            labels = rule.get("labels", {}) or {}
            severity = labels.get("severity", "page")
            # Find all [<window>] occurrences
            windows = re.findall(r"\[(\d+[mhd])\]", expr)
            windows_min = sorted(set(to_minutes(w) for w in windows))
            # Find threshold (last number after >=)
            thresholds = re.findall(r">=\s*([\d.]+)", expr)
            t = float(thresholds[-1]) if thresholds else 0.0
            # SLO — find in expr "1 - X"
            slo_match = re.search(r"1\s*-\s*([\d.]+)", expr)
            slo_target = float(slo_match.group(1)) if slo_match else 0.999
            service = group.get("name", "").replace("-slo", "").replace("_slo", "")
            if len(windows_min) >= 2:
                parsed.append({
                    "alert": rule["alert"],
                    "long_min": windows_min[-1],
                    "short_min": windows_min[0],
                    "threshold": t,
                    "slo": slo_target,
                    "service": service,
                    "severity": severity,
                })
    return parsed


def simulate_mwmbr_rule(buckets: dict, long_min: int, short_min: int,
                         threshold: float, slo: float, total_minutes: int) -> list:
    intervals = []
    in_alert = False
    start_m = None
    for m in range(long_min, total_minutes):
        long_rate = fail_rate_over_window(buckets, m, long_min)
        short_rate = fail_rate_over_window(buckets, m, short_min)
        burn_long = long_rate / (1 - slo) if slo < 1 else 0
        burn_short = short_rate / (1 - slo) if slo < 1 else 0
        if burn_long >= threshold and burn_short >= threshold:
            if not in_alert:
                start_m = m
                in_alert = True
        else:
            if in_alert:
                intervals.append((start_m, m))
                in_alert = False
    if in_alert:
        intervals.append((start_m, total_minutes))
    return intervals


def score(intervals: list, incidents: list, layer: str) -> dict:
    """Compute TP / FP / FN against incidents for a given layer."""
    relevant = [i for i in incidents if i["layer"] == layer]
    tp = 0
    fp = 0
    fn = 0
    mttds = []
    matched_incidents = set()
    for (s_min, e_min) in intervals:
        s_ts = START + timedelta(minutes=s_min)
        e_ts = START + timedelta(minutes=e_min)
        # Find any incident overlap
        overlapped = False
        for inc in relevant:
            if s_ts < inc["end"] and e_ts > inc["start"]:
                if inc["id"] not in matched_incidents:
                    matched_incidents.add(inc["id"])
                    mttd = (s_ts - inc["start"]).total_seconds()
                    mttds.append(max(0, mttd))
                    tp += 1
                overlapped = True
                break
        if not overlapped:
            fp += 1
    fn = len(relevant) - len(matched_incidents)
    return {
        "fired": len(intervals),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "mttd_p50_s": int(sorted(mttds)[len(mttds)//2]) if mttds else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True, help="burn_rate_alerts.yaml")
    ap.add_argument("--truth", required=True, help="incident_window.csv")
    ap.add_argument("--slo-spec", required=True, help="slo_spec.yaml")
    ap.add_argument("--baseline-threshold", type=float, default=0.005,
                    help="Static baseline alert threshold (fail rate)")
    ap.add_argument("--baseline-window-min", type=int, default=5,
                    help="Static baseline window in minutes")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="validation_report.json")
    args = ap.parse_args()

    incidents = load_incidents(Path(args.truth))
    print(f"Loaded {len(incidents)} ground-truth incidents")

    # Build per-minute buckets for api (primary layer for now)
    api_buckets = build_per_minute_counts(Path(args.data) / "access_log.jsonl")
    total_minutes = 7 * 24 * 60

    # 1. Static baseline
    static_intervals = simulate_static_rule(api_buckets,
                                            threshold=args.baseline_threshold,
                                            window_minutes=args.baseline_window_min,
                                            total_minutes=total_minutes)
    static_score = score(static_intervals, incidents, layer="api")

    # 2. Student MWMBR rules
    parsed = parse_mwmbr_rules(Path(args.rules), Path(args.slo_spec))
    print(f"Parsed {len(parsed)} MWMBR rules from student YAML")

    # Only "page" severity rules count toward noise — ticket-severity rules
    # are advisory (e.g., SLO burn over 3-day window) and don't disturb on-call.
    api_rules = [r for r in parsed if r["service"] == "api" and r["severity"] == "page"]
    student_intervals_union = []
    for r in api_rules:
        ints = simulate_mwmbr_rule(api_buckets, r["long_min"], r["short_min"],
                                    r["threshold"], r["slo"], total_minutes)
        student_intervals_union.extend(ints)
    # Merge overlapping intervals
    student_intervals_union.sort()
    merged = []
    for s, e in student_intervals_union:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    student_score = score(merged, incidents, layer="api")

    noise_reduction = 100.0 * (1 - student_score["fired"] / max(1, static_score["fired"]))
    mttd_delta = (student_score["mttd_p50_s"] or 0) - (static_score["mttd_p50_s"] or 0)

    verdict = "pass" if (
        noise_reduction >= 70 and
        abs(mttd_delta) <= 60 and
        student_score["fn"] == 0
    ) else "needs_review"

    report = {
        "layer": "api",
        "static_baseline": static_score,
        "your_mwmbr": student_score,
        "noise_reduction_pct": round(noise_reduction, 1),
        "mttd_delta_s": int(mttd_delta),
        "rules_count": len(api_rules),
        "verdict": verdict,
    }
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
