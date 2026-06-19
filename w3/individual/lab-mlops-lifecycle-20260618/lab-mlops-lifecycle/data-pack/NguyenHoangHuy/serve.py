"""
serve.py — FastAPI model server.

Endpoints:
  POST /predict              — run inference
  GET  /health/active-version — return current version
  POST /reload               — reload model from registry

Usage:
    python serve.py
    uvicorn serve:app --host 0.0.0.0 --port 8000 --reload
"""
import os
import time
from contextlib import asynccontextmanager
from typing import List

import mlflow
import mlflow.pyfunc
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from mlflow import MlflowClient
from pydantic import BaseModel

try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server
    PROMETHEUS_ENABLED = True
except ImportError:
    PROMETHEUS_ENABLED = False

# ── Config ────────────────────────────────────────────────────────────────────
TRACKING_URI  = os.getenv("MLFLOW_TRACKING_URI", "sqlite:////home/claude/mlops-lab/mlflow.db")
MODEL_URI     = "models:/anomaly-detector@production"
MODEL_NAME    = "anomaly-detector"
FEATURE_COLS  = ["latency_p99", "error_rate", "rps"]

# ── Prometheus metrics ────────────────────────────────────────────────────────
if PROMETHEUS_ENABLED:
    REQUEST_COUNT   = Counter("serve_requests_total", "Total predict requests")
    REQUEST_LATENCY = Histogram("serve_predict_latency_seconds",
                                "Predict latency", buckets=[.001,.005,.01,.025,.05,.1,.25,.5,1])
    ACTIVE_VERSION  = Gauge("serve_active_version", "Active model version number")

# ── Shared mutable state ──────────────────────────────────────────────────────
state: dict = {"model": None, "version": "unknown"}


def _load_model():
    mlflow.set_tracking_uri(TRACKING_URI)
    client = MlflowClient(TRACKING_URI)
    model  = mlflow.pyfunc.load_model(MODEL_URI)

    # Resolve alias → version number
    alias_mv = client.get_model_version_by_alias(MODEL_NAME, "production")
    version  = alias_mv.version

    state["model"]   = model
    state["version"] = version
    print(f"[serve] Loaded model v{version} from {MODEL_URI}")
    if PROMETHEUS_ENABLED:
        ACTIVE_VERSION.set(float(version))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield


app = FastAPI(title="Anomaly Detector", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────
class PredictRequest(BaseModel):
    features: List[List[float]]   # [[latency, error_rate, rps], ...]


class PredictResponse(BaseModel):
    predictions: List[int]
    scores:      List[float]
    version:     str


# ── Routes ────────────────────────────────────────────────────────────────────
@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    if state["model"] is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    t0 = time.time()
    X  = np.array(req.features)

    if PROMETHEUS_ENABLED:
        REQUEST_COUNT.inc()

    # mlflow pyfunc expects pandas DataFrame or ndarray
    import pandas as pd
    df = pd.DataFrame(X, columns=FEATURE_COLS)

    raw_preds  = state["model"].predict(df)          # +1 / -1
    # IsolationForest via pyfunc returns array; convert -1→1, 1→0
    predictions = [1 if p == -1 else 0 for p in raw_preds]

    # Anomaly score: decision_function (lower = more anomalous)
    sklearn_model = state["model"]._model_impl.python_model if hasattr(
        state["model"], "_model_impl") else None

    # Fallback: use prediction as score proxy
    scores = [float(p) for p in predictions]

    elapsed = time.time() - t0
    if PROMETHEUS_ENABLED:
        REQUEST_LATENCY.observe(elapsed)

    return PredictResponse(
        predictions=predictions,
        scores=scores,
        version=str(state["version"]),
    )


@app.get("/health/active-version")
def active_version():
    return {"version": state["version"], "model_uri": MODEL_URI}


@app.post("/reload")
def reload():
    try:
        _load_model()
        return {"status": "ok", "version": state["version"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "version": state["version"]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
