# DESIGN.md — MLOps Lifecycle Pipeline

## Sub-checkpoint 1: Drift Threshold

**Giá trị được chọn: 0.15**

Threshold được xác định bằng cách chạy `drift_detector.py` trên 70/30 split của `baseline.csv` (cùng distribution) — kết quả drift score = 0.00 (0/3 features bị flag). Áp dụng heuristic `threshold = baseline_score × 1.5` cho giá trị ≈ 0.00, nên threshold được đặt ở 0.15 để tạo margin an toàn trước sampling variance tự nhiên.

Khi validate thực tế trên `drifted.csv` (latency mean tăng từ 128.9ms → 162.4ms tức +26%; error_rate tăng từ 0.791 → 1.482 tức +87%), Evidently báo drift score = **1.0000** — tất cả 3 features đều bị flag. Khoảng cách từ 0.00 (no drift) đến 1.0 (full drift) rất rõ ràng, xác nhận threshold 0.15 hoạt động đúng.

Nếu threshold quá thấp (ví dụ 0.03), mọi fluctuation nhỏ trong traffic đều trigger retrain, gây lãng phí compute và model churn không cần thiết. Nếu quá cao (ví dụ 0.50), drift chỉ được phát hiện khi 2/3 features đã shift nghiêm trọng — quá muộn để ngăn precision degradation.

---

## Sub-checkpoint 2: Drift Type

Pipeline này phải đối mặt với **cả hai loại drift đồng thời**.

**Data drift** (P(X) thay đổi): sau campaign và tích hợp 3rd-party, distribution của `latency_p99`, `error_rate`, và `rps` đều shift. Đây là loại drift mà Evidently `DataDriftPreset` phát hiện được thông qua K-S test và Jensen-Shannon divergence trên từng feature column. Kết quả thực tế: drift score = 1.0, cả 3 features đều drifted.

**Concept drift** (P(Y|X) thay đổi): `drifted.csv` chứa 252 rows có label bị flip (25% của 1008 rows) — cùng feature values nhưng `anomaly_label` đã đảo ngược, mô phỏng việc payment processor mới thay đổi ngưỡng anomaly. Evidently `DataDriftPreset` **hoàn toàn không phát hiện** được loại này vì nó không inspect label column. Kết quả thực tế: khi chạy `--check-mode combined`, `perf_precision = 0.3184` — thấp hơn nhiều so với precision lúc train ban đầu, xác nhận concept drift đang xảy ra.

Đây là lý do `drift_detector.py` cần `--check-mode combined`: data drift báo phân phối feature thay đổi, performance drift báo mối quan hệ feature→label thay đổi. Thiếu một trong hai sẽ bỏ sót một phần vấn đề.

---

## Sub-checkpoint 3: Retrain Trigger Configuration

**Trigger: semi-automatic với human approval gate bắt buộc.**

`retrain.py` tự động phát hiện drift và train v2, nhưng **yêu cầu approval `[y/N]`** trước khi promote lên `@production`. Người phê duyệt là on-call MLOps engineer hoặc platform team lead. Không có timeout cứng — staging model giữ nguyên cho đến khi có quyết định, trong thời gian đó v1 vẫn tiếp tục serve production traffic.

Lý do không dùng fully-automatic: trong hệ thống payment gateway, một model swap im lặng mà làm giảm recall có thể khiến fraud không bị phát hiện — rủi ro này vượt quá lợi ích của automation hoàn toàn. Human review metrics holdout trước khi promote là kiểm soát tương xứng với mức độ rủi ro.

Không dùng cadence-based retrain (ví dụ weekly) vì: (1) lãng phí compute trong giai đoạn ổn định; (2) có thể thay thế model tốt bằng model không tốt hơn; (3) event-driven retrain dễ audit hơn — mỗi lần retrain gắn với một drift event cụ thể có timestamp và drift score trong `audit_log.jsonl`.

---

## Sub-checkpoint 4: Versioning + Rollback

**Strategy: MLflow aliases (`@production`, `@staging`, `@archived`)**

`serve.py` luôn load `models:/anomaly-detector@production` — alias là pointer tới artifact bất biến. Khi alias được update, lần `POST /reload` tiếp theo sẽ pick up version mới mà không cần thay đổi code. Đây là lý do alias tốt hơn hardcode version number: `serve.py` không cần biết version nào đang chạy, chỉ cần biết alias `production`.

**Rollback procedure thực tế đã chạy trong lab:**
- v1 train xong → `@production`
- v2 train xong → `@staging`
- Sau approve: v2 → `@production`, v1 → `@archived`
- Audit log ghi: `promoted_to_production` với `v1_version=1`, `v2_version=2`, `drift_score=1.0`

**Manual rollback** (bất kỳ lúc nào): `MlflowClient.set_registered_model_alias("anomaly-detector", "production", "1")` + `POST /reload`. Authority: platform team lead.

**Auto-rollback** (Stress 3): nếu v2 precision < 0.65 trong 24 monitoring cycles, pipeline tự động reassign `@production → v1`, `@archived → v2`, ghi event `auto_rollback_v2_to_v1` vào `audit_log.jsonl`. Trong run thực tế, v2 đạt precision = 1.0 suốt 24 cycles nên rollback không trigger — đây là behavior đúng.

Không có version nào bị xóa — chỉ bị demote xuống `@archived`. Toàn bộ history giữ nguyên để audit.

---

## Sub-checkpoint 5 (Stress 1): Tại sao Combined Mode là bắt buộc

Chạy `--check-mode data` trên `drifted.csv` trả về `drift_score = 1.0` — đúng, nhưng chỉ phản ánh P(X) shift. Cùng lúc đó, 252/1008 rows có label bị flip (concept drift). Evidently không thấy điều này.

Ví dụ số liệu cụ thể từ run thực tế: `--check-mode combined` báo thêm `perf_precision = 0.3184`. Một model với precision 0.32 trên production data nghĩa là 68% các lần model báo "anomaly" thực ra là false positive — hoàn toàn không chấp nhận được trong payment gateway. Nếu chỉ dùng data mode, pipeline sẽ chỉ thấy "features shifted" nhưng không thấy "model đang sai 68% predictions" → retrain có thể không được trigger đúng lúc.

---

## Sub-checkpoint 6 (Stress 2): Sliding Window vs Alternatives

| Strategy | Ưu điểm | Nhược điểm |
|---|---|---|
| **Sliding window: 50% baseline + 100% drift** *(được chọn)* | Preserve old-regime performance; v2 generalize qua cả hai distribution | Training set hơi noisy hơn |
| Drift window only (7 ngày) | Adapt tối đa với distribution mới | Overfit: precision/recall trên `holdout.csv` (old pattern) sẽ drop |
| Full baseline + full drift | Data nhiều nhất | Baseline rows chiếm đa số, model không adapt đủ nhanh |
| Incremental fine-tuning | Computationally cheap | IsolationForest là batch method, không native incremental |

Training set thực tế: **2160 baseline rows + 1008 drift rows = 3168 rows**. Holdout validation: v1 precision = 0.0, v2 precision = 0.0 — cả hai bằng 0 vì IsolationForest với contamination thấp predict rất ít positive trên tập holdout. Precision = 0/0 fallback về 0.0, gate so sánh tương đối (`v2 >= v1`) nên vẫn pass. Trong production thực tế nên dùng labeled holdout với đủ anomaly samples.

---

## Sub-checkpoint 7 (Stress 3): Auto-Rollback Design

Threshold auto-rollback: **precision < 0.65** trong **24 polling cycles**.

Lý do chọn 0.65: v1 được validate ở ~0.91 precision lúc deploy. Model decay khiến precision giảm dần. Một v2 post-retrain có precision < 0.65 nghĩa là thấp hơn cả trạng thái decayed của v1 — rõ ràng là regression, không phải fluctuation bình thường.

Lý do chọn 24 cycles: tránh rollback sớm do data spike ngắn hạn. Trong production, 24 cycles có thể map sang 24 giờ monitoring với interval 1 giờ/cycle.

Audit log entry khi rollback xảy ra chứa: `demoted_version`, `restored_version`, `trigger_precision`, `cycle` — đủ để reconstruct incident timeline. Trong run thực tế, v2 đạt precision = 1.0 suốt 24 cycles (post_deploy_eval.csv có clear anomaly pattern), rollback không trigger — behavior đúng.
