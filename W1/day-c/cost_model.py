"""
Cost Model — W1/D3 Phase 2
===========================
Estimates monthly cost for observability pipeline at 3 scale tiers.
Compares self-hosted (build) vs Datadog SaaS (buy).

Usage:
    python cost_model.py
    python cost_model.py --output csv       # also save cost_breakdown.csv
    python cost_model.py --output markdown  # also save cost_breakdown.md
"""

import argparse
from dataclasses import dataclass


# ─────────────────────────────────────────────
# PRICING CONSTANTS (as of 2024, USD/month)
# ─────────────────────────────────────────────

# Storage
ES_HOT_COST_PER_GB = 0.15        # Elasticsearch hot tier (RAM-heavy)
LOKI_COST_PER_GB = 0.015         # Loki warm tier (label-only index)
S3_COST_PER_GB = 0.023           # S3 standard cold storage
S3_GLACIER_COST_PER_GB = 0.004   # S3 Glacier archive

# Compute (EC2/VM estimated monthly)
KAFKA_COST_PER_NODE = 500        # r5.xlarge ~$500/month
KAFKA_MIN_NODES = 3              # minimum 3 brokers for HA
FLINK_COST_PER_NODE = 400        # m5.xlarge ~$400/month
PROMETHEUS_COST_PER_NODE = 600   # r5.xlarge with SSD ~$600/month
OTEL_COLLECTOR_COST = 100        # lightweight, 1 node per tier

# Network
EGRESS_COST_PER_GB = 0.09        # AWS egress ~$0.09/GB
INGEST_RATIO = 0.1               # ~10% of stored data goes out as egress

# SRE operational overhead (human cost)
SRE_HOURLY = 80                  # $80/hour fully-loaded SRE cost
SRE_HOURS_SMALL = 20             # 20h/month for small
SRE_HOURS_MEDIUM = 80            # 80h/month for medium
SRE_HOURS_LARGE = 200            # 200h/month for large

# Datadog SaaS pricing
DATADOG_INFRA_PER_HOST = 23      # $23/host/month (Pro plan)
DATADOG_LOG_INGEST_PER_GB = 0.10 # $0.10/GB ingested
DATADOG_LOG_RETAIN_PER_GB = 1.70 # $1.70/GB/month retained (15 days)
DATADOG_METRIC_PER_1K = 5.00     # $5/1000 custom metrics/month


# ─────────────────────────────────────────────
# TIER DEFINITIONS
# ─────────────────────────────────────────────
@dataclass
class Tier:
    name: str
    services: int
    log_gb_per_day: float
    metric_events_per_sec: int


TIERS = [
    Tier("Small",  services=10,   log_gb_per_day=50,    metric_events_per_sec=100_000),
    Tier("Medium", services=100,  log_gb_per_day=500,   metric_events_per_sec=1_000_000),
    Tier("Large",  services=1000, log_gb_per_day=5_000, metric_events_per_sec=10_000_000),
]


# ─────────────────────────────────────────────
# SELF-HOSTED (BUILD) COST MODEL
# Uses Hot/Warm/Cold tiering strategy:
#   7 days  → Elasticsearch (hot)
#   23 days → Loki (warm)
#   30-365  → S3 (cold)
# ─────────────────────────────────────────────
def calc_build_cost(tier: Tier) -> dict:
    log_per_month = tier.log_gb_per_day * 30

    # ── Storage ──
    # Hot: 7 days in Elasticsearch
    hot_gb = tier.log_gb_per_day * 7
    hot_cost = hot_gb * ES_HOT_COST_PER_GB

    # Warm: 23 days in Loki
    warm_gb = tier.log_gb_per_day * 23
    warm_cost = warm_gb * LOKI_COST_PER_GB

    # Cold: 30-365 days in S3 (335 days worth)
    cold_gb = tier.log_gb_per_day * 335
    cold_cost = cold_gb * S3_COST_PER_GB

    # Metric storage: Prometheus (1 month retention)
    # Each metric event ~100 bytes, Prometheus compresses ~10x
    metric_gb_per_month = (tier.metric_events_per_sec * 100 * 86400 * 30) / (1e9 * 10)
    prometheus_storage_cost = metric_gb_per_month * 0.10  # SSD-backed EBS

    storage_cost = hot_cost + warm_cost + cold_cost + prometheus_storage_cost

    # ── Compute ──
    # Kafka: scale nodes based on throughput
    kafka_nodes = max(KAFKA_MIN_NODES, tier.services // 10)
    kafka_cost = kafka_nodes * KAFKA_COST_PER_NODE

    # Flink/Vector processing nodes
    flink_nodes = max(1, tier.services // 20)
    flink_cost = flink_nodes * FLINK_COST_PER_NODE

    # Prometheus nodes
    prometheus_nodes = max(1, tier.services // 100)
    prometheus_cost = prometheus_nodes * PROMETHEUS_COST_PER_NODE

    # OTel Collector
    otel_cost = OTEL_COLLECTOR_COST * max(1, tier.services // 100)

    compute_cost = kafka_cost + flink_cost + prometheus_cost + otel_cost

    # ── Network ──
    egress_gb = log_per_month * INGEST_RATIO
    network_cost = egress_gb * EGRESS_COST_PER_GB

    # ── SRE Operational Cost ──
    sre_hours = {
        "Small": SRE_HOURS_SMALL,
        "Medium": SRE_HOURS_MEDIUM,
        "Large": SRE_HOURS_LARGE,
    }[tier.name]
    sre_cost = sre_hours * SRE_HOURLY

    total = storage_cost + compute_cost + network_cost + sre_cost

    return {
        "tier": tier.name,
        "mode": "Build (Self-hosted)",
        # Storage breakdown
        "storage_hot_es": round(hot_cost),
        "storage_warm_loki": round(warm_cost),
        "storage_cold_s3": round(cold_cost),
        "storage_metric_prometheus": round(prometheus_storage_cost),
        "storage_total": round(storage_cost),
        # Compute breakdown
        "compute_kafka": round(kafka_cost),
        "compute_flink": round(flink_cost),
        "compute_prometheus": round(prometheus_cost),
        "compute_otel": round(otel_cost),
        "compute_total": round(compute_cost),
        # Other
        "network_egress": round(network_cost),
        "sre_operational": round(sre_cost),
        # Summary
        "total_monthly": round(total),
        # Meta
        "kafka_nodes": kafka_nodes,
        "flink_nodes": flink_nodes,
        "log_gb_per_month": round(log_per_month),
    }


# ─────────────────────────────────────────────
# DATADOG SAAS (BUY) COST MODEL
# ─────────────────────────────────────────────
def calc_datadog_cost(tier: Tier) -> dict:
    log_per_month = tier.log_gb_per_day * 30

    # Infrastructure monitoring: per host
    # Assume avg 5 containers/service = 5 hosts per service
    hosts = tier.services * 5
    infra_cost = hosts * DATADOG_INFRA_PER_HOST

    # Log ingestion
    log_ingest_cost = log_per_month * DATADOG_LOG_INGEST_PER_GB

    # Log retention (15 days default, paying for retained volume)
    log_retained_gb = tier.log_gb_per_day * 15
    log_retain_cost = log_retained_gb * DATADOG_LOG_RETAIN_PER_GB

    # Custom metrics
    # Assume 100 custom metrics per service
    custom_metrics_k = (tier.services * 100) / 1000
    metric_cost = custom_metrics_k * DATADOG_METRIC_PER_1K

    # APM (tracing): $31/host/month
    apm_cost = hosts * 31

    total = infra_cost + log_ingest_cost + log_retain_cost + metric_cost + apm_cost

    return {
        "tier": tier.name,
        "mode": "Buy (Datadog SaaS)",
        # Breakdown
        "infra_monitoring": round(infra_cost),
        "log_ingestion": round(log_ingest_cost),
        "log_retention": round(log_retain_cost),
        "custom_metrics": round(metric_cost),
        "apm_tracing": round(apm_cost),
        # Summary
        "total_monthly": round(total),
        # Meta
        "hosts": hosts,
        "log_gb_per_month": round(log_per_month),
    }


# ─────────────────────────────────────────────
# PRINT FUNCTIONS
# ─────────────────────────────────────────────
def fmt(n: int) -> str:
    """Format number as $1,234"""
    return f"${n:,}"


def print_comparison_table(results: list[dict]):
    build_results = [r for r in results if r["mode"].startswith("Build")]
    dd_results = [r for r in results if r["mode"].startswith("Buy")]

    print("\n" + "=" * 70)
    print("  OBSERVABILITY PIPELINE — MONTHLY COST ESTIMATION")
    print("=" * 70)

    for build, dd in zip(build_results, dd_results):
        tier_name = build["tier"]
        tier = next(t for t in TIERS if t.name == tier_name)

        print(f"\n{'─' * 70}")
        print(f"  TIER: {tier_name.upper()} — {tier.services} services | "
              f"{tier.log_gb_per_day:,} GB log/day | "
              f"{tier.metric_events_per_sec:,} events/sec")
        print(f"{'─' * 70}")

        print(f"\n  {'Component':<35} {'Build':>12} {'Datadog':>12}")
        print(f"  {'-'*35} {'-'*12} {'-'*12}")

        # Storage
        print(f"\n  {'STORAGE':}")
        print(f"  {'  Log - Hot (ES 7d)':<35} {fmt(build['storage_hot_es']):>12} {'(included)':>12}")
        print(f"  {'  Log - Warm (Loki 23d)':<35} {fmt(build['storage_warm_loki']):>12} {'':>12}")
        print(f"  {'  Log - Cold (S3 335d)':<35} {fmt(build['storage_cold_s3']):>12} {'':>12}")
        print(f"  {'  Log Ingest':<35} {'':>12} {fmt(dd['log_ingestion']):>12}")
        print(f"  {'  Log Retention (15d)':<35} {'':>12} {fmt(dd['log_retention']):>12}")
        print(f"  {'  Metric Storage':<35} {fmt(build['storage_metric_prometheus']):>12} {'(included)':>12}")
        print(f"  {'  Storage Subtotal':<35} {fmt(build['storage_total']):>12} {'':>12}")

        # Compute
        print(f"\n  {'COMPUTE':}")
        print(f"  {'  Kafka':<35} {fmt(build['compute_kafka']):>12} {'':>12}")
        print(f"  {'  Flink/Vector':<35} {fmt(build['compute_flink']):>12} {'':>12}")
        print(f"  {'  Prometheus':<35} {fmt(build['compute_prometheus']):>12} {'':>12}")
        print(f"  {'  OTel Collector':<35} {fmt(build['compute_otel']):>12} {'':>12}")
        print(f"  {'  Infra Monitoring':<35} {'':>12} {fmt(dd['infra_monitoring']):>12}")
        print(f"  {'  APM / Tracing':<35} {'':>12} {fmt(dd['apm_tracing']):>12}")
        print(f"  {'  Custom Metrics':<35} {'':>12} {fmt(dd['custom_metrics']):>12}")
        print(f"  {'  Compute Subtotal':<35} {fmt(build['compute_total']):>12} {'':>12}")

        # Other
        print(f"\n  {'OTHER':}")
        print(f"  {'  Network Egress':<35} {fmt(build['network_egress']):>12} {'(included)':>12}")
        print(f"  {'  SRE Operational':<35} {fmt(build['sre_operational']):>12} {'N/A':>12}")

        # Total
        savings = dd["total_monthly"] - build["total_monthly"]
        savings_pct = (savings / dd["total_monthly"] * 100) if dd["total_monthly"] > 0 else 0

        print(f"\n  {'─'*35} {'─'*12} {'─'*12}")
        print(f"  {'TOTAL MONTHLY':<35} {fmt(build['total_monthly']):>12} {fmt(dd['total_monthly']):>12}")

        if savings > 0:
            print(f"\n  ✅ Build saves {fmt(savings)}/month ({savings_pct:.0f}%) vs Datadog")
        else:
            print(f"\n  ✅ Datadog saves {fmt(abs(savings))}/month ({abs(savings_pct):.0f}%) vs self-hosting")

        # Recommendation
        print(f"\n  💡 Recommendation:")
        if tier_name == "Small":
            print(f"     → Use Datadog. Self-hosting complexity not worth it at this scale.")
            print(f"        SRE overhead alone costs {fmt(build['sre_operational'])}/month.")
        elif tier_name == "Medium":
            print(f"     → Evaluate carefully. Build saves money but adds operational burden.")
            print(f"        Consider hybrid: Datadog for logs, self-hosted Prometheus for metrics.")
        else:
            print(f"     → Self-host. Datadog cost is prohibitive at this scale.")
            print(f"        Invest in platform team to manage the infrastructure.")

    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  {'Tier':<10} {'Build':>12} {'Datadog':>12} {'Savings (Build)':>16} {'Verdict':>10}")
    print(f"  {'-'*10} {'-'*12} {'-'*12} {'-'*16} {'-'*10}")
    for build, dd in zip(build_results, dd_results):
        savings = dd["total_monthly"] - build["total_monthly"]
        verdict = "BUILD ✅" if savings > 0 else "BUY ✅"
        print(f"  {build['tier']:<10} {fmt(build['total_monthly']):>12} "
              f"{fmt(dd['total_monthly']):>12} {fmt(savings):>16} {verdict:>10}")
    print()


def save_csv(results: list[dict]):
    import csv
    filename = "cost_breakdown.csv"
    if not results:
        return

    keys = results[0].keys()
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(results)
    print(f"[Output] Saved → {filename}")


def save_markdown(build_results, dd_results):
    filename = "cost_breakdown.md"
    lines = ["# Observability Pipeline — Monthly Cost Estimation\n"]

    for build, dd in zip(build_results, dd_results):
        tier = build["tier"]
        savings = dd["total_monthly"] - build["total_monthly"]
        lines.append(f"## {tier} Tier\n")
        lines.append(f"| Component | Build | Datadog |")
        lines.append(f"|-----------|------:|--------:|")
        lines.append(f"| Storage (Hot ES) | {fmt(build['storage_hot_es'])} | (included) |")
        lines.append(f"| Storage (Warm Loki) | {fmt(build['storage_warm_loki'])} | |")
        lines.append(f"| Storage (Cold S3) | {fmt(build['storage_cold_s3'])} | |")
        lines.append(f"| Log Ingest | | {fmt(dd['log_ingestion'])} |")
        lines.append(f"| Log Retention | | {fmt(dd['log_retention'])} |")
        lines.append(f"| Compute (Kafka) | {fmt(build['compute_kafka'])} | |")
        lines.append(f"| Compute (Flink) | {fmt(build['compute_flink'])} | |")
        lines.append(f"| Compute (Prometheus) | {fmt(build['compute_prometheus'])} | |")
        lines.append(f"| Infra Monitoring | | {fmt(dd['infra_monitoring'])} |")
        lines.append(f"| APM / Tracing | | {fmt(dd['apm_tracing'])} |")
        lines.append(f"| Network Egress | {fmt(build['network_egress'])} | (included) |")
        lines.append(f"| SRE Operational | {fmt(build['sre_operational'])} | N/A |")
        lines.append(f"| **TOTAL** | **{fmt(build['total_monthly'])}** | **{fmt(dd['total_monthly'])}** |")
        lines.append(f"\n> Build saves **{fmt(savings)}/month** vs Datadog\n")

    with open(filename, "w") as f:
        f.write("\n".join(lines))
    print(f"[Output] Saved → {filename}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Observability cost model: Build vs Buy")
    parser.add_argument(
        "--output",
        choices=["none", "csv", "markdown"],
        default="none",
        help="Also save output as csv or markdown",
    )
    args = parser.parse_args()

    all_results = []
    build_results = []
    dd_results = []

    for tier in TIERS:
        build = calc_build_cost(tier)
        dd = calc_datadog_cost(tier)
        all_results.extend([build, dd])
        build_results.append(build)
        dd_results.append(dd)

    print_comparison_table(all_results)

    if args.output == "csv":
        save_csv(all_results)
    elif args.output == "markdown":
        save_markdown(build_results, dd_results)