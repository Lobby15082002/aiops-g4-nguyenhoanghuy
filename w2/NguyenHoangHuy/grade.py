"""
Auto-grade a student's audit.jsonl against eval/expected.json.

Usage:
    python grade.py --audit audit.jsonl --expected eval/expected.json
"""

import argparse
import json
import sys
from pathlib import Path


def action_matches(recommended: dict, accepted: dict) -> bool:
    """Match by action name AND any specified params (subset match)."""
    if recommended.get("selected_action") != accepted.get("name"):
        return False

    accepted_params = accepted.get("params", {}) or {}
    rec_params = recommended.get("params", {}) or {}

    for k, v in accepted_params.items():
        if rec_params.get(k) != v:
            return False

    return True


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--audit", required=True)
    p.add_argument("--expected", required=True)
    args = p.parse_args()

    expected = json.loads(Path(args.expected).read_text())

    audit_lines = [
        json.loads(line)
        for line in Path(args.audit).read_text().splitlines()
        if line.strip()
    ]

    by_id = {e.get("incident_id"): e for e in audit_lines}

    correct = 0
    forbidden = 0
    missing = 0
    detail = []

    # -----------------------------
    # Incident grading
    # -----------------------------
    for eid, expected_entry in expected.items():
        rec = by_id.get(eid)

        if rec is None:
            missing += 1
            detail.append((eid, "MISSING from audit.jsonl"))
            continue

        accepted = expected_entry.get("accepted_actions", [])
        must_not = expected_entry.get("must_not_action")

        if must_not and rec.get("selected_action") == must_not:
            forbidden += 1
            detail.append(
                (eid, f"VIOLATED must_not_action ({must_not})")
            )
            continue

        if any(action_matches(rec, a) for a in accepted):
            correct += 1
            detail.append(
                (eid, f"OK -> {rec.get('selected_action')}")
            )
        else:
            detail.append(
                (
                    eid,
                    f"WRONG -> {rec.get('selected_action')}; "
                    f"expected one of {[a['name'] for a in accepted]}"
                )
            )

    total = len(expected)

    # -----------------------------
    # Summary
    # -----------------------------
    print(f"Correct: {correct}/{total}")
    print(f"Forbidden (chose must_not_action): {forbidden}/{total}")
    print(f"Missing from audit: {missing}/{total}")

    print()
    print("Per-incident detail:")

    for eid, note in detail:
        print(f"  {eid}: {note}")

    # -----------------------------
    # Rubric breakdown
    # -----------------------------
    print()
    print("====================================")
    print("RUBRIC BREAKDOWN")
    print("====================================")

    score = 0

    runs_ok = (missing == 0)

    # 1. Runs OK
    if runs_ok:
        score += 10
        print("[+10] Runs OK (no missing incidents)")
    else:
        print(f"[ +0] Runs OK failed ({missing} missing incident(s))")

    sample = audit_lines[0] if audit_lines else {}

    # Debug sample
    print()
    print("Sample audit record used for feature checks:")
    print(json.dumps(sample, indent=2))

    print()

    # 2. top_3_neighbors
    if sample.get("top_3_neighbors"):
        score += 15
        print("[+15] top_3_neighbors present")
    else:
        print("[ +0] top_3_neighbors missing")

    # 3. consensus_score
    if sample.get("consensus_score") is not None:
        score += 15
        print("[+15] consensus_score present")
    else:
        print("[ +0] consensus_score missing")

    # 4. blast radius
    has_blast_radius = (
        "blast_radius_check" in sample
        or
        "blast_radius_services"
        in sample.get("selected_action_meta", {})
    )

    if has_blast_radius:
        score += 10
        print("[+10] blast_radius_check present")
    else:
        print("[ +0] blast_radius_check missing")

    # 5. Accuracy
    pct_correct = correct / total if total else 0

    if pct_correct >= 0.6:
        score += 15
        print(f"[+15] Accuracy {pct_correct:.1%}")
    elif pct_correct >= 0.4:
        score += 10
        print(f"[+10] Accuracy {pct_correct:.1%}")
    else:
        print(f"[ +0] Accuracy {pct_correct:.1%}")

    # 6. Forbidden actions
    if forbidden == 0:
        score += 10
        print("[+10] No forbidden actions")
    elif forbidden <= 1:
        score += 5
        print("[ +5] One forbidden action")
    else:
        print(f"[ +0] Forbidden actions = {forbidden}")

    # 7. Missing bonus
    if missing == 0:
        score += 10
        print("[+10] No missing incidents")
    else:
        print(f"[ +0] Missing incidents = {missing}")

    print()
    print("====================================")
    print(f"TOTAL AUTO SCORE = {score}/85")
    print("====================================")
    print()
    print("FINDINGS (15 pts) + optional bonus (up to 20) graded manually.")

    return 0 if (missing == 0 and forbidden <= 1) else 1


if __name__ == "__main__":
    sys.exit(main())