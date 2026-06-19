#!/usr/bin/env python3
"""
closed_loop.py — Closed-Loop Auto-Remediation Orchestrator
Ronki AIOps Lab — Individual Assignment

Pattern: Detect → Decide → Act → Verify → Rollback
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import requests
import yaml

# Optional: prometheus_client for metrics endpoint (port 9100)
try:
    from prometheus_client import start_http_server as _start_metrics_http
    _PROM_CLIENT_AVAILABLE = True
except ImportError:
    _PROM_CLIENT_AVAILABLE = False


def start_metrics_server(port: int = 9100):
    """Start Prometheus metrics HTTP server so prometheus.yml scrape job sees up=1."""
    if _PROM_CLIENT_AVAILABLE:
        _start_metrics_http(port)
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                          "event_type": "METRICS_SERVER_STARTED",
                          "service": "", "action": "metrics", "result": "ok",
                          "port": port}), flush=True)
    else:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                          "event_type": "METRICS_SERVER_SKIP",
                          "service": "", "action": "metrics", "result": "skip",
                          "reason": "prometheus_client not installed"}), flush=True)

# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------

class JSONLogger:
    def __init__(self, audit_log_path: str):
        self.audit_log_path = audit_log_path
        self._lock = threading.Lock()

    def log(self, event_type: str, service: str = "", action: str = "",
            result: str = "", **kwargs):
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "service": service,
            "action": action,
            "result": result,
            **kwargs,
        }
        line = json.dumps(event)
        print(line, flush=True)
        with self._lock:
            with open(self.audit_log_path, "a") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# Blast-radius tracker
# ---------------------------------------------------------------------------

class BlastRadiusGuard:
    def __init__(self, max_per_minute: int, max_restarts_per_hour: int):
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_restarts_per_hour
        self._minute_window: deque = deque()   # timestamps of all actions
        self._hour_window: dict = defaultdict(deque)  # per service
        self._lock = threading.Lock()

    def check_and_record(self, service: str) -> tuple[bool, str]:
        now = time.time()
        with self._lock:
            # Clean expired entries
            while self._minute_window and now - self._minute_window[0] > 60:
                self._minute_window.popleft()
            while self._hour_window[service] and now - self._hour_window[service][0] > 3600:
                self._hour_window[service].popleft()

            if len(self._minute_window) >= self.max_per_minute:
                return False, f"blast_radius: max_actions_per_minute={self.max_per_minute} exceeded"
            if len(self._hour_window[service]) >= self.max_per_hour:
                return False, f"blast_radius: max_restarts_per_hour={self.max_per_hour} for {service} exceeded"

            self._minute_window.append(now)
            self._hour_window[service].append(now)
            return True, "ok"


# ---------------------------------------------------------------------------
# Circuit breaker (per service)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    def __init__(self, threshold: int):
        self.threshold = threshold
        self._counts: dict = defaultdict(int)
        self._open: set = set()
        self._lock = threading.Lock()

    def is_open(self, service: str) -> bool:
        with self._lock:
            return service in self._open

    def record_failure(self, service: str) -> bool:
        """Returns True if circuit just opened."""
        with self._lock:
            self._counts[service] += 1
            if self._counts[service] >= self.threshold:
                self._open.add(service)
                return True
            return False

    def reset(self, service: str):
        with self._lock:
            self._counts[service] = 0
            self._open.discard(service)

    def record_success(self, service: str):
        with self._lock:
            self._counts[service] = 0


# ---------------------------------------------------------------------------
# Per-service mutex (concurrent alert serialization)
# ---------------------------------------------------------------------------

class ServiceMutex:
    def __init__(self):
        self._locks: dict = {}
        self._meta_lock = threading.Lock()

    def try_acquire(self, service: str) -> bool:
        with self._meta_lock:
            if service not in self._locks:
                self._locks[service] = threading.Lock()
        return self._locks[service].acquire(blocking=False)

    def release(self, service: str):
        with self._meta_lock:
            lock = self._locks.get(service)
        if lock:
            try:
                lock.release()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# Prometheus helper
# ---------------------------------------------------------------------------

class PrometheusClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def query(self, promql: str) -> float | None:
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
                timeout=10,
            )
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if results:
                return float(results[0]["value"][1])
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Runbook executor
# ---------------------------------------------------------------------------

def run_script(script: str, service: str, extra_args: list[str] = None,
               dry_run: bool = False, timeout: int = 120) -> tuple[int, str, str]:
    cmd = ["bash", script, "--service", service]
    if extra_args:
        cmd.extend(extra_args)
    if dry_run:
        cmd.append("--dry-run")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Timeout after {timeout}s"
    except FileNotFoundError:
        return 1, "", f"Script not found: {script}"


# ---------------------------------------------------------------------------
# Verify step
# ---------------------------------------------------------------------------

def verify_action(prom: PrometheusClient, service: str, alert_name: str,
                  cfg: dict, logger: JSONLogger) -> bool:
    thresholds = cfg["verify_thresholds"]
    queries = cfg["prometheus_queries"]
    timeout = thresholds["verify_timeout_seconds"]
    interval = thresholds["verify_poll_interval_seconds"]
    min_samples = thresholds["verify_min_samples"]

    samples_pass = 0
    samples_total = 0
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(interval)

        # Choose metric based on alert type
        if alert_name == "InstanceDown":
            q = queries["up"].replace("{service}", service)
            val = prom.query(q)
            threshold = thresholds["up_required"]
            passed = (val is not None) and (val >= threshold)
            metric_name = "up"
        elif alert_name == "HighErrorRate":
            q = queries["error_rate_pct"].replace("{service}", service)
            val = prom.query(q)
            threshold = thresholds["error_rate_max_pct"]
            passed = (val is not None) and (val <= threshold)
            metric_name = "error_rate_pct"
        else:  # HighLatency default
            q = queries["latency_p99"].replace("{service}", service)
            val = prom.query(q)
            threshold = thresholds["latency_p99_max_ms"]
            passed = (val is not None) and (val < threshold)
            metric_name = "latency_p99_ms"

        samples_total += 1
        if passed:
            samples_pass += 1

        logger.log(
            "VERIFY_SAMPLE",
            service=service,
            action="verify",
            result="pass" if passed else "fail",
            metric=metric_name,
            value=val,
            threshold=threshold,
            sample=samples_total,
        )

        if samples_total >= min_samples:
            if samples_pass >= min_samples:
                return True

    # Final verdict
    return samples_pass >= min_samples


# ---------------------------------------------------------------------------
# Multi-step transactional execution (scenario 4)
# ---------------------------------------------------------------------------

def execute_multi_step(script_name: str, service: str, cfg: dict,
                       logger: JSONLogger, dry_run: bool = False,
                       timeout: int = 120) -> bool:
    """Execute a multi-step deploy; rollback in reverse on failure."""
    multi_cfg = cfg.get("multi_step_map", {}).get(script_name.split("/")[-1], None)
    if not multi_cfg:
        return False

    steps = multi_cfg["steps"]
    rollback_map = multi_cfg["rollback_steps"]
    completed = []

    for step in steps:
        step_id = step["id"]
        args = step["args"]
        rc, out, err = run_script(script_name, service, args, dry_run=dry_run, timeout=timeout)

        if rc == 0:
            logger.log("TRANSACTIONAL_STEP", service=service, action=script_name,
                       result="complete", step_id=step_id, exit_code=rc)
            if not dry_run:
                completed.append(step_id)
        else:
            logger.log("TRANSACTIONAL_STEP_FAIL", service=service, action=script_name,
                       result="fail", step_id=step_id, exit_code=rc,
                       completed_before_failure=completed, stderr=err)
            # Rollback in reverse
            for done_step in reversed(completed):
                rollback_args = rollback_map.get(done_step)
                if rollback_args:
                    rb_rc, rb_out, rb_err = run_script(
                        script_name, service, rollback_args, timeout=timeout
                    )
                    logger.log("TRANSACTIONAL_ROLLBACK_STEP", service=service,
                               action=script_name, result="executed",
                               rolled_back_step=done_step, exit_code=rb_rc,
                               ts_script=script_name)

            logger.log("TRANSACTIONAL_ROLLBACK_COMPLETE", service=service,
                       action=script_name, result="complete",
                       rolled_back=[
                           f"rollback-{s.split('-')[1]}" for s in reversed(completed)
                       ])
            return False

    return True


# ---------------------------------------------------------------------------
# Core alert processor
# ---------------------------------------------------------------------------

def process_alert(alert: dict, cfg: dict, logger: JSONLogger,
                  prom: PrometheusClient, blast: BlastRadiusGuard,
                  cb: CircuitBreaker, mutex: ServiceMutex,
                  orchestrator_dry_run: bool):
    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "")
    service = labels.get("service", labels.get("job", "unknown"))

    logger.log("ALERT_DETECTED", service=service, action="detect",
               result="firing", alertname=alert_name, severity=labels.get("severity", ""))

    # --- Circuit breaker check ---
    if cb.is_open(service):
        logger.log("CIRCUIT_BREAKER_HALT", service=service, action="halt",
                   result="blocked", reason="circuit_open")
        return

    # --- Per-service mutex ---
    if not mutex.try_acquire(service):
        logger.log("SERVICE_LOCK_BUSY", service=service, action="lock",
                   result="skipped", alertname=alert_name)
        return

    try:
        _handle_alert(alert_name, service, cfg, logger, prom, blast, cb,
                      orchestrator_dry_run)
    finally:
        mutex.release(service)


def _handle_alert(alert_name: str, service: str, cfg: dict, logger: JSONLogger,
                  prom: PrometheusClient, blast: BlastRadiusGuard,
                  cb: CircuitBreaker, orchestrator_dry_run: bool):
    runbook_map = cfg["runbook_map"]
    runbook_registry = set(cfg["runbook_registry"])
    rollback_map_cfg = cfg.get("rollback_map", {})
    timeout = cfg.get("runbook_timeout_seconds", 120)

    # --- DECIDE ---
    runbook = runbook_map.get(alert_name)
    if not runbook:
        logger.log("DECIDE_NO_RUNBOOK", service=service, action="decide",
                   result="escalate", alertname=alert_name)
        return

    # --- Hallucination / validation defense ---
    if runbook not in runbook_registry:
        logger.log("DECISION_VALIDATION_FAILED", service=service,
                   action="escalate_no_auto_action",
                   result="validation_failed",
                   bad_runbook=runbook, alertname=alert_name,
                   raw_decision=runbook)
        return  # Do NOT proceed — no subprocess, no circuit breaker increment

    logger.log("DECIDE_RUNBOOK", service=service, action="decide",
               result="ok", runbook=runbook, alertname=alert_name)

    # --- Blast-radius check ---
    ok, reason = blast.check_and_record(service)
    if not ok:
        logger.log("BLAST_RADIUS_EXCEEDED", service=service, action="blast_radius",
                   result="escalate", reason=reason)
        return
    logger.log("BLAST_RADIUS_OK", service=service, action="blast_radius", result="ok")

    # --- DRY-RUN ---
    rc, out, err = run_script(runbook, service, dry_run=True, timeout=timeout)
    if rc != 0:
        logger.log("DRY_RUN_FAIL", service=service, action=runbook, result="fail",
                   exit_code=rc, stderr=err)
        return
    logger.log("DRY_RUN_PASS", service=service, action=runbook, result="pass",
               stdout=out.strip())

    if orchestrator_dry_run:
        logger.log("ORCHESTRATOR_DRY_RUN", service=service, action=runbook,
                   result="skipped", reason="--dry-run flag set")
        return

    # --- ACT ---
    script_name = runbook.split("/")[-1]
    is_multi_step = script_name in cfg.get("multi_step_map", {})

    if is_multi_step:
        success = execute_multi_step(runbook, service, cfg, logger, timeout=timeout)
        if not success:
            cb.record_failure(service)
            return
        logger.log("ACTION_SUCCESS", service=service, action=runbook,
                   result="success", mode="multi_step")
        cb.record_success(service)
        return

    rc, out, err = run_script(runbook, service, timeout=timeout)
    logger.log("ACTION_EXECUTED", service=service, action=runbook,
               result="executed", exit_code=rc, stdout=out.strip(), stderr=err.strip())

    if rc != 0:
        logger.log("ACTION_FAILED", service=service, action=runbook,
                   result="fail", exit_code=rc)
        _do_rollback(runbook, service, rollback_map_cfg, logger, cb, timeout)
        return

    # --- VERIFY ---
    logger.log("VERIFY_START", service=service, action="verify", result="started")
    passed = verify_action(prom, service, alert_name, cfg, logger)

    if passed:
        logger.log("VERIFY_PASS", service=service, action="verify", result="pass")
        logger.log("ACTION_SUCCESS", service=service, action=runbook, result="success")
        cb.record_success(service)
    else:
        logger.log("VERIFY_FAIL", service=service, action="verify", result="fail")
        _do_rollback(runbook, service, rollback_map_cfg, logger, cb, timeout)


def _do_rollback(runbook: str, service: str, rollback_map_cfg: dict,
                 logger: JSONLogger, cb: CircuitBreaker, timeout: int):
    rollback_script = rollback_map_cfg.get(runbook)
    if rollback_script:
        logger.log("ROLLBACK_TRIGGERED", service=service, action=rollback_script,
                   result="triggered")
        rc, out, err = run_script(rollback_script, service, timeout=timeout)
        logger.log("ROLLBACK_EXECUTED", service=service, action=rollback_script,
                   result="executed", exit_code=rc)
    else:
        logger.log("ROLLBACK_TRIGGERED", service=service, action="none",
                   result="no_rollback_defined")

    just_opened = cb.record_failure(service)
    if just_opened:
        logger.log("CIRCUIT_BREAKER_HALT", service=service, action="circuit_breaker",
                   result="CIRCUIT_OPEN",
                   failure_count=cb._counts[service],
                   reason="consecutive_failure_threshold_reached")


# ---------------------------------------------------------------------------
# Alert poller
# ---------------------------------------------------------------------------

class AlertPoller:
    def __init__(self, alertmanager_url: str):
        self.url = alertmanager_url
        self._seen: set = set()

    def poll(self) -> list[dict]:
        try:
            resp = requests.get(self.url, timeout=10)
            alerts = resp.json()
            new_alerts = []
            for a in alerts:
                if a.get("status", {}).get("state") != "active":
                    continue
                fingerprint = a.get("fingerprint", "")
                if fingerprint and fingerprint not in self._seen:
                    self._seen.add(fingerprint)
                    new_alerts.append(a)
            return new_alerts
        except Exception as e:
            return []

    def clear_seen(self):
        """Reset seen set (call periodically so re-fires are caught)."""
        self._seen.clear()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Closed-Loop Auto-Remediation Orchestrator")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--dry-run", action="store_true",
                        help="Orchestrator dry-run: detect + decide + dry-run only, no execution")
    parser.add_argument("--metrics-port", type=int, default=9100,
                        help="Prometheus metrics server port (default: 9100)")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # Start metrics server FIRST so prometheus sees up=1 immediately
    start_metrics_server(args.metrics_port)

    logger = JSONLogger(cfg.get("audit_log_path", "audit_log.jsonl"))
    prom = PrometheusClient(cfg["prometheus_url"])
    blast = BlastRadiusGuard(
        cfg["blast_radius"]["max_actions_per_minute"],
        cfg["blast_radius"]["max_restarts_per_service_per_hour"],
    )
    cb = CircuitBreaker(cfg["circuit_breaker"]["consecutive_failure_threshold"])
    mutex = ServiceMutex()
    poller = AlertPoller(cfg["alertmanager_url"])
    poll_interval = cfg.get("poll_interval_seconds", 15)

    logger.log("ORCHESTRATOR_START", action="start", result="running",
               config=args.config, dry_run=args.dry_run)

    # Reset seen every 5 minutes so recurring alerts are re-processed
    last_reset = time.time()

    while True:
        try:
            if time.time() - last_reset > 300:
                poller.clear_seen()
                last_reset = time.time()

            alerts = poller.poll()

            if alerts:
                threads = []
                for alert in alerts:
                    t = threading.Thread(
                        target=process_alert,
                        args=(alert, cfg, logger, prom, blast, cb, mutex, args.dry_run),
                        daemon=True,
                    )
                    threads.append(t)
                    t.start()
                # Don't join — let concurrent processing happen

        except KeyboardInterrupt:
            logger.log("ORCHESTRATOR_STOP", action="stop", result="shutdown")
            sys.exit(0)
        except Exception as e:
            logger.log("ORCHESTRATOR_ERROR", action="error", result="exception",
                       error=str(e))

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
