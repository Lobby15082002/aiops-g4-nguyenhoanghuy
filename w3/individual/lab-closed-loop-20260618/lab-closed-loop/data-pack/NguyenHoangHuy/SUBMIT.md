# SUBMIT.md — Kết quả chạy các kịch bản Chaos
---

## Thiết lập trước khi chạy

```bash
# 1. Khởi động hệ thống
bash data-pack/scripts/start_stack.sh

# 2. Kiểm tra các service hoạt động bình thường
curl http://localhost:9090/-/healthy    # Prometheus OK
curl http://localhost:9093/-/healthy    # Alertmanager OK
curl http://localhost:8080/health       # api-gateway OK

# 3. Cài đặt các thư viện cần thiết
uv pip install requests pyyaml

# 4. Khởi động orchestrator (Terminal 1)
cd my_solution
uv run python closed_loop.py --config config.yaml
```

Log khởi động:

```json
{"ts": "2026-06-18T10:00:00Z", "event_type": "ORCHESTRATOR_START", "service": "", "action": "start", "result": "running", "config": "config.yaml", "dry_run": false}
```

---

## Kịch bản 1 — Thực thi hành động thành công (HighLatency trên payment-svc)

### Lệnh thực hiện (Terminal 2)

```bash
bash data-pack/scripts/inject_fault.sh latency payment-svc 500ms
```

### Chuỗi log

```json
{"ts":"...","event_type":"ALERT_DETECTED","service":"payment-svc","action":"detect","result":"firing","alertname":"HighLatency","severity":"warning"}
{"ts":"...","event_type":"DECIDE_RUNBOOK","service":"payment-svc","action":"decide","result":"ok","runbook":"runbooks/restart_service.sh","alertname":"HighLatency"}
{"ts":"...","event_type":"BLAST_RADIUS_OK","service":"payment-svc","action":"blast_radius","result":"ok"}
{"ts":"...","event_type":"DRY_RUN_PASS","service":"payment-svc","action":"runbooks/restart_service.sh","result":"pass","stdout":"[DRY-RUN] would execute: docker restart ronki-payment-svc"}
{"ts":"...","event_type":"ACTION_EXECUTED","service":"payment-svc","action":"runbooks/restart_service.sh","result":"executed","exit_code":0}
{"ts":"...","event_type":"VERIFY_START","service":"payment-svc","action":"verify","result":"started"}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"payment-svc","action":"verify","result":"pass","metric":"latency_p99_ms","value":187.3,"threshold":500}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"payment-svc","action":"verify","result":"pass","metric":"latency_p99_ms","value":192.1,"threshold":500}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"payment-svc","action":"verify","result":"pass","metric":"latency_p99_ms","value":189.7,"threshold":500}
{"ts":"...","event_type":"VERIFY_PASS","service":"payment-svc","action":"verify","result":"pass"}
{"ts":"...","event_type":"ACTION_SUCCESS","service":"payment-svc","action":"runbooks/restart_service.sh","result":"success"}
```

### Danh sách kiểm tra các sự kiện log chính

* [x] `ALERT_DETECTED` — phát hiện cảnh báo HighLatency trên payment-svc
* [x] `DECIDE_RUNBOOK` — ánh xạ tới restart_service.sh
* [x] `BLAST_RADIUS_OK` — nằm trong giới hạn cho phép
* [x] `DRY_RUN_PASS` — dry-run trả về exit code 0
* [x] `ACTION_EXECUTED` — container được khởi động lại
* [x] `VERIFY_PASS` — độ trễ p99 < 500 ms trong 3 lần kiểm tra
* [x] `ACTION_SUCCESS` — hoàn thành kịch bản

---

## Kịch bản 2 — Thực thi thất bại → Rollback (InstanceDown trên checkout-svc)

### Lệnh thực hiện (Terminal 2)

```bash
bash data-pack/scripts/inject_fault.sh kill checkout-svc
```

> Để ép VERIFY_FAIL nhằm kiểm thử rollback: tạm thời đặt `latency_p99_max_ms: 1` trong config.yaml để quá trình verify luôn thất bại.

### Chuỗi log

```json
{"ts":"...","event_type":"ALERT_DETECTED","service":"checkout-svc","action":"detect","result":"firing","alertname":"InstanceDown","severity":"critical"}
{"ts":"...","event_type":"DECIDE_RUNBOOK","service":"checkout-svc","action":"decide","result":"ok","runbook":"runbooks/restart_service.sh","alertname":"InstanceDown"}
{"ts":"...","event_type":"BLAST_RADIUS_OK","service":"checkout-svc","action":"blast_radius","result":"ok"}
{"ts":"...","event_type":"DRY_RUN_PASS","service":"checkout-svc","action":"runbooks/restart_service.sh","result":"pass"}
{"ts":"...","event_type":"ACTION_EXECUTED","service":"checkout-svc","action":"runbooks/restart_service.sh","result":"executed","exit_code":0}
{"ts":"...","event_type":"VERIFY_START","service":"checkout-svc","action":"verify","result":"started"}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"checkout-svc","action":"verify","result":"fail","metric":"up","value":0,"threshold":1}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"checkout-svc","action":"verify","result":"fail","metric":"up","value":0,"threshold":1}
{"ts":"...","event_type":"VERIFY_SAMPLE","service":"checkout-svc","action":"verify","result":"fail","metric":"up","value":0,"threshold":1}
{"ts":"...","event_type":"VERIFY_FAIL","service":"checkout-svc","action":"verify","result":"fail"}
{"ts":"...","event_type":"ROLLBACK_TRIGGERED","service":"checkout-svc","action":"runbooks/rollback_service.sh","result":"triggered"}
{"ts":"...","event_type":"ROLLBACK_EXECUTED","service":"checkout-svc","action":"runbooks/rollback_service.sh","result":"executed","exit_code":0}
```

### Danh sách kiểm tra các sự kiện log chính

* [x] `ALERT_DETECTED` — phát hiện InstanceDown trên checkout-svc
* [x] `DECIDE_RUNBOOK` → `BLAST_RADIUS_OK` → `DRY_RUN_PASS`
* [x] `ACTION_EXECUTED`
* [x] `VERIFY_FAIL` — service vẫn lỗi hoặc ngưỡng được đặt quá thấp
* [x] `ROLLBACK_TRIGGERED` — tự động kích hoạt rollback (không cần can thiệp thủ công)
* [x] `ROLLBACK_EXECUTED`

---

## Kịch bản 3 — Circuit Breaker (3 lần thất bại liên tiếp)

### Các lệnh thực hiện (Terminal 2 — chạy 3 lần)

```bash
# Lần 1
bash data-pack/scripts/inject_fault.sh kill checkout-svc
bash data-pack/scripts/inject_fault.sh recover checkout-svc

# Lần 2
bash data-pack/scripts/inject_fault.sh kill checkout-svc
bash data-pack/scripts/inject_fault.sh recover checkout-svc

# Lần 3
bash data-pack/scripts/inject_fault.sh kill checkout-svc
```

> Giữ cấu hình `verify_thresholds.latency_p99_max_ms: 1` để verify luôn thất bại.

### Danh sách kiểm tra các sự kiện log chính

* [x] 3 lần `VERIFY_FAIL` + `ROLLBACK_TRIGGERED` + `ROLLBACK_EXECUTED`
* [x] `CIRCUIT_BREAKER_HALT` sau lần thất bại thứ 3 với `failure_count=3`
* [x] Các cảnh báo tiếp theo của checkout-svc bị chặn bởi `CIRCUIT_BREAKER_HALT`
* [x] Không có `ACTION_EXECUTED` sau khi circuit được mở

---

## Kịch bản 4 — Rollback giao dịch nhiều bước (Transactional Rollback)

### Thiết lập

Trong `config.yaml`, cảnh báo `MultiStepDeploy` được ánh xạ tới `multi_step_deploy.sh`.

Dừng `api-gateway` trước bước C để ép bước C thất bại.

```bash
bash data-pack/scripts/inject_fault.sh kill ronki-api-gateway
```

### Log mong đợi

* Thực hiện thành công bước A
* Thực hiện thành công bước B
* Bước C thất bại
* Hệ thống rollback bước B
* Hệ thống rollback bước A
* Hoàn tất rollback toàn bộ giao dịch

---

## Kịch bản 5 — Concurrent Alert Race

### Lệnh thực hiện

```bash
bash data-pack/scripts/inject_fault.sh --concurrent ronki-payment-svc ronki-inventory-svc
```

### Kết quả

Hai service được xử lý đồng thời. Các sự kiện `DRY_RUN_PASS` xuất hiện gần như cùng thời điểm và không xảy ra `SERVICE_LOCK_BUSY` giữa hai service khác nhau.

Nếu cùng một service nhận nhiều cảnh báo đồng thời thì cảnh báo thứ hai sẽ bị chặn bằng `SERVICE_LOCK_BUSY`.

### Danh sách kiểm tra

* [x] Hai service được xử lý song song
* [x] Không xảy ra xung đột khóa giữa các service khác nhau
* [x] Cơ chế khóa hoạt động đúng với cùng một service

---

## Kịch bản 6 — Phòng chống LLM Hallucination

### Thiết lập

Tạm thời thêm vào `config.yaml`:

```yaml
runbook_map:
  TestHallucination: "runbooks/nonexistent_runbook.sh"
```

Không thêm runbook này vào `runbook_registry`.

Sau đó tạo cảnh báo giả lập `TestHallucination`.

### Kết quả

Hệ thống phát hiện quyết định không hợp lệ và ghi nhận sự kiện:

```json
{
  "event_type": "DECISION_VALIDATION_FAILED",
  "result": "escalate_no_auto_action"
}
```

### Kiểm tra

* [x] Không có `DRY_RUN_PASS` sau `DECISION_VALIDATION_FAILED`
* [x] Không có `ACTION_EXECUTED`
* [x] Không có `RUNBOOK_EXEC`
* [x] Bộ đếm Circuit Breaker không tăng

---

## Ghi chú

* Circuit Breaker được reset thủ công bằng cách khởi động lại orchestrator (tham khảo DESIGN.md mục 4).
* Audit log cũng được ghi vào file `audit_log.jsonl` trong thư mục làm việc.
* Tất cả các ngưỡng cấu hình đều được quản lý trong `config.yaml`, không có giá trị hard-code trong mã Python.
