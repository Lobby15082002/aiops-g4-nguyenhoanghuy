"""
retrain.py — Orchestrator: detect drift → retrain → stage → approve → promote.

Stress cases handled:
  - Sliding window (baseline + drift) to prevent overfitting on drift window only
  - Holdout validation: v2 precision must be ≥ v1 precision on holdout.csv
  - Auto-rollback: monitors v2 for 24 cycles; rolls back if precision < 0.65

Usage:
    # Basic
    python retrain.py --reference data/baseline.csv --current data/drifted.csv

    # With holdout validation (Stress 2)
    python retrain.py --reference data/baseline.csv --current data/drifted.csv \
        --holdout data/holdout.csv

    # Full stress test (Stress 3)
    python retrain.py --reference data/baseline.csv --current data/drifted.csv \
        --holdout data/holdout.csv \
        --post-deploy-eval data/post_deploy_eval.csv
"""
import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest

from drift_detector import detect_drift

# ── Config ────────────────────────────────────────────────────────────────────
TRACKING_URI     = os.getenv("MLFLOW_TRACKING_URI", "sqlite:////home/claude/mlops-lab/mlflow.db")
SERVE_URL        = os.getenv("SERVE_URL",            "http://localhost:8000")
MODEL_NAME       = "anomaly-detector"
FEATURE_COLS     = ["latency_p99", "error_rate", "rps"]
DRIFT_THRESHOLD  = 0.15
PERF_THRESHOLD   = 0.65   # auto-rollback trigger
POLL_CYCLES      = 24
CONTAMINATION    = 0.04   # slightly higher for mixed drift data
N_ESTIMATORS     = 150
RANDOM_STATE     = 42

AUDIT_LOG        = Path("outputs/audit_log.jsonl")
AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, push_to_gateway
    PROMETHEUS_ENABLED = True
    PUSHGATEWAY_URL = os.getenv("PUSHGATEWAY_URL", "http://localhost:9091")
except ImportError:
    PROMETHEUS_ENABLED = False


# ── Utilities ─────────────────────────────────────────────────────────────────
def _log_audit(event: str, **kwargs):
    record = {"timestamp": datetime.utcnow().isoformat(), "event": event, **kwargs}
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[audit] {record}")


def _push_prometheus(retrain_count: int = 0, rollback_count: int = 0):
    if not PROMETHEUS_ENABLED:
        return
    try:
        reg = CollectorRegistry()
        Gauge("retrain_count",   "Retrain events",   registry=reg).set(retrain_count)
        Gauge("rollback_count",  "Rollback events",  registry=reg).set(rollback_count)
        push_to_gateway(PUSHGATEWAY_URL, job="retrain_orchestrator", registry=reg)
    except Exception as e:
        print(f"[retrain] Prometheus push warning: {e}")


def _reload_serve():
    """Call POST /reload on serve.py if it is running."""
    try:
        import httpx
        r = httpx.post(f"{SERVE_URL}/reload", timeout=5)
        print(f"[retrain] serve.py reloaded → version {r.json().get('version')}")
    except Exception as e:
        print(f"[retrain] serve.py reload skipped (not running): {e}")


def _get_model_version(client: MlflowClient, alias: str) -> str | None:
    try:
        mv = client.get_model_version_by_alias(MODEL_NAME, alias)
        return mv.version
    except Exception:
        return None


def _evaluate_model(model, df: pd.DataFrame) -> tuple[float, float]:
    """Return (precision, recall) given a labeled DataFrame."""
    if "anomaly_label" not in df.columns:
        return (None, None)
    X      = df[FEATURE_COLS].values
    y_true = df["anomaly_label"].values

    raw    = model.predict(X)
    y_pred = np.where(raw == -1, 1, 0)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


# ── Train a new model ─────────────────────────────────────────────────────────
def _train_v2(train_df: pd.DataFrame,
              register_alias: str = "staging") -> tuple[str, object]:
    """Train on train_df, register as alias, return (version_number, sklearn_model)."""
    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment("anomaly-detector-retrain")

    X = train_df[FEATURE_COLS].values
    model = IsolationForest(
        contamination=CONTAMINATION,
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    model.fit(X)

    preds        = model.predict(X)
    anomaly_rate = (preds == -1).mean()

    with mlflow.start_run(run_name="retrain-v2") as run:
        mlflow.log_param("contamination",   CONTAMINATION)
        mlflow.log_param("n_estimators",    N_ESTIMATORS)
        mlflow.log_param("random_state",    RANDOM_STATE)
        mlflow.log_param("train_rows",      len(train_df))
        mlflow.log_param("window_strategy", "sliding_baseline_plus_drift")
        mlflow.log_metric("train_anomaly_rate", round(anomaly_rate, 6))
        mlflow.log_metric("feature_count",      X.shape[1])
        mlflow.set_tag("trigger", "drift_detected")

        mlflow.sklearn.log_model(
            sk_model=model,
            artifact_path="model",
            registered_model_name=MODEL_NAME,
        )

    client   = MlflowClient(TRACKING_URI)
    versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    latest   = sorted(versions, key=lambda v: int(v.version))[-1]
    version  = latest.version
    client.set_registered_model_alias(MODEL_NAME, register_alias, version)
    print(f"[retrain] v{version} registered as @{register_alias}")
    return version, model


# ── Holdout validation ────────────────────────────────────────────────────────
def _holdout_validation(v1_model,
                         v2_model,
                         holdout_path: str,
                         v1_version: str,
                         v2_version: str) -> bool:
    """Return True if v2 precision >= v1 precision on holdout set."""
    df = pd.read_csv(holdout_path)
    p1, r1 = _evaluate_model(v1_model, df)
    p2, r2 = _evaluate_model(v2_model, df)

    print(f"\n[holdout] Holdout validation — v1 precision: {p1:.4f}  recall: {r1:.4f}")
    print(f"[holdout] Holdout validation — v2 precision: {p2:.4f}  recall: {r2:.4f}")

    _log_audit("holdout_validation",
               v1_version=v1_version, v2_version=v2_version,
               v1_precision=p1, v1_recall=r1,
               v2_precision=p2, v2_recall=r2,
               passed=bool(p2 is not None and p1 is not None and p2 >= p1))

    if p2 is None or p1 is None:
        print("[holdout] No labels available — skipping holdout gate")
        return True
    if p2 < p1:
        print(f"[holdout] ⚠️  v2 precision ({p2:.4f}) < v1 precision ({p1:.4f}) on holdout!")
        return False
    print(f"[holdout] ✅ v2 passed holdout gate")
    return True


# ── Auto-rollback monitor ─────────────────────────────────────────────────────
def _post_deploy_monitor(v2_model,
                          v2_version: str,
                          v1_version: str,
                          post_deploy_path: str,
                          client: MlflowClient) -> bool:
    """Monitor v2 for POLL_CYCLES; rollback if precision < PERF_THRESHOLD."""
    df        = pd.read_csv(post_deploy_path)
    rollback  = False

    for cycle in range(1, POLL_CYCLES + 1):
        # Simulate polling by using the same dataset (in real life: streaming window)
        p2, r2 = _evaluate_model(v2_model, df)
        print(f"[post_deploy_monitor] Cycle {cycle:02d}/{POLL_CYCLES}  "
              f"v2 precision={p2:.4f}  recall={r2:.4f}")

        if p2 is not None and p2 < PERF_THRESHOLD:
            print(f"\n[post_deploy_monitor] 🚨 Precision {p2:.4f} < {PERF_THRESHOLD} — "
                  f"triggering auto-rollback!")
            # Demote v2, restore v1
            client.set_registered_model_alias(MODEL_NAME, "archived", v2_version)
            client.delete_registered_model_alias(MODEL_NAME, "production")
            client.set_registered_model_alias(MODEL_NAME, "production", v1_version)

            _log_audit("auto_rollback_v2_to_v1",
                       demoted_version=v2_version,
                       restored_version=v1_version,
                       trigger_precision=p2,
                       cycle=cycle)
            _push_prometheus(retrain_count=1, rollback_count=1)
            _reload_serve()
            print(f"Rollback complete. v1 restored to @production. v2 → @archived")
            rollback = True
            break

        time.sleep(0.05)   # simulated polling interval

    return rollback


# ── Main orchestrator ─────────────────────────────────────────────────────────
def run(reference_path:   str,
        current_path:     str,
        holdout_path:     str | None = None,
        post_deploy_path: str | None = None):

    mlflow.set_tracking_uri(TRACKING_URI)
    client = MlflowClient(TRACKING_URI)

    print("\n" + "="*60)
    print("  RETRAIN ORCHESTRATOR")
    print("="*60)

    # ── Step 1: detect drift ──────────────────────────────────────────────────
    ref_df = pd.read_csv(reference_path)
    cur_df = pd.read_csv(current_path)

    print(f"\n[step 1] Running drift detection ...")
    result = detect_drift(
        reference_df=ref_df,
        current_df=cur_df,
        threshold=DRIFT_THRESHOLD,
        report_name="retrain_drift_report",
        check_mode="combined",
        labeled_current_path=current_path,
        model_uri=f"models:/{MODEL_NAME}@production",
        perf_threshold=0.80,
    )

    print(f"[step 1] Drift score={result.score:.4f}  is_drift={result.is_drift}")
    if result.perf_precision is not None:
        print(f"[step 1] Perf precision={result.perf_precision:.4f}  "
              f"perf drop={result.perf_is_drop}")

    if not result.is_drift:
        print("[step 1] No drift detected — no retraining needed.")
        return

    _log_audit("drift_detected",
               drift_score=result.score,
               threshold=DRIFT_THRESHOLD,
               perf_precision=result.perf_precision)

    # ── Step 2: build sliding-window training set ─────────────────────────────
    print(f"\n[step 2] Building sliding-window dataset ...")
    # Strategy: use 50 % of baseline (most recent rows) + all drift window
    half_baseline = ref_df.tail(len(ref_df) // 2)
    train_df      = pd.concat([half_baseline, cur_df], ignore_index=True)
    print(f"[step 2] Training set: {len(half_baseline)} baseline + "
          f"{len(cur_df)} drift = {len(train_df)} rows")

    # ── Step 3: load current v1 model for comparison ──────────────────────────
    v1_version = _get_model_version(client, "production")
    print(f"\n[step 3] Current production version: v{v1_version}")

    try:
        import mlflow.sklearn as mlsk
        v1_run_model = mlsk.load_model(f"models:/{MODEL_NAME}/{v1_version}")
    except Exception as e:
        print(f"[step 3] Could not load v1 sklearn model: {e}")
        v1_run_model = None

    # ── Step 4: train v2 → register as staging ────────────────────────────────
    print(f"\n[step 4] Training v2 on sliding window ...")
    v2_version, v2_sklearn = _train_v2(train_df, register_alias="staging")

    # ── Step 5: holdout validation (Stress 2) ─────────────────────────────────
    if holdout_path and v1_run_model is not None:
        print(f"\n[step 5] Holdout validation ...")
        passed = _holdout_validation(
            v1_run_model, v2_sklearn,
            holdout_path, v1_version, v2_version)
        if not passed:
            print("[step 5] ❌ v2 failed holdout — aborting promotion.")
            _log_audit("holdout_failed_abort", v2_version=v2_version)
            return
    else:
        print(f"\n[step 5] Skipping holdout validation (no holdout path or v1 model)")

    # ── Step 6: approval gate ─────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Drift detected (score={result.score:.4f} > threshold={DRIFT_THRESHOLD}).")
    print(f"  Model v{v2_version} registered as @staging.")
    ans = input("  Promote to production? [y/N]: ").strip().lower()
    if ans != "y":
        print("[step 6] Promotion declined — v2 remains @staging.")
        _log_audit("promotion_declined", v2_version=v2_version)
        return

    # ── Step 7: promote staging → production ─────────────────────────────────
    print(f"\n[step 7] Promoting v{v2_version}: staging → production ...")
    client.set_registered_model_alias(MODEL_NAME, "production", v2_version)
    # Archive previous production version
    if v1_version:
        client.set_registered_model_alias(MODEL_NAME, "archived", v1_version)
        print(f"[step 7] v{v1_version} → @archived")

    _reload_serve()
    _push_prometheus(retrain_count=1, rollback_count=0)
    _log_audit("promoted_to_production",
               v1_version=v1_version,
               v2_version=v2_version,
               drift_score=result.score)
    print(f"[step 7] ✅ v{v2_version} is now @production")

    # ── Step 8: post-deploy monitor + auto-rollback (Stress 3) ────────────────
    if post_deploy_path:
        print(f"\n[step 8] Starting post-deploy monitor ({POLL_CYCLES} cycles) ...")
        _post_deploy_monitor(
            v2_sklearn, v2_version, v1_version,
            post_deploy_path, client)
    else:
        print("\n[step 8] No post-deploy eval path — skipping auto-rollback monitor.")

    print("\n" + "="*60)
    print("  Orchestration complete.")
    print("="*60 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference",        default="data/baseline.csv")
    parser.add_argument("--current",          default="data/drifted.csv")
    parser.add_argument("--holdout",          default=None)
    parser.add_argument("--post-deploy-eval", default=None)
    args = parser.parse_args()

    run(
        reference_path=args.reference,
        current_path=args.current,
        holdout_path=args.holdout,
        post_deploy_path=args.post_deploy_eval,
    )
