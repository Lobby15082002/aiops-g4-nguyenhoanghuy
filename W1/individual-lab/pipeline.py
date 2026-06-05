from fastapi import FastAPI, Request
import json
import uvicorn
from collections import deque
from datetime import datetime, timezone
import os

app = FastAPI()

ALERTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.jsonl")

# ─── Sliding window lưu N datapoint gần nhất ───────────────────────────────
WINDOW_SIZE = 20   # 20 điểm = ~1 phút production (với speed=10)

window_rps       = deque(maxlen=WINDOW_SIZE)   # http_requests_per_sec
window_queue     = deque(maxlen=WINDOW_SIZE)   # queue_depth
window_latency   = deque(maxlen=WINDOW_SIZE)   # http_p99_latency_ms
window_5xx       = deque(maxlen=WINDOW_SIZE)   # http_5xx_rate

# Đếm số lần WARN/ERROR trong log gần đây
window_warn_count = deque(maxlen=WINDOW_SIZE)

# Tránh spam alert — chỉ fire mỗi 10 tick
last_alert_tick = -10
tick_count = 0

# ─── Ngưỡng phát hiện traffic_spike ────────────────────────────────────────
# Dựa vào bảng normal range trong đề bài:
#   rps bình thường: 80–160  → spike khi vượt 3x baseline (> 400)
#   queue_depth bình thường: 2–10 → spike khi > 50
#   latency p99 bình thường: 35–65ms → spike khi > 300ms
#   5xx_rate bình thường: 0–0.8% → critical khi > 10%

RPS_SPIKE_THRESHOLD      = 400.0   # req/s
QUEUE_SPIKE_THRESHOLD    = 50      # count
LATENCY_SPIKE_THRESHOLD  = 300.0   # ms
RATE_5XX_WARNING         = 3.0     # %
RATE_5XX_CRITICAL        = 10.0    # %


def write_alert(timestamp: str, alert_type: str, severity: str, message: str):
    alert = {
        "timestamp": timestamp,
        "type": alert_type,
        "severity": severity,
        "message": message,
    }
    with open(ALERTS_FILE, "a") as f:
        f.write(json.dumps(alert) + "\n")
    print(f"[ALERT] {severity.upper()} | {alert_type} | {message}")


def avg(dq):
    return sum(dq) / len(dq) if dq else 0


def detect_anomaly(metrics: dict, logs: list, timestamp: str):
    global last_alert_tick, tick_count

    rps      = metrics["http_requests_per_sec"]
    queue    = metrics["queue_depth"]
    latency  = metrics["http_p99_latency_ms"]
    rate_5xx = metrics["http_5xx_rate"]

    # Đếm WARN/ERROR trong batch log này
    warn_count = sum(1 for l in logs if l.get("level") in ("WARN", "ERROR", "FATAL"))

    # Cập nhật window
    window_rps.append(rps)
    window_queue.append(queue)
    window_latency.append(latency)
    window_5xx.append(rate_5xx)
    window_warn_count.append(warn_count)

    tick_count += 1

    # Cần ít nhất WINDOW_SIZE/2 điểm trước khi bắt đầu phán xét
    if tick_count < WINDOW_SIZE // 2:
        return

    # Không spam — chờ ít nhất 10 tick giữa các alert
    if tick_count - last_alert_tick < 10:
        return

    avg_rps     = avg(window_rps)
    avg_queue   = avg(window_queue)
    avg_latency = avg(window_latency)
    avg_5xx     = avg(window_5xx)

    # ── CRITICAL: nhiều dấu hiệu cùng lúc ──────────────────────────────────
    critical_signals = 0
    evidence_parts = []

    if avg_rps > RPS_SPIKE_THRESHOLD:
        critical_signals += 1
        evidence_parts.append(f"avg RPS={avg_rps:.0f} (normal 80-160)")

    if avg_queue > QUEUE_SPIKE_THRESHOLD:
        critical_signals += 1
        evidence_parts.append(f"avg queue={avg_queue:.0f} (normal 2-10)")

    if avg_latency > LATENCY_SPIKE_THRESHOLD:
        critical_signals += 1
        evidence_parts.append(f"avg latency={avg_latency:.0f}ms (normal 35-65ms)")

    if avg_5xx > RATE_5XX_CRITICAL:
        critical_signals += 1
        evidence_parts.append(f"5xx_rate={avg_5xx:.1f}% (critical)")

    if critical_signals >= 2:
        last_alert_tick = tick_count
        evidence = "; ".join(evidence_parts)
        write_alert(
            timestamp,
            "traffic_spike",
            "critical",
            f"Traffic spike detected — {evidence}"
        )
        return

    # ── WARNING: 1 dấu hiệu đơn nhưng rõ ràng ──────────────────────────────
    if avg_rps > RPS_SPIKE_THRESHOLD * 0.7:   # 70% ngưỡng
        last_alert_tick = tick_count
        write_alert(
            timestamp,
            "traffic_spike",
            "warning",
            f"RPS elevated: avg={avg_rps:.0f} req/s — possible traffic spike building up"
        )
        return

    if avg_queue > QUEUE_SPIKE_THRESHOLD * 0.6 and avg_latency > LATENCY_SPIKE_THRESHOLD * 0.5:
        last_alert_tick = tick_count
        write_alert(
            timestamp,
            "traffic_spike",
            "warning",
            f"Queue depth={avg_queue:.0f} + latency={avg_latency:.0f}ms rising together"
        )
        return

    if avg_5xx > RATE_5XX_WARNING:
        last_alert_tick = tick_count
        write_alert(
            timestamp,
            "traffic_spike",
            "warning",
            f"Error rate rising: 5xx={avg_5xx:.1f}% — may indicate overload"
        )


@app.post("/ingest")
async def ingest(request: Request):
    payload    = await request.json()
    metrics    = payload["metrics"]
    logs       = payload["logs"]
    timestamp  = payload["timestamp"]

    detect_anomaly(metrics, logs, timestamp)

    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok", "ticks": tick_count}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
