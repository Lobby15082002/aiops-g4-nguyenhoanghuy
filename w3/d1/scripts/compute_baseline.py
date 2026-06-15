"""Compute baseline SLI from 7-day generated logs.

Usage:
  uv run python scripts/compute_baseline.py --data data/ --out baseline.json
"""
import json
import argparse
import statistics
from pathlib import Path


def load_jsonl(path: Path):
    with path.open() as f:
        for line in f:
            yield json.loads(line)


def compute_api_sli(log_path: Path) -> dict:
    total = 0
    good = 0          # 2xx/3xx AND latency < 500ms AND not (5xx or 429)
    fail = 0          # 5xx or 429
    latencies = []
    for ev in load_jsonl(log_path):
        total += 1
        status = ev["status"]
        latency = ev["latency_ms"]
        latencies.append(latency)
        if status >= 500 or status == 429:
            fail += 1
        elif 200 <= status < 400 and latency < 500:
            good += 1
        # 4xx (not 429) does not count as fail nor good
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0
    return {
        "events_per_day": int(total / 3),
        "events_total": total,
        "fail_count": fail,
        "success_rate": good / total if total else 0,
        "fail_rate": fail / total if total else 0,
        "latency_p99_ms": p99,
    }


def compute_db_sli(log_path: Path) -> dict:
    total = 0
    good = 0          # success AND duration < 100ms
    durations = []
    fail = 0
    for ev in load_jsonl(log_path):
        total += 1
        durations.append(ev["duration_ms"])
        if not ev["success"]:
            fail += 1
        elif ev["duration_ms"] < 100:
            good += 1
    durations.sort()
    p99 = durations[int(len(durations) * 0.99)] if durations else 0
    return {
        "events_per_day": int(total / 3),
        "events_total": total,
        "fail_count": fail,
        "success_rate": good / total if total else 0,
        "fail_rate": fail / total if total else 0,
        "duration_p99_ms": p99,
    }


def compute_frontend_sli(log_path: Path) -> dict:
    total = 0
    good = 0          # dom_ready < 3000 AND no js_error AND no network_error
    fail = 0
    doms = []
    for ev in load_jsonl(log_path):
        total += 1
        doms.append(ev["dom_ready_ms"])
        is_fail = ev["js_error"] or ev["network_error"] or ev["dom_ready_ms"] >= 3000
        if is_fail:
            fail += 1
        else:
            good += 1
    doms.sort()
    p99 = doms[int(len(doms) * 0.99)] if doms else 0
    return {
        "events_per_day": int(total / 3),
        "events_total": total,
        "fail_count": fail,
        "success_rate": good / total if total else 0,
        "fail_rate": fail / total if total else 0,
        "dom_ready_p99_ms": p99,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data", help="path to data/ dir")
    ap.add_argument("--out", default="baseline.json")
    args = ap.parse_args()

    data = Path(args.data)
    print(f"Reading from {data.resolve()}...")
    out = {
        "api": compute_api_sli(data / "access_log.jsonl"),
        "db": compute_db_sli(data / "db_query_log.jsonl"),
        "frontend": compute_frontend_sli(data / "frontend_rum.jsonl"),
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nbaseline.json written:")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
