"""
cost_model.py — AIOps platform break-even calculator.
Formula: value_per_month = incidents_per_month × avg_incident_duration_hours
                           × expected_mttr_reduction_pct × downtime_cost_per_hour
ROI = monthly_value / aiops_monthly_cost
Verdict: roi > 1.5 → worth_it | 1.0 < roi ≤ 1.5 → marginal | roi ≤ 1.0 → not_worth_it
"""


def is_worth_it(
    num_services: int,
    incidents_per_month: int,
    avg_incident_duration_hours: float,
    downtime_cost_per_hour: float,
    expected_mttr_reduction_pct: float = 0.4,
    aiops_monthly_cost: float = 15_000,
) -> dict:
    """
    Returns:
        {
            "monthly_value": float,
            "monthly_cost": float,
            "roi": float,
            "payback_months": float,  # or float('inf')
            "verdict": "worth_it" | "marginal" | "not_worth_it"
        }
    """
    monthly_downtime_hours = incidents_per_month * avg_incident_duration_hours
    monthly_value = (
        monthly_downtime_hours
        * expected_mttr_reduction_pct
        * downtime_cost_per_hour
    )

    roi = monthly_value / aiops_monthly_cost if aiops_monthly_cost > 0 else float("inf")
    payback_months = (
        aiops_monthly_cost / monthly_value if monthly_value > 0 else float("inf")
    )

    if roi > 1.5:
        verdict = "worth_it"
    elif roi > 1.0:
        verdict = "marginal"
    else:
        verdict = "not_worth_it"

    return {
        "monthly_value": round(monthly_value, 2),
        "monthly_cost": aiops_monthly_cost,
        "roi": round(roi, 2),
        "payback_months": round(payback_months, 2) if payback_months != float("inf") else float("inf"),
        "verdict": verdict,
    }


if __name__ == "__main__":
    # Scenario 1 (from spec): 20 services, small shop — expected: not_worth_it
    print("=== Scenario 1: Small shop (20 services) ===")
    print(is_worth_it(
        num_services=20,
        incidents_per_month=2,
        avg_incident_duration_hours=1,
        downtime_cost_per_hour=10_000,
        aiops_monthly_cost=15_000,
    ))

    # Scenario 2 (from spec): 100 services, mid-tier — expected: worth_it
    print("\n=== Scenario 2: Mid-tier platform (100 services) ===")
    print(is_worth_it(
        num_services=100,
        incidents_per_month=5,
        avg_incident_duration_hours=2,
        downtime_cost_per_hour=20_000,
        aiops_monthly_cost=25_000,
    ))

    # Scenario 3 (custom): E-commerce Vietnam mid-tier, 50 services
    # Downtime cost chọn $8k/hour — mid-range e-commerce (ITIC 2024: $5k-$50k/hour for e-commerce)
    # Justification: công ty e-commerce Vietnam quy mô trung bình, GMV ~$10M/tháng,
    # 1h downtime mất ~0.08% monthly GMV + SLA penalty + support cost ≈ $8k
    print("\n=== Scenario 3 (custom): Vietnam e-commerce, 50 services ===")
    print("# Industry: E-commerce mid-tier")
    print("# Downtime cost: $8,000/hour (mid-range ITIC 2024 bracket: $5k-$50k/h)")
    print("# Justification: GMV loss + SLA penalty + ops cost for ~$10M/month platform")
    print(is_worth_it(
        num_services=50,
        incidents_per_month=4,
        avg_incident_duration_hours=1.5,
        downtime_cost_per_hour=8_000,
        aiops_monthly_cost=15_000,
    ))
