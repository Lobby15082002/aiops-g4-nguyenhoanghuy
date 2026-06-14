# DESIGN.md — W2-D3: AIOps Pipeline Serving

## 1. Kiến trúc Pipeline

Trong bài này, em triển khai một API bằng `serve.py` để gom toàn bộ pipeline xử lý sự cố thành một endpoint duy nhất.

```
POST /incident
    │
    ▼
Layer 1: Correlate
    → Gom nhóm alert theo thời gian
    → Kết hợp thông tin topology giữa các service
    → Tạo ra các cluster liên quan
    │
    ▼
Layer 2: RCA (Root Cause Analysis)
    → Chọn cluster chính
    → Phân tích mức độ ảnh hưởng trên service graph
    → So sánh với dữ liệu incident lịch sử
    → Dự đoán loại nguyên nhân gốc
    │
    ▼
Layer 3: LLM Enrichment (tùy chọn)
    → Sinh phần tóm tắt ngắn cho người trực hệ thống
    → Sử dụng cache để giảm số lần gọi model
    → Có cơ chế timeout và fallback nếu model không phản hồi
    │
    ▼
IncidentResponse JSON
```

Pipeline được thiết kế theo từng lớp độc lập để dễ kiểm thử và dễ mở rộng trong tương lai.

---

## 2. Lý do chọn FastAPI

Em chọn **FastAPI** để xây dựng service vì một số lý do sau:

* Hỗ trợ lập trình bất đồng bộ (async) khá tốt, phù hợp với việc gọi API của LLM.
* Tích hợp sẵn Pydantic giúp kiểm tra dữ liệu đầu vào và giảm lỗi do request không hợp lệ.
* Tự động sinh tài liệu API tại `/docs`, thuận tiện cho việc kiểm thử và demo.

So với Flask, FastAPI hỗ trợ validation và async tốt hơn. BentoML cũng được cân nhắc nhưng bài toán hiện tại chủ yếu là xử lý pipeline thay vì phục vụ mô hình machine learning chuyên biệt nên FastAPI đơn giản và phù hợp hơn.

---

## 3. Ước lượng hiệu năng

Mục tiêu của endpoint là phản hồi trong thời gian hợp lý ngay cả khi có thêm bước gọi LLM.

| Thành phần       | Thời gian ước lượng |
| ---------------- | ------------------- |
| Validate request | Rất nhỏ             |
| Correlation      | Vài mili giây       |
| RCA              | Vài mili giây       |
| Gọi LLM          | Lớn nhất (vài giây) |
| Trả response     | Rất nhỏ             |

Qua phân tích, bước gọi LLM là phần tốn thời gian nhất của pipeline. Vì vậy em áp dụng một số cách tối ưu:

* Sử dụng cache cho các prompt đã xuất hiện trước đó.
* Cho phép tắt hoàn toàn LLM bằng biến môi trường khi cần.
* Dùng model nhỏ để giảm thời gian phản hồi.
* Nếu kết quả RCA có độ tin cậy cao thì có thể bỏ qua bước sinh tóm tắt bằng LLM.

---

## 4. Khả năng chịu tải và xử lý lỗi

### Concurrency

Service chạy bằng Uvicorn và hỗ trợ xử lý bất đồng bộ.

Trong trường hợp cần phục vụ nhiều request hơn, có thể tăng số lượng worker của Uvicorn. Mỗi worker sẽ tải riêng dữ liệu graph và history vào bộ nhớ, đổi lại khả năng xử lý đồng thời sẽ tốt hơn.

Các dữ liệu như service graph và incident history chỉ được đọc sau khi khởi tạo nên không phát sinh vấn đề tranh chấp dữ liệu giữa các request.

### Fault Tolerance

Để tránh việc toàn bộ hệ thống bị ảnh hưởng khi LLM gặp sự cố, em bổ sung các cơ chế sau:

* Giới hạn thời gian chờ phản hồi từ LLM.
* Nếu LLM timeout hoặc không khả dụng, hệ thống vẫn trả kết quả RCA.
* Kiểm tra dữ liệu đầu vào bằng Pydantic trước khi chạy pipeline.
* Ghi log lỗi phía server để hỗ trợ debug nhưng không trả chi tiết stack trace cho người dùng.

Nhờ đó pipeline vẫn có thể hoạt động ngay cả khi thành phần LLM gặp vấn đề.

---

## 5. Service Graph

Hiện tại service graph được lưu trong file `services.json` và được cập nhật thủ công.

Cách làm này phù hợp với quy mô nhỏ của bài lab vì dễ triển khai và dễ kiểm soát. Tuy nhiên khi số lượng service tăng lên, việc cập nhật thủ công có thể dẫn đến sai lệch so với hệ thống thực tế.

Trong tương lai có thể tích hợp các công cụ Distributed Tracing như OpenTelemetry để tự động thu thập quan hệ giữa các service và cập nhật topology chính xác hơn.
