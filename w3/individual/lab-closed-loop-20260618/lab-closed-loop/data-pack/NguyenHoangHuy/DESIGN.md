# DESIGN.md — Các Quyết Định Thiết Kế Của Closed-Loop Orchestrator

## 1. Bộ Máy Ra Quyết Định: Rule-Based hay LLM-Based

**Lựa chọn: Rule-Based (Phương án A)**

Em lựa chọn bộ máy ra quyết định dựa trên luật (rule-based) được triển khai thông qua `runbook_map` tĩnh trong file `config.yaml`:

```yaml
runbook_map:
  HighLatency:    "runbooks/restart_service.sh"
  HighErrorRate:  "runbooks/clear_cache.sh"
  InstanceDown:   "runbooks/restart_service.sh"
  MultiStepDeploy: "runbooks/multi_step_deploy.sh"
```

### Lý do lựa chọn Rule-Based

* **Tính xác định (Determinism):** Trong xử lý sự cố trên môi trường thực tế, một hành động luôn đúng với các cảnh báo đã biết sẽ tốt hơn một quyết định mang tính xác suất. Ví dụ, khi một service bị down thì việc khởi động lại service gần như không có sự mơ hồ trong quyết định.

* **Độ trễ thấp:** Việc tra cứu luật có độ phức tạp O(1), trong khi suy luận bằng LLM thường mất từ 1–3 giây cho mỗi cảnh báo. Điều này đặc biệt quan trọng khi nhiều cảnh báo xuất hiện cùng lúc.

* **Khả năng kiểm toán (Auditability):** Mỗi quyết định đều có thể truy vết trực tiếp tới một dòng cấu hình trong file YAML. Kỹ sư trực ca có thể dễ dàng kiểm tra lý do tại sao một runbook được lựa chọn.

* **Tính an toàn:** Cơ chế whitelist thông qua `runbook_registry` (phòng chống hallucination) vẫn cần thiết đối với cả hai cách tiếp cận. Tuy nhiên, với rule-based, các ánh xạ được cấu hình thủ công nên nguy cơ xảy ra lỗi chỉ đến từ việc cấu hình sai.

### Đánh đổi so với LLM-Based

| Tiêu chí               | Rule-Based         | LLM-Based              |
| ---------------------- | ------------------ | ---------------------- |
| Cảnh báo đã biết       | ✅ Chính xác        | ✅ Tốt                  |
| Cảnh báo mới/chưa biết | ❌ Không xử lý được | ✅ Có khả năng suy luận |
| Độ trễ phản hồi        | ~0 ms              | 1–3 giây               |
| Khả năng kiểm toán     | ✅ Dòng cấu hình    | ⚠ Prompt và phản hồi   |
| Rủi ro hành động sai   | Thấp               | Trung bình             |
| Bảo trì                | Chỉnh sửa YAML     | Prompt Engineering     |

Đối với hệ thống Ronki hiện tại chỉ có 3 loại cảnh báo đã biết, cách tiếp cận rule-based là hoàn toàn đủ và có mức độ rủi ro thấp hơn.

---

## 2. Cấu Hình Blast Radius

```yaml
blast_radius:
  max_actions_per_minute: 3
  max_restarts_per_service_per_hour: 5
```

### `max_actions_per_minute: 3`

Lý do lựa chọn:

Hệ thống Ronki bao gồm 5 service. Nếu cả 5 service cùng phát sinh cảnh báo trong cùng một thời điểm, việc thực hiện hành động trên tất cả service trong vòng 1 phút có thể gây mất an toàn. Ví dụ, việc khởi động lại hàng loạt có thể tạo ra hiện tượng "thundering herd", khiến nhiều service đồng thời kết nối lại tới cơ sở dữ liệu và làm hệ thống quá tải.

Giới hạn 3 hành động mỗi phút cho phép hệ thống ưu tiên xử lý các cảnh báo quan trọng nhất trước. Nếu xuất hiện cảnh báo thứ 4 trong cùng khoảng thời gian, cảnh báo đó sẽ được chuyển cho con người xử lý.

Với chu kỳ polling 15 giây, giới hạn này tương đương khoảng 1 hành động mỗi 2 lần polling, đồng thời vẫn đảm bảo đủ thời gian cho các bước verify hoàn thành.

### `max_restarts_per_service_per_hour: 5`

Lý do lựa chọn:

Nếu một service cần khởi động lại hơn 5 lần trong vòng một giờ thì nguyên nhân gây lỗi có khả năng không còn là sự cố tạm thời mà là lỗi hệ thống nghiêm trọng hơn, chẳng hạn như lỗi mã nguồn hoặc vấn đề hạ tầng.

Giới hạn này cho phép hệ thống có 5 cơ hội tự phục hồi trong vòng một giờ mà không che giấu các lỗi mang tính hệ thống.

Circuit Breaker với ngưỡng 3 lần thất bại liên tiếp đóng vai trò lớp bảo vệ bên trong, trong khi giới hạn 5 lần restart mỗi giờ đóng vai trò lớp bảo vệ bên ngoài nhằm ngăn chặn các vòng lặp restart kéo dài.

---

## 3. Bước Verify: Metric, Ngưỡng và Timeout

### Metric được sử dụng theo từng loại cảnh báo

| Cảnh báo      | Truy vấn Prometheus                                                               | Ngưỡng                        | Điều kiện đạt |
| ------------- | --------------------------------------------------------------------------------- | ----------------------------- | ------------- |
| HighLatency   | `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[1m])) * 1000` | `latency_p99_max_ms = 500 ms` | Giá trị < 500 |
| HighErrorRate | `rate(http_errors_total[2m]) / rate(http_requests_total[2m]) * 100`               | `error_rate_max_pct = 10%`    | Giá trị ≤ 10  |
| InstanceDown  | `up{job="<service>"}`                                                             | `up_required = 1`             | Giá trị ≥ 1   |

### Cấu hình Verify

* **Timeout:** 60 giây
* **Poll interval:** 10 giây
* **Số mẫu tối đa:** 6 mẫu trong khoảng thời gian timeout
* **Điều kiện thành công:** 3 mẫu liên tiếp đạt yêu cầu

### Tại sao cần 3 mẫu liên tiếp?

Một mẫu duy nhất có thể chỉ phản ánh sự phục hồi tạm thời hoặc một biến động ngắn hạn của hệ thống.

Việc yêu cầu 3 mẫu liên tiếp đạt ngưỡng giúp xác nhận rằng service đã thực sự ổn định, thay vì chỉ phục hồi trong thời gian ngắn rồi tiếp tục gặp lỗi.

Cách tiếp cận này giúp giảm nguy cơ xuất hiện false positive, tức là hệ thống kết luận việc khắc phục đã thành công trong khi service vẫn chưa ổn định.

### Tại sao chọn ngưỡng 500 ms?

Giá trị p99 latency thông thường của `payment-svc` khoảng 195 ms, trong khi cảnh báo được kích hoạt khi độ trễ đạt 500 ms.

Do đó, ngưỡng verify được đặt ở mức 500 ms nhằm xác nhận rằng sau khi thực hiện hành động khắc phục, độ trễ đã giảm xuống dưới ngưỡng gây cảnh báo.

Nếu sử dụng ngưỡng quá chặt như 250 ms thì trong điều kiện tải cao bình thường, verify có thể thất bại dù service thực tế đã hoạt động ổn định.

---

## 4. Circuit Breaker: Cơ Chế Reset

**Chế độ reset: Thủ công (Manual)**

```yaml
circuit_breaker:
  consecutive_failure_threshold: 3
  reset_mode: "manual"
```

### Tại sao chọn reset thủ công?

Circuit Breaker sẽ được kích hoạt sau 3 lần thất bại liên tiếp trên cùng một service.

Khi đến lần thất bại thứ ba, điều đó có nghĩa là hệ thống đã thử thực hiện runbook tự động ba lần nhưng đều không thành công.

Nếu cấu hình reset tự động sau vài phút, orchestrator có thể tiếp tục thực hiện cùng một runbook thất bại nhiều lần, tạo thêm nhiễu và thậm chí khiến tình trạng trở nên nghiêm trọng hơn. Ví dụ, liên tục restart một service đang bị memory leak có thể làm hệ thống mất ổn định hơn.

Vì vậy, việc reset thủ công buộc kỹ sư trực ca phải đánh giá nguyên nhân trước khi cho phép hệ thống tiếp tục tự động xử lý.

Sự kiện `CIRCUIT_BREAKER_HALT` trong audit log cung cấp đầy đủ thông tin như:

* Số lần thất bại
* Thời điểm xảy ra
* Runbook gần nhất
* Metric verify gần nhất

Nhờ đó kỹ sư có thể phân tích nguyên nhân trước khi mở lại circuit.

### Cách reset

Sau khi điều tra và xử lý nguyên nhân gốc, kỹ sư chỉ cần khởi động lại tiến trình orchestrator.

Trong tương lai có thể mở rộng bằng cách xây dựng API:

```http
POST /circuit-breaker/reset?service=<name>
```

để thực hiện reset mà không cần dừng hệ thống.

### Đánh đổi so với reset tự động

Reset tự động kết hợp với cơ chế exponential backoff (ví dụ đợi 2^n phút) là một giải pháp hợp lý trong trường hợp lỗi chỉ mang tính tạm thời.

Tuy nhiên, đối với một nền tảng thương mại điện tử, nơi một hành động restart không phù hợp có thể ảnh hưởng tới các đơn hàng đang xử lý, lựa chọn reset thủ công là an toàn hơn.

Chi phí của việc chờ con người xác nhận thường thấp hơn chi phí của việc để hệ thống tiếp tục thực hiện các hành động tự động có khả năng làm tình trạng trở nên nghiêm trọng hơn.
