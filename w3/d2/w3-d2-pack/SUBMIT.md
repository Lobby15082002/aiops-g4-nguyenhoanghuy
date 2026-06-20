# W3-D2 Submission

## 3 thứ mình học được về AIOps pipeline của nhóm

1. **Pipeline chỉ detect được fault khi event được ghi nhận đúng cách** — nếu `POST /events` bị fail (ví dụ pipeline chưa khởi động xong), toàn bộ experiment sẽ bị đánh dấu là FN dù fault đã được inject thành công vào service. Điều này cho thấy chaos engineering không chỉ dùng để kiểm tra application stack mà còn cần kiểm tra độ ổn định của chính monitoring pipeline.

2. **Độ chính xác của RCA không chỉ phụ thuộc vào loại fault mà còn phụ thuộc vào cách xác định target** — ở Exp 8 (network partition), pipeline chọn `frontend` thay vì `api-gateway` vì nó lấy source của partition làm root cause. Điều này cho thấy một hệ thống RCA tốt cần hiểu được topology và dependency giữa các service, thay vì chỉ dựa vào trường target của event.

3. **`disk_fill` và `time_skew` là những blind spot của pipeline lightweight** — trong pipeline hiện tại, `DETECTION_DELAY["disk_fill"] = None`, nghĩa là loại fault này sẽ không bao giờ được detect. Trong thực tế, một AIOps pipeline hoàn chỉnh nên có cơ chế meta-monitoring riêng cho các infrastructure fault thay vì chỉ tập trung vào anomaly ở tầng ứng dụng.

## 1 fault mà mình nghĩ pipeline nên catch nhưng lại bỏ lỡ

* **Experiment:** Exp 10 — `checkout_svc_http500_inject_20pct` (cascade_retry)
* **Lý do mình kỳ vọng sẽ detect được:** checkout-svc inject 20% HTTP 500 khiến payment-svc phải retry nhiều lần, làm số lượng alert ở checkout tăng lên đáng kể. Vì pipeline đã có `http_error` trong `DETECTION_DELAY` với delay = 2s nên mình kỳ vọng alert sẽ xuất hiện trong vòng khoảng 5 giây.
* **Giả thuyết về nguyên nhân miss:** Event được ghi nhận thông qua `POST /events` sau khi fault được inject, nhưng `query_pipeline` lại gọi `/rca` với `window_end = inject_ts + dur + 30`. Do pipeline lưu dữ liệu trong bộ nhớ và event được append sau thời điểm inject, nên truy vấn RCA có thể bị ảnh hưởng bởi các event còn sót lại từ experiment trước. Kết quả là RCA trả về candidate không chính xác. Theo mình, nên tách biệt event theo từng experiment hoặc bổ sung `experiment_id` để việc truy vấn chính xác hơn.

## 1 trade-off trong thiết kế pipeline mà mình muốn xem xét lại

Pipeline hiện tại sử dụng **in-memory EVENTS list**. Cách làm này khá đơn giản, không cần database và dễ triển khai, nhưng đổi lại toàn bộ lịch sử sẽ mất khi pipeline restart.

Trong quá trình làm lab, đây là một vấn đề thực tế vì khi pipeline restart giữa chừng, `/alerts` trả về `[]`, khiến chaos runner đánh giá là FN dù thực tế fault đã xảy ra. Ở lần chạy đầu tiên của `chaos_runner.py`, tất cả kết quả đều có `detected=False` chỉ vì pipeline bị restart.

Nếu được thiết kế lại, mình sẽ cân nhắc dùng append-only log hoặc Redis có TTL. Chi phí triển khai vẫn thấp nhưng dữ liệu sẽ không bị mất sau khi restart, đồng thời giúp kết quả chaos experiment có tính tái lập cao hơn.

## Scoreboard Summary

* detected: 9/10
* rca_correct: 8/9
* mttd_p50: 1.8s
* false_alarms: 0
* verdict: PASS (detected ≥ 7/10 ✓, RCA correct ≥ 5/detected ✓, FA ≤ 1 ✓)
