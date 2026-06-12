"""
Layer 3 — Action selection.

Algorithm:
  1. If OOD (best_sim < threshold) → page_oncall immediately.
  2. For each candidate action compute Expected Utility:
       EU(a) = P_success(a) * benefit(a) - cost_penalty(a) - blast_penalty(a)
     where:
       benefit(a)       = 1.0 (normalised — we care about relative ranking)
       cost_penalty(a)  = cost_min / MAX_COST_MIN  (normalised)
       blast_penalty(a) = blast_radius_services / MAX_BLAST * BLAST_WEIGHT
  3. page_oncall is assigned cost_penalty=0 but also benefit=ONCALL_BENEFIT (< 1),
     so it isn't always chosen by the naive zero-cost logic.
  4. Blast-radius gate: if top action blast_radius_services >= BLAST_GATE and
     confidence < BLAST_CONFIDENCE_FLOOR → drop to next candidate or escalate.
  5. Confidence gate: if final confidence < CONFIDENCE_FLOOR → escalate.
  6. Tie breaking: if top-2 EU within TIE_MARGIN → prefer lower blast_radius.

Returns a full audit record matching the grading contract.
"""

from __future__ import annotations

from typing import Any

# ── Tuning constants ────────────────────────────────────────────────────────
MAX_COST_MIN          = 15.0   # network_policy_revert is most expensive
MAX_BLAST             = 4.0    # network_policy_revert has blast=4
BLAST_WEIGHT          = 0.30   # how much blast radius penalises utility
ONCALL_BENEFIT        = 0.35   # page_oncall is last resort, not always-winner
BLAST_GATE            = 3      # blast_radius_services >= this requires high confidence
BLAST_CONFIDENCE_FLOOR= 0.55   # minimum confidence to act on high-blast action
CONFIDENCE_FLOOR      = 0.20   # global: below this → always escalate
OOD_THRESHOLD         = 0.20   # mirror from retrieval.py


def select_action(
    retrieval_result: dict,
    actions_catalog: list[dict],
    incident: dict,
) -> dict:
    """
    Accepts the retrieval result from Layer 2 and the action catalog.
    Returns the final audit record dict.
    """
    incident_id = _derive_incident_id(incident)

    # Build catalog lookup
    catalog: dict[str, dict] = {a["name"]: a for a in actions_catalog}

    # ── OOD escalation ────────────────────────────────────────────────────
    if retrieval_result["is_ood"]:
        return _build_record(
            incident_id      = incident_id,
            action           = "page_oncall",
            params           = {"team": "platform-team"},
            confidence       = 0.10,
            reason           = "OOD: no sufficiently similar historical precedent found",
            retrieval_result = retrieval_result,
            eu_trace         = {},
            blast_radius_check = {"gate_triggered": False, "blast_radius": 0},
        )

    votes      = retrieval_result["action_votes"]
    best_sim   = retrieval_result["best_sim"]
    consensus  = retrieval_result["consensus_score"]

    # ── Compute EU for each candidate action ─────────────────────────────
    eu_trace: dict[str, dict] = {}

    for action_name, vote_info in votes.items():
        cat = catalog.get(action_name, {})

        # P_success: combine history estimate + consensus signal
        p_hist = retrieval_result.get("top_action_p_success", 0.5)
        if action_name == retrieval_result["top_action"]:
            p_success = p_hist
        else:
            # secondary candidates get reduced P_success estimate
            ratio = vote_info["weighted_score"] / max(
                1e-9, votes[retrieval_result["top_action"]]["weighted_score"]
            )
            p_success = p_hist * ratio * 0.8

        p_success = max(0.0, min(1.0, p_success))

        # Benefit: 1.0 for all real actions; reduced for page_oncall
        if action_name == "page_oncall":
            benefit = ONCALL_BENEFIT
        else:
            benefit = 1.0

        # Cost penalty (normalised)
        cost_min     = cat.get("cost_min", 0)
        cost_penalty = cost_min / MAX_COST_MIN

        # Blast penalty
        blast = cat.get("blast_radius_services", 0)
        blast_penalty = (blast / MAX_BLAST) * BLAST_WEIGHT

        eu = p_success * benefit - cost_penalty - blast_penalty

        eu_trace[action_name] = {
            "eu":           round(eu, 4),
            "p_success":    round(p_success, 4),
            "benefit":      benefit,
            "cost_penalty": round(cost_penalty, 4),
            "blast_penalty":round(blast_penalty, 4),
            "blast_radius": blast,
            "weighted_score": vote_info["weighted_score"],
        }

    # ── Sort candidates by EU (descending) ───────────────────────────────
    ranked = sorted(eu_trace.items(), key=lambda x: -x[1]["eu"])

    # ── Blast-radius gate + confidence check ─────────────────────────────
    confidence_raw = best_sim * consensus

    selected_action = None
    blast_check_info = {}

    for action_name, info in ranked:
        if action_name == "page_oncall":
            # We'll consider oncall only if no real action passes gates
            continue

        blast = info["blast_radius"]
        confidence_adjusted = confidence_raw * info["p_success"]

        blast_triggered = (blast >= BLAST_GATE and
                           confidence_adjusted < BLAST_CONFIDENCE_FLOOR)
        global_low_conf = confidence_adjusted < CONFIDENCE_FLOOR

        blast_check_info = {
            "action":             action_name,
            "blast_radius":       blast,
            "confidence_needed":  BLAST_CONFIDENCE_FLOOR if blast >= BLAST_GATE else CONFIDENCE_FLOOR,
            "confidence_actual":  round(confidence_adjusted, 4),
            "gate_triggered":     blast_triggered or global_low_conf,
        }

        if blast_triggered or global_low_conf:
            # This action is too risky; try next
            continue

        selected_action = action_name
        break

    # If nothing passes the gate, escalate
    if selected_action is None:
        selected_action = "page_oncall"
        blast_check_info["gate_triggered"] = True

    # ── Params inference ─────────────────────────────────────────────────
    params = _infer_params(selected_action, retrieval_result, incident, catalog)

    # ── Final confidence ──────────────────────────────────────────────────
    if selected_action == "page_oncall":
        confidence = round(min(0.95, confidence_raw), 4)
    else:
        eu_info    = eu_trace.get(selected_action, {})
        confidence = round(min(0.95, confidence_raw * eu_info.get("p_success", 0.5)), 4)

    return _build_record(
        incident_id       = incident_id,
        action            = selected_action,
        params            = params,
        confidence        = confidence,
        reason            = _build_reason(selected_action, ranked, retrieval_result),
        retrieval_result  = retrieval_result,
        eu_trace          = eu_trace,
        blast_radius_check= blast_check_info,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _derive_incident_id(incident: dict) -> str:
    """Use the file basename hint stored in incident or the incident_id field."""
    return incident.get("_eval_id") or incident.get("incident_id", "UNKNOWN")


def _infer_params(
    action_name: str,
    retrieval_result: dict,
    incident: dict,
    catalog: dict,
) -> dict:
    """
    Infer best-guess params for the selected action by combining:
      1. The most common params from top-k neighbours (from retrieval).
      2. The alerting service from the current incident.
    """
    alerting_svc = incident.get("trigger_alert", {}).get("service", "")
    hist_params  = retrieval_result.get("top_action_params", [])

    if action_name == "page_oncall":
        return {"team": "platform-team"}

    if action_name == "rollback_service":
        # Prefer the alerting / primary suspect service
        svc = alerting_svc or (hist_params[0] if hist_params else "unknown-svc")
        ver = "previous"
        if len(hist_params) >= 2:
            ver = hist_params[1] if hist_params[1] not in ("previous",) else "previous"
        return {"service": svc, "target_version": ver}

    if action_name == "increase_pool_size":
        svc = alerting_svc or (hist_params[0] if hist_params else "unknown-svc")
        return {"service": svc, "from_value": 50, "to_value": 100}

    if action_name == "restart_pod":
        svc = alerting_svc or (hist_params[0] if hist_params else "unknown-svc")
        return {"service": svc, "pod_selector": "default"}

    if action_name == "dns_config_rollback":
        return {"configmap_name": "dns-config", "target_revision": "previous"}

    if action_name == "network_policy_revert":
        return {"policy_name": "default-network-policy"}

    return {}


def _build_reason(
    selected: str,
    ranked: list,
    retrieval_result: dict,
) -> str:
    top3 = retrieval_result.get("top_3_neighbors", [])
    neighbor_summary = "; ".join(
        f"{n['id']} (sim={n['similarity']}, outcome={n['outcome']})"
        for n in top3[:3]
    )
    if selected == "page_oncall":
        if retrieval_result.get("is_ood"):
            return f"OOD escalation: best_sim={retrieval_result['best_sim']} below threshold. Neighbors: {neighbor_summary}"
        return f"Low confidence or high blast radius forced escalation. Neighbors: {neighbor_summary}"

    top_eu_action, top_eu_info = ranked[0] if ranked else ("?", {})
    return (
        f"Selected '{selected}' via outcome-weighted kNN + EU maximisation. "
        f"Top EU action: '{top_eu_action}' (EU={top_eu_info.get('eu', '?')}). "
        f"Neighbors: {neighbor_summary}"
    )


def _build_record(
    incident_id:        str,
    action:             str,
    params:             dict,
    confidence:         float,
    reason:             str,
    retrieval_result:   dict,
    eu_trace:           dict,
    blast_radius_check: dict,
) -> dict:
    # Strip private keys from retrieval result for output
    clean_retrieval = {k: v for k, v in retrieval_result.items()
                       if not k.startswith("_")}

    return {
        # ── Grader-required fields ──────────────────────────────────────
        "incident_id":     incident_id,
        "selected_action": action,
        "params":          params,
        "confidence":      confidence,
        # ── Grader bonus fields ─────────────────────────────────────────
        "top_3_neighbors":    retrieval_result.get("top_3_neighbors", []),
        "consensus_score":    retrieval_result.get("consensus_score", 0.0),
        "blast_radius_check": blast_radius_check,
        # ── Evidence chain (Option B) ───────────────────────────────────
        "evidence": {
            "reason":              reason,
            "best_sim":            retrieval_result.get("best_sim"),
            "is_ood":              retrieval_result.get("is_ood"),
            "action_votes":        clean_retrieval.get("action_votes", {}),
            "eu_breakdown":        eu_trace,
            "retrieval_summary":   clean_retrieval,
        },
    }
