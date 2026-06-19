# SUBMIT.md — Lab Reflection

## Q1. What drift threshold did you choose and why? Did you validate it against real data?

**Threshold: 0.15** (share of drifted columns theo Evidently `DataDriftPreset`).

Cách derive: chạy `drift_detector.py` trên 70/30 split của `baseline.csv` — cùng distribution, không có real drift — kết quả drift score = 0.00 (0/3 features bị flag). Threshold được đặt ở 0.15 để tạo buffer trước sampling variance, theo heuristic `baseline_score × 1.5` làm lower bound.

Validate thực tế trên `drifted.csv`: latency mean tăng từ 128.9ms lên 162.4ms (+26%), error_rate tăng từ 0.791 lên 1.482 (+87%). Evidently trả về `drift_score = 1.0000` — tất cả 3 features đều drift, vượt xa threshold 0.15. Khoảng cách giữa no-drift score (0.00) và real-drift score (1.00) rất lớn, xác nhận threshold 0.15 hoạt động đúng và không cần điều chỉnh thêm.

---

## Q2. What happens if model v2 after retraining performs worse than v1 in production? How does your pipeline handle this case?

Pipeline có **hai lớp bảo vệ** độc lập nhau.

Lớp 1 — **Holdout gate trước promotion**: trước khi hỏi approval, `retrain.py` evaluate cả v1 lẫn v2 trên `holdout.csv` (500 rows từ old distribution). Nếu v2 precision < v1 precision, script in cảnh báo và abort — v2 ở lại `@staging`, v1 tiếp tục serve. Trong run thực tế: v1 precision = 0.0, v2 precision = 0.0 → tied → gate pass.

Lớp 2 — **Auto-rollback sau promotion**: sau khi v2 được promote lên `@production`, pipeline chạy 24 monitoring cycles trên `post_deploy_eval.csv`. Mỗi cycle tính v2 precision; nếu precision < 0.65, pipeline tự động: (1) reassign `@production → v1`, (2) reassign `@archived → v2`, (3) gọi `POST /reload` trên serve.py, (4) ghi event `auto_rollback_v2_to_v1` vào `outputs/audit_log.jsonl` với fields `demoted_version`, `restored_version`, `trigger_precision`, `cycle`. Trong run thực tế v2 đạt precision = 1.0 suốt 24 cycles nên rollback không xảy ra — đây là behavior đúng vì `post_deploy_eval.csv` được generate với clear anomaly pattern.

---

## Q3. What is the difference between data drift and concept drift? Which type does Evidently detect in this lab?

**Data drift**: phân phối input features P(X) thay đổi — ví dụ latency mean tăng từ 128.9ms lên 162.4ms. Mối quan hệ giữa features và label P(Y|X) không đổi: nếu trước đây latency cao là anomaly thì sau drift nó vẫn là anomaly, chỉ là "cao" bây giờ có giá trị tuyệt đối khác.

**Concept drift**: mối quan hệ P(Y|X) thay đổi — cùng feature values nhưng label đã đảo ngược. Trong lab này, `drifted.csv` chứa 252/1008 rows bị flip label (25%), mô phỏng việc payment processor mới thay đổi pattern lỗi: những gì trước đây là anomaly nay là normal và ngược lại.

Evidently `DataDriftPreset` chỉ detect **data drift** — nó áp dụng K-S test và Jensen-Shannon divergence lên từng feature column, không bao giờ nhìn vào label column. Trong run thực tế, `--check-mode data` trả về drift_score = 1.0 nhưng hoàn toàn bỏ sót concept drift. Chỉ khi dùng `--check-mode combined` mới thấy `perf_precision = 0.3184` — con số này phơi bày rằng model đang sai 68% predictions dù features trông bình thường.

---

## Q4. Why is a blue-green swap more important than simply replacing the model file directly?

Thay file trực tiếp (overwrite `model.pkl`) trong khi serve.py đang chạy tạo ra race condition: request đang in-flight có thể đọc file ở trạng thái partially-written, gây crash hoặc predict với model corrupt. Quan trọng hơn: không có rollback path — version cũ đã bị ghi đè.

Blue-green swap trong lab này dùng **MLflow aliases làm routing layer**. `serve.py` luôn load `models:/anomaly-detector@production` — đây là pointer tới artifact bất biến trên registry. Swap là atomic: `client.set_registered_model_alias("anomaly-detector", "production", "2")` cập nhật một record trong SQLite database, không động đến file nào đang được serve. Sau đó `POST /reload` được gọi để serve.py fetch alias mới — request in-flight tiếp tục với v1, request tiếp theo dùng v2.

Ngoài ra, `GET /health/active-version` cho phép verify chính xác version nào đang được serve trước khi declare cutover thành công. Rollback là một alias update + reload — không cần file operation, không downtime, không mất observability.

---

## Q5. If you had to automate the approval gate (no human required), what metric and threshold would you use?

Dùng **hai điều kiện AND**:

```python
if v2_precision >= v1_precision * 0.95 and v2_recall >= 0.80:
    # auto-promote
else:
    # keep staging, alert on-call
```

**v2_precision ≥ v1_precision × 0.95**: cho phép v2 sacrifice tối đa 5% precision so với v1 — chấp nhận được vì v2 được train trên mixed distribution nên precision trên holdout (old pattern) có thể hơi thấp hơn. Nếu dùng equality strict (v2 ≥ v1), gate sẽ quá nghiêm khi cả hai đều = 0.0 như trong lab này.

**v2_recall ≥ 0.80**: recall là metric quan trọng hơn precision trong payment anomaly detection — false negative (bỏ sót fraud) đắt hơn false positive (alert nhầm). Floor 0.80 đảm bảo model không quá conservative.

Trong code `retrain.py`, thay block `input()` bằng:

```python
if v2_precision >= v1_precision * 0.95 and v2_recall >= 0.80:
    client.set_registered_model_alias(MODEL_NAME, "production", v2_version)
    _reload_serve()
    _log_audit("auto_promoted", reason="passed_precision_recall_gate")
else:
    _log_audit("auto_promotion_rejected",
               v2_precision=v2_precision, v2_recall=v2_recall)
```

Gate này vẫn không phải "fully automatic với no control" — nó vẫn có điều kiện rõ ràng, logged, và alertable. Sự khác biệt với human gate là thời gian phản hồi: milliseconds thay vì phút/giờ.
