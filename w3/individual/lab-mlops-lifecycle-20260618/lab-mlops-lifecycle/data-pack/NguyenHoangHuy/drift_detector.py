"""
drift_detector.py — Drift detection using Evidently.

Supports two modes:
  data     : DataDriftPreset — detects P(X) shift
  combined : data drift + performance drift (precision drop vs model predictions)

Usage:
    python drift_detector.py \
        --reference data/baseline.csv \
        --current   data/drifted.csv  \
        --threshold 0.15

    python drift_detector.py \
        --reference    data/baseline.csv \
        --current      data/drifted.csv  \
        --threshold    0.15              \
        --check-mode   combined          \
        --labeled-current data/drifted.csv \
        --model-uri    models:/anomaly-detector@production
"""
import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from evidently.presets import DataDriftPreset
from evidently import Report

try:
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
    PROMETHEUS_ENABLED = True
except ImportError:
    PROMETHEUS_ENABLED = False

# ── Config ────────────────────────────────────────────────────────────────────
TRACKING_URI     = os.getenv("MLFLOW_TRACKING_URI", "sqlite:////home/claude/mlops-lab/mlflow.db")
PUSHGATEWAY_URL  = os.getenv("PUSHGATEWAY_URL",      "http://localhost:9091")
FEATURE_COLS     = ["latency_p99", "error_rate", "rps"]
REPORT_DIR       = Path("outputs/drift_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class DriftResult:
    score:       float
    is_drift:    bool
    report_path: str
    # optional perf fields (combined mode)
    perf_precision: float | None = None
    perf_is_drop:   bool | None  = None


# ── Core: data drift ──────────────────────────────────────────────────────────
def _compute_data_drift(reference_df, current_df, threshold, report_name="drift_report"):
    ref = reference_df[FEATURE_COLS]
    cur = current_df[FEATURE_COLS]

    report = Report([DataDriftPreset()])
    result = report.run(reference_data=ref, current_data=cur)

    # Evidently 0.7.x: extract share of drifted columns
    drift_score = 0.0
    for key, val in result.metric_results.items():
        if hasattr(val, 'share') and hasattr(val.share, 'value'):
            drift_score = float(val.share.value)
            break

    report_path = REPORT_DIR / f"{report_name}.html"
    result.save_html(str(report_path))

    return float(drift_score), str(report_path)


# ── Optional: performance drift (concept drift proxy) ─────────────────────────
def _compute_perf_drift(labeled_current_path: str,
                         model_uri:            str,
                         perf_threshold:       float = 0.80) -> tuple[float, bool]:
    """
    Compare model predictions on labeled_current vs the true labels.
    Returns (precision, is_drop) where is_drop = precision < perf_threshold.
    """
    mlflow.set_tracking_uri(TRACKING_URI)
    model = mlflow.pyfunc.load_model(model_uri)

    df   = pd.read_csv(labeled_current_path)
    if "anomaly_label" not in df.columns:
        print("[drift_detector] WARNING: no anomaly_label column — skipping perf check")
        return (None, None)

    X     = df[FEATURE_COLS]
    y_true = df["anomaly_label"].values  # 0/1

    raw   = model.predict(X)             # +1 (normal) / -1 (anomaly)
    y_pred = np.where(np.array(raw) == -1, 1, 0)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0

    is_drop = precision < perf_threshold
    return float(precision), is_drop


# ── Public API ─────────────────────────────────────────────────────────────────
def detect_drift(reference_df:  pd.DataFrame,
                  current_df:    pd.DataFrame,
                  threshold:     float        = 0.15,
                  report_name:   str          = "drift_report",
                  check_mode:    str          = "data",
                  labeled_current_path: str | None = None,
                  model_uri:     str | None   = None,
                  perf_threshold: float       = 0.80,
                  log_to_mlflow: bool         = True) -> DriftResult:

    # ── Data drift ────────────────────────────────────────────────────────────
    drift_score, report_path = _compute_data_drift(
        reference_df, current_df, threshold, report_name)
    is_drift = drift_score > threshold

    # ── Performance drift (combined mode) ─────────────────────────────────────
    perf_precision = None
    perf_is_drop   = None
    if check_mode == "combined" and labeled_current_path and model_uri:
        perf_precision, perf_is_drop = _compute_perf_drift(
            labeled_current_path, model_uri, perf_threshold)
        # Combined drift flag: either data drift OR perf drop
        is_drift = is_drift or bool(perf_is_drop)

    # ── MLflow logging ────────────────────────────────────────────────────────
    if log_to_mlflow:
        try:
            mlflow.set_tracking_uri(TRACKING_URI)
            with mlflow.start_run(run_name="drift-check") as run:
                mlflow.log_metric("drift_score",    drift_score)
                mlflow.log_metric("drift_threshold", threshold)
                mlflow.log_metric("is_drift",        int(is_drift))
                if perf_precision is not None:
                    mlflow.log_metric("perf_precision", perf_precision)
                mlflow.log_artifact(report_path)
        except Exception as e:
            print(f"[drift_detector] MLflow log warning: {e}")

    # ── Prometheus push ───────────────────────────────────────────────────────
    if PROMETHEUS_ENABLED:
        try:
            registry = CollectorRegistry()
            g_score  = Gauge("drift_score",   "Drift score",   registry=registry)
            g_flag   = Gauge("drift_detected","Drift flag",    registry=registry)
            g_score.set(drift_score)
            g_flag.set(int(is_drift))
            push_to_gateway(PUSHGATEWAY_URL, job="drift_detector", registry=registry)
        except Exception as e:
            print(f"[drift_detector] Prometheus push warning: {e}")

    result = DriftResult(
        score=drift_score,
        is_drift=is_drift,
        report_path=report_path,
        perf_precision=perf_precision,
        perf_is_drop=perf_is_drop,
    )
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference",        default="data/baseline.csv")
    parser.add_argument("--current",          default="data/drifted.csv")
    parser.add_argument("--threshold",        type=float, default=0.15)
    parser.add_argument("--check-mode",       choices=["data", "combined"], default="data")
    parser.add_argument("--labeled-current",  default=None)
    parser.add_argument("--model-uri",        default="models:/anomaly-detector@production")
    parser.add_argument("--perf-threshold",   type=float, default=0.80)
    args = parser.parse_args()

    ref_df = pd.read_csv(args.reference)
    cur_df = pd.read_csv(args.current)

    result = detect_drift(
        reference_df=ref_df,
        current_df=cur_df,
        threshold=args.threshold,
        report_name="drift_report",
        check_mode=args.check_mode,
        labeled_current_path=args.labeled_current,
        model_uri=args.model_uri,
        perf_threshold=args.perf_threshold,
    )

    print(f"\n{'='*55}")
    print(f"  Drift score     : {result.score:.4f}  (threshold={args.threshold})")
    print(f"  Drift detected  : {'YES ⚠️' if result.is_drift else 'NO ✅'}")
    if result.perf_precision is not None:
        print(f"  Perf precision  : {result.perf_precision:.4f}  "
              f"(perf drop={'YES' if result.perf_is_drop else 'NO'})")
    print(f"  Report saved to : {result.report_path}")
    print(f"{'='*55}\n")
