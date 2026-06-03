"""
Mock Streaming Pipeline — W1/D3
================================
Simulates a Kafka-style producer/consumer pipeline:
  Producer: reads CSV row-by-row → puts into queue.Queue (fake Kafka)
  Consumer: reads from queue → extracts rolling features → saves to parquet

Usage:
    uv run python pipeline.py
    uv run python pipeline.py --input path/to/your.csv
    uv run python pipeline.py --mode threaded   # bonus: run producer+consumer concurrently
"""

import argparse
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path

import pandas as pd


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ROLLING_WINDOW = 12          # 12 rows × 5 min = 1-hour rolling window
RATE_OF_CHANGE_LAG = 1       # compare with 1 step back (5 minutes ago)
OUTPUT_PARQUET = "features.parquet"
OUTPUT_JSONL = "events.jsonl"
SENTINEL = None              # signal producer is done


# ─────────────────────────────────────────────
# PRODUCER
# Reads CSV row by row → emits into queue (fake Kafka)
# ─────────────────────────────────────────────
def producer(csv_path: str, q: queue.Queue, use_jsonl: bool = False):
    """
    Reads machine_temperature_system_failure.csv row by row.
    Each row = 1 "event" pushed to the queue.
    Also optionally appends to events.jsonl (fake Kafka log).
    """
    print(f"[Producer] Starting — reading from {csv_path}")

    jsonl_file = open(OUTPUT_JSONL, "w") if use_jsonl else None

    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    total = len(df)

    for i, row in df.iterrows():
        event = {
            "timestamp": row["timestamp"].isoformat(),
            "value": row["value"],
        }

        # Push to queue (fake Kafka produce)
        q.put(event)

        # Optionally write to events.jsonl
        if jsonl_file:
            jsonl_file.write(json.dumps(event) + "\n")

        # Progress log every 5000 rows
        if (i + 1) % 5000 == 0:
            print(f"[Producer] Emitted {i + 1}/{total} events...")

    # Signal consumer that we're done
    q.put(SENTINEL)

    if jsonl_file:
        jsonl_file.close()

    print(f"[Producer] Done — emitted {total} events total.")


# ─────────────────────────────────────────────
# FEATURE EXTRACTOR
# Stateful: maintains a rolling buffer of recent values
# ─────────────────────────────────────────────
class FeatureExtractor:
    """
    Mimics what Flink/Spark Streaming does at scale:
    maintain state (rolling window) per stream key,
    compute features on each new event.
    """

    def __init__(self, window_size: int = ROLLING_WINDOW):
        self.window_size = window_size
        self.buffer = deque(maxlen=window_size)  # rolling window of recent values

    def update(self, event: dict) -> dict | None:
        """
        Process 1 event. Returns feature dict if enough data, else None.
        """
        value = event["value"]
        self.buffer.append(value)

        # Need at least 2 points for rate of change
        if len(self.buffer) < 2:
            return None

        values = list(self.buffer)

        # Rolling mean — "what's the average temperature lately?"
        rolling_mean = sum(values) / len(values)

        # Rolling std — "how much is temperature fluctuating?"
        mean = rolling_mean
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        rolling_std = variance ** 0.5

        # Rate of change — "how fast is temperature changing right now?"
        rate_of_change = values[-1] - values[-2]

        # Z-score — "how unusual is this value vs recent history?"
        z_score = (value - rolling_mean) / rolling_std if rolling_std > 0 else 0.0

        return {
            "timestamp": event["timestamp"],
            "value": value,
            "rolling_mean": round(rolling_mean, 4),
            "rolling_std": round(rolling_std, 4),
            "rate_of_change": round(rate_of_change, 4),
            "z_score": round(z_score, 4),
            "window_size": len(values),
        }


# ─────────────────────────────────────────────
# CONSUMER
# Reads from queue → extracts features → collects results
# ─────────────────────────────────────────────
def consumer(q: queue.Queue, results: list):
    """
    Reads events from queue one by one (like a Kafka consumer).
    Extracts features using a rolling window.
    Appends feature dicts to results list.
    """
    print(f"[Consumer] Starting — window size = {ROLLING_WINDOW} rows ({ROLLING_WINDOW * 5} min)")

    extractor = FeatureExtractor(window_size=ROLLING_WINDOW)
    processed = 0
    skipped = 0

    while True:
        event = q.get()

        # Sentinel = producer is done
        if event is SENTINEL:
            break

        features = extractor.update(event)

        if features:
            results.append(features)
            processed += 1
        else:
            skipped += 1  # not enough history yet

        q.task_done()

    print(f"[Consumer] Done — {processed} feature rows extracted, {skipped} skipped (warmup).")


# ─────────────────────────────────────────────
# SAVE OUTPUT
# ─────────────────────────────────────────────
def save_output(results: list):
    if not results:
        print("[Output] No features to save.")
        return

    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Save as parquet (columnar, efficient for downstream ML)
    df.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"[Output] Saved {len(df)} rows → {OUTPUT_PARQUET}")

    # Show sample
    print("\n── Sample output (first 5 rows) ──")
    print(df.head().to_string(index=False))

    print(f"\n── Feature stats ──")
    print(df[["rolling_mean", "rolling_std", "rate_of_change", "z_score"]].describe().round(3).to_string())


# ─────────────────────────────────────────────
# MAIN — Single-threaded mode (default)
# ─────────────────────────────────────────────
def run_single_threaded(csv_path: str, use_jsonl: bool):
    """
    Producer and consumer run in the same thread.
    Producer fills the queue completely, then consumer drains it.
    Simple, easy to debug.
    """
    print("=" * 50)
    print("Mode: single-threaded")
    print("=" * 50)

    q = queue.Queue()
    results = []

    start = time.time()

    producer(csv_path, q, use_jsonl=use_jsonl)
    consumer(q, results)
    save_output(results)

    elapsed = time.time() - start
    print(f"\n[Done] Total time: {elapsed:.2f}s")


# ─────────────────────────────────────────────
# MAIN — Threaded mode (bonus)
# Producer and consumer run concurrently, like real Kafka
# ─────────────────────────────────────────────
def run_threaded(csv_path: str, use_jsonl: bool):
    """
    Producer runs in a separate thread, consumer in main thread.
    More realistic — producer emits while consumer processes simultaneously.
    """
    print("=" * 50)
    print("Mode: threaded (producer + consumer concurrently)")
    print("=" * 50)

    q = queue.Queue(maxsize=1000)  # backpressure: producer blocks if consumer is slow
    results = []

    start = time.time()

    # Start producer in background thread
    producer_thread = threading.Thread(
        target=producer,
        args=(csv_path, q),
        kwargs={"use_jsonl": use_jsonl},
        daemon=True,
    )
    producer_thread.start()

    # Consumer runs in main thread
    consumer(q, results)

    # Wait for producer to finish
    producer_thread.join()

    save_output(results)

    elapsed = time.time() - start
    print(f"\n[Done] Total time: {elapsed:.2f}s")


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock streaming pipeline for machine temperature data")
    parser.add_argument(
        "--input",
        default="realKnownCause/machine_temperature_system_failure.csv",
        help="Path to input CSV file",
    )
    parser.add_argument(
        "--mode",
        choices=["single", "threaded"],
        default="single",
        help="single: sequential | threaded: concurrent producer+consumer (bonus)",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Also write raw events to events.jsonl (fake Kafka log)",
    )
    args = parser.parse_args()

    csv_path = args.input
    if not Path(csv_path).exists():
        print(f"[Error] File not found: {csv_path}")
        print("Usage: uv run python pipeline.py --input path/to/machine_temperature_system_failure.csv")
        exit(1)

    if args.mode == "threaded":
        run_threaded(csv_path, use_jsonl=args.jsonl)
    else:
        run_single_threaded(csv_path, use_jsonl=args.jsonl)