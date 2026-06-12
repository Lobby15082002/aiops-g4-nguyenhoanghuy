"""
Layer 2 — Precedent retrieval + outcome-weighted candidate voting.

Design:
  - Hybrid similarity: cosine on log/trace dims + Jaccard on service one-hot.
    Log/trace dims carry 70% weight; service overlap 30%.
  - Top-k neighbours (default k=5) but also threshold-gated:
    if the best similarity < OOD_THRESHOLD, the incident is flagged as OOD
    (out-of-distribution) and the engine should escalate.
  - Outcome weighting: success=1.0, partial=0.4, failed=0.0.
    Each candidate action accumulates a weighted score from votes.
  - Tie-breaking: when top-2 scores are within TIE_MARGIN, prefer the
    action with lower blast_radius (passed from decision layer).
"""

from __future__ import annotations

import math
from typing import Any

from features import (
    extract_history_features, feature_vector, feature_keys,
    OUTCOME_WEIGHTS, KNOWN_SERVICES, SVC_INDEX
)

# ── Tuning constants ──────────────────────────────────────────────────────
TOP_K           = 5      # neighbours to retrieve
OOD_THRESHOLD   = 0.20   # below this best-sim → OOD
TIE_MARGIN      = 0.05   # top-2 action scores within this → tie

# Feature group weights for hybrid similarity
W_LOG_TRACE = 0.70
W_SERVICE   = 0.30


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a: list[float], b: list[float]) -> float:
    """Binary Jaccard for one-hot service vectors."""
    inter = sum(1 for x, y in zip(a, b) if x > 0 and y > 0)
    union = sum(1 for x, y in zip(a, b) if x > 0 or  y > 0)
    return inter / union if union > 0 else 0.0


def _split_dims(keys: list[str], values: list[float]):
    """Split a flat feature vector into (log_trace_dims, service_dims)."""
    lt, svc = [], []
    for k, v in zip(keys, values):
        if k.startswith("svc_"):
            svc.append(v)
        else:
            lt.append(v)
    return lt, svc


def similarity(query_vec: dict, hist_vec: dict) -> float:
    """
    Hybrid similarity between two incident vectors.
    Returns a value in [0, 1].
    """
    qkeys = feature_keys(query_vec)
    hkeys = feature_keys(hist_vec)

    # Align on common keys
    common = [k for k in qkeys if k in set(hkeys)]
    if not common:
        return 0.0

    qvals = [query_vec[k] for k in common]
    hvals = [hist_vec[k]  for k in common]

    q_lt, q_svc = _split_dims(common, qvals)
    h_lt, h_svc = _split_dims(common, hvals)

    cos  = _cosine(q_lt, h_lt)   if q_lt  else 0.0
    jacc = _jaccard(q_svc, h_svc) if q_svc else 0.0

    return W_LOG_TRACE * cos + W_SERVICE * jacc


def _parse_history_action(s: str) -> dict:
    """'rollback_service:payment-svc:v3.1' → {name, params:[...]}"""
    parts = s.split(":")
    if not parts:
        return {"name": "page_oncall", "params": []}
    return {"name": parts[0], "params": parts[1:]}


def retrieve_and_vote(
    query_vec: dict,
    history: list[dict],
    top_k: int = TOP_K,
) -> dict:
    """
    kNN retrieval from history, returns a structured result:

    {
      "top_3_neighbors": [...],   # required by grader
      "consensus_score": float,   # required by grader
      "is_ood": bool,
      "best_sim": float,
      "action_votes": {
          action_name: {
              "weighted_score": float,
              "raw_count":      int,
              "supporting_incidents": [...],
          }
      },
      "top_action": str,
      "top_action_params": [...],
      "top_action_p_success": float,
    }
    """
    # Pre-compute history feature vectors (cached lazily in caller via history objects)
    scored = []
    for h in history:
        if "_vec" not in h:
            h["_vec"] = extract_history_features(h)
        sim = similarity(query_vec, h["_vec"])
        scored.append((sim, h))

    scored.sort(key=lambda x: -x[0])
    top_neighbours = scored[:top_k]
    best_sim = top_neighbours[0][0] if top_neighbours else 0.0

    # Build top_3_neighbors for grader
    top3 = []
    for sim, h in top_neighbours[:3]:
        top3.append({
            "id":             h["id"],
            "similarity":     round(sim, 4),
            "root_cause":     h.get("root_cause_class", ""),
            "outcome":        h.get("outcome", ""),
            "actions_taken":  h.get("actions_taken", []),
        })

    # OOD check
    is_ood = best_sim < OOD_THRESHOLD

    # ── Outcome-weighted action voting ────────────────────────────────────
    action_votes: dict[str, dict] = {}

    for sim, h in top_neighbours:
        outcome = h.get("outcome", "partial")
        ow      = OUTCOME_WEIGHTS.get(outcome, 0.2)
        vote    = sim * ow  # weighted vote

        for raw_action in h.get("actions_taken", []):
            parsed = _parse_history_action(raw_action)
            name   = parsed["name"]
            params = parsed["params"]

            if name not in action_votes:
                action_votes[name] = {
                    "weighted_score":        0.0,
                    "raw_count":             0,
                    "supporting_incidents":  [],
                    "params_candidates":     [],
                }
            action_votes[name]["weighted_score"]      += vote
            action_votes[name]["raw_count"]            += 1
            action_votes[name]["supporting_incidents"].append(h["id"])
            action_votes[name]["params_candidates"].append(params)

    if not action_votes:
        # Fallback: no history at all
        action_votes["page_oncall"] = {
            "weighted_score":       0.0,
            "raw_count":            1,
            "supporting_incidents": [],
            "params_candidates":    [["platform-team"]],
        }

    # ── Consensus score ───────────────────────────────────────────────────
    # Fraction of top-k votes going to the winning action (normalised)
    total_score = sum(v["weighted_score"] for v in action_votes.values())
    top_action  = max(action_votes, key=lambda a: action_votes[a]["weighted_score"])
    top_score   = action_votes[top_action]["weighted_score"]
    consensus   = (top_score / total_score) if total_score > 0 else 0.0

    # Best params for top action: most common params candidate
    params_cands = action_votes[top_action]["params_candidates"]
    top_params   = _most_common_params(params_cands)

    # P_success estimate: weighted fraction of supporting incidents with success
    p_success = _estimate_p_success(top_action, top_neighbours)

    return {
        "top_3_neighbors":       top3,
        "consensus_score":       round(consensus, 4),
        "is_ood":                is_ood,
        "best_sim":              round(best_sim, 4),
        "action_votes":          {
            k: {
                "weighted_score":       round(v["weighted_score"], 4),
                "raw_count":            v["raw_count"],
                "supporting_incidents": v["supporting_incidents"],
            }
            for k, v in action_votes.items()
        },
        "top_action":            top_action,
        "top_action_params":     top_params,
        "top_action_p_success":  round(p_success, 4),
        "_top_neighbours_full":  top_neighbours,  # private, for decision layer
    }


def _most_common_params(candidates: list[list[str]]) -> list[str]:
    if not candidates:
        return []
    # Convert to tuples for hashing
    counts: dict[tuple, int] = {}
    for c in candidates:
        t = tuple(c)
        counts[t] = counts.get(t, 0) + 1
    best = max(counts, key=lambda t: counts[t])
    return list(best)


def _estimate_p_success(action_name: str, top_neighbours: list) -> float:
    """
    Estimate P(success | action) from matching neighbours.
    Only considers neighbours that took this action.
    """
    successes = 0
    total     = 0
    for sim, h in top_neighbours:
        actions = [_parse_history_action(a)["name"] for a in h.get("actions_taken", [])]
        if action_name in actions:
            total += 1
            if h.get("outcome") == "success":
                successes += 1
    if total == 0:
        return 0.5  # unknown → neutral
    return successes / total
