"""
pipeline.py — Train IsolationForest on baseline.csv, log to MLflow,
               register with alias 'production'.

Usage:
    python pipeline.py [--data data/baseline.csv] [--register-alias production]
"""
import argparse
import os
from pathlib import Path

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest

# ── Config ────────────────────────────────────────────────────────────────────
TRACKING_URI   = os.getenv("MLFLOW_TRACKING_URI", "sqlite:////home/claude/mlops-lab/mlflow.db")
MODEL_NAME     = "anomaly-detector"
FEATURE_COLS   = ["latency_p99", "error_rate", "rps"]

# IsolationForest hyperparams
CONTAMINATION  = 0.02   # Expected fraction of anomalies in baseline (~2%)
N_ESTIMATORS   = 100
RANDOM_STATE   = 42


def load_features(csv_path: str) -> np.ndarray:
    df = pd.read_csv(csv_path)
    return df[FEATURE_COLS].values, df


def train(data_path: str, register_alias: str = "production") -> str:
    """Train, log, register, and return the registered model version."""
    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment("anomaly-detector-training")

    X, df = load_features(data_path)
    n_rows, n_features = X.shape

    model = IsolationForest(
        contamination=CONTAMINATION,
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        n_jobs=-1,
    )
    model.fit(X)

    # -1 = anomaly, +1 = normal  →  map to 1/0
    preds = model.predict(X)
    anomaly_rate = (preds == -1).mean()

    with mlflow.start_run(run_name="train-isoforest") as run:
        # Log parameters
        mlflow.log_param("contamination", CONTAMINATION)
        mlflow.log_param("n_estimators",  N_ESTIMATORS)
        mlflow.log_param("random_state",  RANDOM_STATE)
        mlflow.log_param("data_path",     data_path)
        mlflow.log_param("feature_cols",  ",".join(FEATURE_COLS))

        # Log metrics
        mlflow.log_metric("train_anomaly_rate", round(anomaly_rate, 6))
        mlflow.log_metric("feature_count",      n_features)
        mlflow.log_metric("train_rows",         n_rows)

        # Log model artifact and register
        model_info = mlflow.sklearn.log_model(
            sk_model=model,
            artifact_path="model",
            registered_model_name=MODEL_NAME,
        )
        run_id = run.info.run_id

    # ── Set alias on the newly-registered version ─────────────────────────────
    client  = MlflowClient(TRACKING_URI)
    # Find the version we just registered (latest by creation time)
    versions = client.search_model_versions(f"name='{MODEL_NAME}'")
    latest   = sorted(versions, key=lambda v: int(v.version))[-1]
    version_number = latest.version

    client.set_registered_model_alias(MODEL_NAME, register_alias, version_number)
    print(f"[pipeline] Model registered: {MODEL_NAME} v{version_number} → @{register_alias}")
    print(f"[pipeline] Run ID          : {run_id}")
    print(f"[pipeline] train_anomaly_rate = {anomaly_rate:.4f}")
    return version_number


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",           default="data/baseline.csv")
    parser.add_argument("--register-alias", default="production")
    args = parser.parse_args()
    train(args.data, args.register_alias)
