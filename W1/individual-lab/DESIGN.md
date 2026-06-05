# Detection Approach — DESIGN.md

## Approach tôi dùng
**Sliding Window + Multi-Signal Threshold Detection**

## Tại sao chọn approach này
Streaming data có noise cao — một điểm dữ liệu bất thường duy nhất có thể chỉ là nhiễu ngẫu nhiên.
Sliding window giúp làm mượt dữ liệu bằng cách lấy trung bình N điểm gần nhất, giảm false positive
đáng kể. Kỹ thuật này nhẹ (O(1) mỗi tick), không cần lưu toàn bộ history, phù hợp với streaming
real-time mà không gây độ trễ xử lý.

## Cách hoạt động
Mỗi khi nhận 1 payload từ generator, pipeline cập nhật 4 sliding window (kích thước 20 điểm) cho
các metric: `http_requests_per_sec`, `queue_depth`, `http_p99_latency_ms`, `http_5xx_rate`. Sau đó
tính trung bình rolling của từng window và so sánh với ngưỡng được tính dựa trên khoảng bình thường
trong đề bài. Alert được phân cấp:
- **critical**: ≥ 2 signal vượt ngưỡng đồng thời (multi-signal correlation)
- **warning**: 1 signal vượt 70% ngưỡng, hoặc 2 signal nhẹ hơn kết hợp

Để tránh alert storm, pipeline có cooldown 10 tick giữa các lần fire.

## Parameters tôi chọn
| Parameter | Giá trị | Lý do |
|---|---|---|
| `WINDOW_SIZE` | 20 | ~1 phút production (speed=10, 3s/tick × 20 = 1 phút real = 10 phút prod) |
| `RPS_SPIKE_THRESHOLD` | 400 req/s | 3x baseline bình thường (80–160), traffic_spike có thể lên 8x |
| `QUEUE_SPIKE_THRESHOLD` | 50 | 5–10x normal queue depth (bình thường 2–10) |
| `LATENCY_SPIKE_THRESHOLD` | 300ms | ~5x normal p99 (bình thường 35–65ms) |
| `RATE_5XX_CRITICAL` | 10% | Vượt xa normal range (0–0.8%), rõ ràng là sự cố |
| Alert cooldown | 10 tick | Tránh spam, giữ alerts.jsonl readable |

## Cải thiện nếu có thêm thời gian
- **Baseline tự học**: Thay vì hardcode threshold, tính mean + stddev trong 5 phút đầu chạy, sau đó dùng Z-score (> 3σ là anomaly). Kháng nhiễu tốt hơn với workload thay đổi theo giờ.
- **Detection đa loại fault**: Hiện tại pipeline tối ưu cho `traffic_spike`. Nên thêm heuristic riêng cho `memory_leak` (trend tăng đơn điệu của memory) và `dependency_timeout` (upstream_timeout_rate tăng trước 5xx).
- **Exponential smoothing (EWMA)**: Thay sliding window bằng EWMA cho phản ứng nhanh hơn với đột biến mà vẫn mượt.
