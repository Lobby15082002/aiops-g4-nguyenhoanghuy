# W3-D1 Design Decisions
## 1. SLI choice cho frontend

Khi nhìn vào data `frontend_rum.jsonl`, em thấy mỗi event có mấy field có thể dùng làm SLI: `dom_ready_ms`, `js_error`, `network_error`. Ban đầu em cũng phân vân không biết chọn cái nào, nhưng sau khi đọc lại §2.1 về 3 tiêu chí SLI quality thì em quyết định chọn kết hợp `js_error=false AND network_error=false` làm SLI chính.

Lý do chọn cái này vì nó đáp ứng đủ 3 tiêu chí: measurable (có field boolean rõ ràng trong log), user-side (đo trực tiếp từ browser của user), và proportional với user pain (khi js_error hoặc network_error xảy ra thì user thực sự không tương tác được với trang). Baseline tính ra `success_rate = 98.6%` với công thức này, con số này phản ánh đúng thực tế service.

Còn `dom_ready_ms` thì em không chọn làm SLI chính vì khó xác định ngưỡng cut hợp lý — baseline ghi nhận `dom_ready_p99 = 1430ms`, nếu đặt threshold 3000ms thì quá rộng, 1000ms thì lại quá chặt dễ gây false positive. Nên em để nó làm signal phụ quan sát thôi, không đưa vào SLI.

---

## 2. SLO target cho api

Baseline thực tế của api là `success_rate = 97.6%` (từ `baseline.json`). Ban đầu em thử đặt target 99.0% nhưng sau khi chạy validate thì bị `fn=2` — tức là bỏ sót 2 incident. Lý do là target quá thấp so với SLO làm cho burn rate threshold 14.4 trở nên quá cao, không nhạy đủ để detect incident nhỏ.

em điều chỉnh lên `target = 0.997` (99.7%) thì kết quả cải thiện rõ rệt: `fn` về 0 và `fp` cũng về 0. Tại sao không chọn 99.99%? Vì nhìn vào §3.2 cost ladder, để đạt 99.99% cần Multi-AZ, automated runbook, 24/7 on-call — với một e-commerce API thông thường thì chi phí đó không xứng đáng. 99.9% thì cần multi-instance với auto-failover, 99.7% là con số thực tế phù hợp với baseline hiện tại và không đòi hỏi overinvest về infra.

---

## 3. Latency threshold p99

Baseline ghi nhận `latency_p99_ms = 156ms` cho api trong điều kiện bình thường (3 ngày synthetic data). em chọn ngưỡng latency cut ở **500ms** vì đây là con số phổ biến cho user-facing API — dưới 500ms thì user cảm nhận response là "nhanh", trên đó bắt đầu có cảm giác chờ.

Tại sao không chọn 200ms? Vì baseline p99 là 156ms, nếu đặt threshold 200ms thì buffer chỉ còn 44ms — quá chặt, bất kỳ spike nhỏ nào cũng trigger. Tại sao không chọn 1s? Vì 1s đã là ngưỡng user bắt đầu bỏ trang, để đến lúc đó mới alert thì muộn rồi. 500ms là điểm cân bằng giữa không quá nhạy và không quá trễ. Nếu sau này traffic tăng và p99 baseline dịch lên thì em sẽ re-evaluate lại threshold này.

---

## 4. 4xx exclusion

em loại 4xx (trừ 429) ra khỏi error count vì 4xx là lỗi từ phía client, không phải lỗi hệ thống. Ví dụ 404 Not Found hay 400 Bad Request là do user gửi request sai — hệ thống xử lý đúng rồi, không nên tính vào SLI failure.

Còn 429 (Too Many Requests) thì em giữ lại vì đây là trường hợp hệ thống chủ động reject user do rate limiting — user muốn dùng nhưng bị từ chối, đó là user pain thật sự. Trong data `access_log.jsonl`, em kiểm tra thấy có các request trả về status 5xx trong các incident window, trong khi 4xx xuất hiện rải rác đều đặn kể cả lúc hệ thống bình thường — điều này confirm rằng 4xx là client error pattern bình thường, không liên quan đến health của service. Nếu đếm 4xx vào fail thì SLI sẽ bị kéo xuống bởi behavior của bot hay scraper, không phản ánh đúng trải nghiệm user thật.

---

## 5. MWMBR tuning

em không dùng Google default threshold 14.4 cho Tier 1 mà tune xuống còn **7**. Lý do là sau khi chạy validate với threshold 14.4, kết quả cho thấy `mttd_delta_s = 180s` — tức là MWMBR detect chậm hơn static baseline 3 phút. Hạ xuống 10 thì còn 120s, hạ xuống 7 thì `mttd_delta_s = 60s` và verdict chuyển sang `pass`.

Về ảnh hưởng đến các metric khác: `noise_reduction_pct` vẫn giữ ở 86.4% (so với static baseline fired 22 lần, MWMBR chỉ fired 3 lần đều là TP), `fn` vẫn = 0 — tức là không bỏ sót incident nào. Việc hạ threshold không làm tăng false positive vì short window (5m) đóng vai trò filter: chỉ fire khi CẢ HAI long window VÀ short window đều vượt threshold, nên spike ngắn sẽ không trigger nhầm. Trade-off là threshold thấp hơn có thể nhạy hơn với noise trong môi trường production thật, nên cần monitor thêm sau khi deploy.