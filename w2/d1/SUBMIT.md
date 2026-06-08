# SUBMIT.md - W2D1: Alert Correlation
## Design Decisions

**gap_sec = 30, vì sao?**

Em chọn `gap_sec = 30` vì sau khi debug thử với 30, 60, 120 thì thấy với `gap_sec = 60` hoặc `120`, toàn bộ 20 alert rơi vào 1 session duy nhất vì các alert liên tiếp nhau chỉ cách 15-30 giây. Với `gap_sec = 30` thì dataset tách thành 5 session có nghĩa hơn - session 0 gom đúng incident chính (12 alert payment cascade), còn recommender và search tách riêng ra đúng với label "unrelated" trong data.

**max_hop = 2, vì sao?**

Em giữ `max_hop = 2` theo khuyến nghị trong bài. Với `max_hop = 1` thì chỉ gom được service kề nhau trực tiếp, bỏ sót cascade 2 bước như `edge-lb → checkout-svc → payment-svc`. Với `max_hop = 3` thì graph rộng quá, `search-svc` và `recommender-svc` có thể bị kéo vào cluster chính dù không liên quan. `max_hop = 2` là điểm cân bằng hợp lý nhất.

**Alert bị "miss" - tại sao?**

`recommender-svc` (a-0013) rơi vào session riêng vì nó cách alert trước đó 40 giây - vừa đủ vượt `gap_sec = 30`. Ngoài ra trong graph, `recommender-svc` không kết nối gần với `payment-svc` hay `checkout-svc` trong vòng 2 hop, nên topology cũng không kéo nó vào cluster chính. Đây thực ra là đúng vì data có ghi note "unrelated - concurrent batch retrain".

**Nếu có 10000 alert, code chậm ở đâu?**

Chậm nhất ở hàm `topology_group()` - phần so sánh từng cặp service dùng `nx.shortest_path_length()`. Với N service thì số cặp là O(N²), mỗi cặp lại chạy BFS trên graph. Với 20 alert thì ổn, nhưng nếu 10000 alert có 500 service khác nhau thì số cặp lên tới 125,000 lần gọi BFS - rất chậm. Cách fix là precompute toàn bộ shortest path một lần bằng `nx.all_pairs_shortest_path_length()` rồi lookup dict thay vì tính lại mỗi lần.

---

## EOD Checkpoint

**Câu 1: Vì sao fingerprint không include timestamp hay value?**

Vì `timestamp` và `value` thay đổi mỗi lần alert fire - nếu include thì 2 alert cùng service, cùng metric nhưng fire lúc khác nhau sẽ có fingerprint khác nhau hoàn toàn, dedup không hoạt động được. Ví dụ: `payment-svc latency_p99_ms crit` fire lúc `09:42:01` với value `1200ms`, rồi fire lại lúc `09:44:30` với value `1840ms` - đây rõ ràng là cùng 1 vấn đề nhưng nếu include timestamp/value thì hệ thống tạo ra 2 cluster riêng thay vì gom lại, engineer nhìn vào thấy 2 "vấn đề khác nhau" trong khi thực ra chỉ là 1.

**Câu 2: Duplicate vs Correlated alert khác nhau thế nào?**

Duplicate là cùng 1 alert fire đi fire lại nhiều lần - cùng service, cùng metric, cùng severity, chỉ khác timestamp. Correlated là các alert khác nhau nhưng cùng chung 1 nguyên nhân gốc. Ví dụ từ dataset: nếu `payment-svc latency_p99_ms crit` fire 3 lần liên tiếp thì đó là duplicate - fingerprint giống hệt nhau. Còn `payment-svc latency_p99_ms crit` + `checkout-svc 5xx_rate crit` + `edge-lb p99_latency_ms crit` là correlated - 3 fingerprint khác nhau nhưng cùng do payment-svc bị quá tải kéo theo.

**Câu 3: gap_sec = 30 vs gap_sec = 600**

`gap_sec = 30`: tách thành 5 session nhỏ, incident chính được cô lập rõ, recommender và search tách riêng đúng chỗ. `gap_sec = 600`: toàn bộ 20 alert rơi vào 1 session duy nhất vì span của dataset chỉ có 6.5 phút, kết quả là 1 cluster khổng lồ vô nghĩa.

**Câu 4: Correlator có gom recommender-svc vào cluster chính không?**

Không. Có 2 lý do độc lập cùng ngăn việc này. Thứ nhất về thời gian: `recommender-svc` alert lúc `09:45:10`, cách alert cuối của session 0 tới 40 giây, vượt `gap_sec = 30` nên bị tách session riêng ngay từ Layer 2. Thứ hai về topology: `recommender-svc` không nằm trong vòng 2 hop của `payment-svc` hay `checkout-svc` trên service graph, nên Layer 3 cũng không kéo nó vào. Kết quả đúng với thực tế vì data ghi rõ đây là "unrelated - concurrent batch retrain".

**Câu 5: Limitation lớn nhất của topology grouping?**

Topology grouping chỉ dựa vào cấu trúc graph tĩnh - nó không biết service nào thực sự đang gọi service nào vào lúc incident xảy ra. Ví dụ: `search-svc` và `payment-svc` có thể gần nhau trên graph nhưng trong incident này `search-svc` alert do nguyên nhân hoàn toàn khác, không liên quan gì đến payment. Topology vẫn có thể gom nhầm nếu 2 service tình cờ nằm gần nhau trên graph. Cách khắc phục là kết hợp thêm correlation từ distributed trace thực tế - nếu trace không có span nào từ `payment-svc` sang `search-svc` trong khoảng thời gian incident thì không gom, dù graph cho phép.