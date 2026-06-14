# SUBMIT.md — W2-D3: Model Serving

## Latency thực tế của endpoint

Em thực hiện test bằng cách gửi liên tiếp 20 request với bộ dữ liệu gồm 20 alert và đo thời gian phản hồi thông qua header `X-Response-Time-Ms`.

Kết quả thu được:

* p50 ≈ 10ms
* p99 ≈ 1870ms

Trong quá trình test, request đầu tiên mất khoảng 1.8 giây do phải gọi LLM để sinh phần tóm tắt sự cố. Các request tiếp theo có cùng nội dung nên sử dụng lại kết quả đã lưu trong cache, thời gian phản hồi chỉ dao động khoảng 5–16ms.

Điều này cho thấy cơ chế cache hoạt động khá hiệu quả đối với các incident có mẫu tương tự nhau. Khi cache hit, phần lớn thời gian xử lý chỉ còn lại ở các bước correlation, RCA và serialize response.

Qua quan sát, bước gọi LLM là thành phần tốn nhiều thời gian nhất khi cache miss. Trong khi đó các bước xử lý dữ liệu còn lại chỉ chiếm một phần rất nhỏ tổng thời gian phản hồi.

Nếu số lượng alert tăng lên đáng kể, thời gian của bước correlation và RCA cũng sẽ tăng theo do phải xử lý nhiều dữ liệu hơn. Ngược lại, thời gian gọi LLM gần như không thay đổi nhiều vì chỉ phụ thuộc vào nội dung prompt được gửi đi.

---

## Khi LLM gặp sự cố hoặc có nhiều request đồng thời

Em thực hiện test bằng cách gửi đồng thời 4 request sử dụng `ThreadPoolExecutor`.

Với cấu hình mặc định chạy một worker, các request được xử lý lần lượt. Request đầu tiên thường có độ trễ cao nhất do phải gọi LLM, trong khi các request sau có thể tận dụng cache nên phản hồi nhanh hơn nhiều.

Để tránh việc toàn bộ hệ thống bị ảnh hưởng khi LLM không khả dụng, endpoint có cơ chế timeout:

* Nếu LLM phản hồi bình thường, kết quả sẽ bao gồm phần tóm tắt do LLM sinh ra.
* Nếu LLM timeout hoặc gặp lỗi, endpoint vẫn trả về kết quả từ các bước correlation và RCA.
* Trường `llm_summary` sẽ được gán giá trị `null`.

Ngoài ra, hệ thống hỗ trợ biến môi trường `AIOPS_USE_LLM=false` để tắt hoàn toàn bước enrichment bằng LLM khi cần thiết. Khi đó endpoint vẫn hoạt động bình thường nhưng chỉ sử dụng kết quả từ graph analysis và retrieval.

---

## `/healthz` và `/readyz` kiểm tra điều gì?

Em tách thành hai endpoint riêng vì chúng phục vụ hai mục đích khác nhau.

### `/healthz`

Endpoint này chỉ kiểm tra xem tiến trình ứng dụng còn hoạt động hay không.

Nếu service vẫn đang chạy thì trả về:

```json
{
  "status": "ok"
}
```

Mục đích là để hệ thống orchestration (ví dụ Kubernetes) biết khi nào cần khởi động lại pod nếu ứng dụng bị treo hoặc bị crash.

### `/readyz`

Endpoint này kiểm tra xem ứng dụng đã sẵn sàng nhận request hay chưa.

Cụ thể, em kiểm tra:

* Service graph đã được load thành công.
* Incident history đã được load vào bộ nhớ.

Chỉ khi các dữ liệu cần thiết đã sẵn sàng thì endpoint mới trả về trạng thái thành công.

### Vì sao cần tách riêng?

Theo em, "còn sống" và "sẵn sàng phục vụ" là hai khái niệm khác nhau.

Một service có thể vẫn đang chạy nhưng chưa load xong dữ liệu. Trong trường hợp đó, `/healthz` nên pass nhưng `/readyz` nên fail để tránh nhận traffic quá sớm.

Ngược lại, nếu LLM tạm thời không khả dụng thì service vẫn có thể xử lý correlation và RCA. Vì vậy em không đưa trạng thái của LLM vào `/readyz`, giúp hệ thống tiếp tục phục vụ request thay vì bị đánh dấu là không sẵn sàng chỉ vì một thành phần bổ sung gặp sự cố.
