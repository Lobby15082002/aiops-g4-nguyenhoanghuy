"""
Evidence-Driven Remediation Engine — main entry point.

Usage:
    python engine.py decide --incident eval/E01.json \
                            --history incidents_history.json \
                            --actions actions.yaml

    # Run all 8 eval incidents at once:
    python engine.py batch --eval-dir eval \
                           --history incidents_history.json \
                           --actions actions.yaml

Outputs:
  - JSON decision to stdout
  - Appends one line to audit.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from features  import extract_features
from retrieval import retrieve_and_vote
from decision  import select_action


# ─────────────────────────────────────────────────────────────────────────────

def load_incident(incident_path: Path, eval_id: str | None = None) -> dict:
    incident = json.loads(incident_path.read_text())
    # Inject eval_id so the decision layer can embed it in audit record
    if eval_id is None:
        eval_id = incident_path.stem  # e.g. "E01"
    incident["_eval_id"] = eval_id
    return incident


def decide(
    incident_path: Path,
    history_path:  Path,
    actions_path:  Path,
    eval_id:       str | None = None,
) -> dict:
    incident       = load_incident(incident_path, eval_id)
    history        = json.loads(history_path.read_text())
    actions_catalog= yaml.safe_load(actions_path.read_text())

    # Layer 1: feature extraction
    incident_vec = extract_features(incident)

    # Layer 2: retrieval + voting
    retrieval_result = retrieve_and_vote(incident_vec, history)

    # Layer 3: action selection
    decision = select_action(retrieval_result, actions_catalog, incident)

    return decision


def write_audit(decision: dict, audit_path: Path = Path("audit.jsonl")) -> None:
    with open(audit_path, "a") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────────────────────

def cmd_decide(args: argparse.Namespace) -> int:
    decision = decide(
        Path(args.incident),
        Path(args.history),
        Path(args.actions),
    )
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    audit_path = Path(args.audit)
    write_audit(decision, audit_path)
    print(f"\n[audit] appended to {audit_path}", file=sys.stderr)
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Run all E*.json incidents in eval_dir and write audit.jsonl."""
    eval_dir   = Path(args.eval_dir)
    audit_path = Path(args.audit)

    # Clear audit file for fresh batch run
    if audit_path.exists():
        audit_path.unlink()

    incidents = sorted(eval_dir.glob("E*.json"))
    if not incidents:
        print(f"[ERROR] No E*.json files found in {eval_dir}", file=sys.stderr)
        return 1

    for inc_path in incidents:
        eval_id  = inc_path.stem
        decision = decide(
            inc_path,
            Path(args.history),
            Path(args.actions),
            eval_id=eval_id,
        )
        print(f"[{eval_id}] → {decision['selected_action']} "
              f"(confidence={decision['confidence']}, "
              f"sim={decision['evidence']['best_sim']})",
              file=sys.stderr)
        write_audit(decision, audit_path)

    print(f"\n[batch] wrote {len(incidents)} entries to {audit_path}", file=sys.stderr)
    return 0


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="Evidence-driven remediation engine"
    )
    sub = p.add_subparsers(dest="cmd")

    # ── decide ──
    d = sub.add_parser("decide", help="Decide action for one incident")
    d.add_argument("--incident", required=True,  help="Path to incident JSON")
    d.add_argument("--history",  default="incidents_history.json")
    d.add_argument("--actions",  default="actions.yaml")
    d.add_argument("--audit",    default="audit.jsonl")

    # ── batch ──
    b = sub.add_parser("batch", help="Run all eval incidents and produce audit.jsonl")
    b.add_argument("--eval-dir", default="eval",                  help="Directory with E*.json files")
    b.add_argument("--history",  default="incidents_history.json")
    b.add_argument("--actions",  default="actions.yaml")
    b.add_argument("--audit",    default="audit.jsonl")

    args = p.parse_args()

    if args.cmd == "decide":
        return cmd_decide(args)
    elif args.cmd == "batch":
        return cmd_batch(args)
    else:
        p.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
