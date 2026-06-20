# Chaos Engineering Report — W3-D2

## 1. Setup

- Stack: w3-d2-pack (Docker Compose, 12 mock microservices)
- Pipeline: aiops-pipeline FastAPI on port 8000 (apps/pipeline_app.py)
- Baseline window: stack up ~60s trước khi chạy experiment đầu
- Total experiments run: 10
- Synthetic probe: synthetic_probe.sh → http://localhost:8080/checkout/health → probe.log

## 2. Results Table

==== Chaos Run ====
Total: 10
Detected: 9/10
RCA correct: 8/9
False alarms in baseline windows: 0
Precision: 1.00
Recall: 0.90
MTTD p50: 1.8s, p95: 3.2s

Per-experiment:

| # | name                                    | detected | mttd  | rca_service   | rca_correct |
|---|-----------------------------------------|----------|-------|---------------|-------------|
| 1 | payment_latency_500ms                   | Y        | 1.8s  | payment-svc   | Y           |
| 2 | payment_packet_loss_30pct               | Y        | 1.6s  | payment-svc   | Y           |
| 3 | inventory_pod_kill                      | Y        | 1.6s  | inventory-svc | Y           |
| 4 | api_gateway_cpu_stress_90pct            | Y        | 2.3s  | api-gateway   | Y           |
| 5 | payment_db_memory_fill_95pct            | Y        | 1.1s  | payment-db    | Y           |
| 6 | auth_svc_clock_skew_60s                 | Y        | 2.9s  | auth-svc      | Y           |
| 7 | log_collector_disk_fill_95pct           | Y        | 2.8s  | log-collector | Y           |
| 8 | frontend_api_gateway_full_partition_30s | Y        | 0.9s  | frontend      | N           |
| 9 | dns_resolver_slow_lookup_2s             | Y        | 3.2s  | dns-resolver  | Y           |
|10 | checkout_svc_http500_inject_20pct       | N        | —     | —             | —           |

Gaps identified:
- Exp 8: RCA picked 'frontend' instead of 'api-gateway' → topology resolution weakness
- Exp 10: FN — cascade_retry / http_error fault not detected by pipeline

## 3. Detailed Per-Experiment Analysis

**Exp 1 — payment_latency_500ms**
- Hypothesis: inject +500ms latency vào payment-svc, detector fire latency anomaly, RCA pick payment-svc
- Observed: detected=True, MTTD=1.8s, RCA=payment-svc
- Match expected? Yes — pipeline detect và RCA đúng root

**Exp 2 — payment_packet_loss_30pct**
- Hypothesis: 30% packet loss trên payment-svc, detector fire error_rate anomaly, RCA pick payment-svc
- Observed: detected=True, MTTD=1.6s, RCA=payment-svc
- Match expected? Yes

**Exp 3 — inventory_pod_kill**
- Hypothesis: kill inventory-svc pod, detector fire availability alert, RCA pick inventory-svc
- Observed: detected=True, MTTD=1.6s, RCA=inventory-svc
- Match expected? Yes

**Exp 4 — api_gateway_cpu_stress_90pct**
- Hypothesis: CPU 90% trên api-gateway gây latency cascade, detector fire multi-service alert
- Observed: detected=True, MTTD=2.3s, RCA=api-gateway
- Match expected? Yes

**Exp 5 — payment_db_memory_fill_95pct**
- Hypothesis: memory fill 95% trên payment-db, detector fire connection pool alert, RCA pick payment-db không phải payment-svc
- Observed: detected=True, MTTD=1.1s, RCA=payment-db
- Match expected? Yes — RCA phân biệt đúng DB vs service

**Exp 6 — auth_svc_clock_skew_60s**
- Hypothesis: clock skew +60s gây JWT validation fail, detector fire auth error, RCA pick auth-svc
- Observed: detected=True, MTTD=2.9s, RCA=auth-svc
- Match expected? Yes

**Exp 7 — log_collector_disk_fill_95pct**
- Hypothesis: disk fill 95% trên log-collector, meta-monitoring catch log ingestion lag
- Observed: detected=True, MTTD=2.8s, RCA=log-collector
- Match expected? Yes — đáng chú ý: pipeline detect được meta-monitoring fault

**Exp 8 — frontend_api_gateway_full_partition_30s**
- Hypothesis: network partition giữa frontend và api-gateway, tất cả downstream timeout, RCA pick api-gateway (edge)
- Observed: detected=True, MTTD=0.9s, RCA=frontend (WRONG — expected api-gateway)
- Match expected? No — RCA pick source của partition thay vì edge destination
- Lý do: pipeline dùng `target` field của event, mà target resolve về `frontend` (side đầu tiên của "frontend ↔ api-gateway"). Topology-aware RCA cần biết partition direction để pick đúng edge

**Exp 9 — dns_resolver_slow_lookup_2s**
- Hypothesis: DNS slow lookup +2s, service discovery chậm, detector fire, RCA phụ thuộc topology
- Observed: detected=True, MTTD=3.2s, RCA=dns-resolver
- Match expected? Yes

**Exp 10 — checkout_svc_http500_inject_20pct**
- Hypothesis: 20% HTTP 500 inject trên checkout-svc tạo retry storm, RCA KHÔNG được pick checkout dựa vào alert count
- Observed: detected=False — pipeline hoàn toàn miss fault này
- Lý do: `cascade_retry` fault type map sang `http_error` payload, nhưng pipeline không fire alert cho experiment này do timing issue — event được record sau khi query window đã đóng

## 4. Gap Analysis — Top 3 Pipeline Weakness

**Gap 1: RCA topology resolution cho network partition (Exp 8)**
- Symptom: Exp 8, RCA pick `frontend` thay vì `api-gateway`
- Likely cause: RCA chỉ dùng `target` field (source của partition) thay vì resolve destination edge. Correlator không có dependency graph để biết api-gateway là downstream critical path
- Recommended fix: Implement topology-aware RCA (§7.3) — với network_partition fault, RCA phải traverse dependency graph và pick service ở phía downstream của cut, không phải source

**Gap 2: Cascade retry / http_error không detect được (Exp 10)**
- Symptom: Exp 10, detected=False — FN hoàn toàn
- Likely cause: `cascade_retry` fault inject vào checkout-svc qua `http_error` payload, nhưng alert fire_ts rơi ngoài query window do cooldown timing. Pipeline không có mechanism detect retry storm pattern riêng biệt
- Recommended fix: Thêm retry_storm detector — monitor `payment_retry_count` tăng đột biến trong khi `checkout_error_rate` cao. Detector nên có window dài hơn (300s) cho cascade pattern

**Gap 3: Monitoring dependency loop risk (Exp 7 — partial)**
- Symptom: log-collector disk fill được detect, nhưng nếu pipeline phụ thuộc vào log-collector để scrape metrics thì sẽ bị blind khi log-collector fail
- Likely cause: §7.5 pattern — AIOps stack chạy trên cùng observability infrastructure mà nó monitor
- Recommended fix: AIOps pipeline phải có observability stack riêng biệt (separate Prometheus, separate log path) không depend vào monitored services

## 5. Hypothesis Cho Gap Chưa Khẳng Định

- **Gap 2 (cascade_retry)**: Cần thêm experiment với window query dài hơn (t+300s thay vì t+90s) để xác nhận pipeline có detect được retry storm nếu đủ thời gian không, hay là blind spot thật sự của detector
- **Gap 1 (partition direction)**: Cần experiment inject partition từ api-gateway phía → frontend để xem RCA có flip không — xác nhận bug là direction-dependent hay topology-independent