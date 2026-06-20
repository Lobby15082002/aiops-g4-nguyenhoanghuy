# W3-D3 Submission — Hoang Huy

---

## Outage chosen

- ID: 1
- Name: AWS S3 us-east-1 2017-02-28
- Why this one: Pattern "operator action without guardrail" là failure mode dễ reproduce nhất (Easy) và có giá trị học cao — nó demonstrate rằng AIOps pipeline hoàn toàn blind với infrastructure-layer events, không chỉ application metrics. Muốn quan sát gap đó trực tiếp thay vì chỉ đọc lý thuyết.
- Failure mode: operator-action-without-guardrail

---

## 3 thứ tôi học từ outage này

1. **`--remove-orphans` không có scope restriction** — flag này stop tất cả containers trong compose project, không chỉ service được chỉ định. Trước khi reproduce mình không biết điều này; nghĩ `stop` chỉ ảnh hưởng service mình gọi. Thực tế là bất kỳ unscoped stop nào trong compose project đều có blast radius = toàn bộ project.

2. **AIOps pipeline chỉ "thấy" những gì được wired vào metrics source** — Reproduction chạy xong, inject xong, 3 containers Exited — pipeline không fire alert nào. `rca_observed.json` trả về `root_service: null`. Pipeline không thiếu thuật toán; nó thiếu data source. Đây là gap kiến trúc, không phải gap model.

3. **Blameless wording thay đổi cách nhìn vào fix** — Nếu viết "operator quên nhập service name" thì action item là "nhắc operator cẩn thận hơn" (vô dụng). Khi viết "maintenance script pipeline cho phép unscoped destructive command đến production mà không có scope validation" thì action item tự nhiên trở thành "thêm validation vào script" — đây là fix thực sự có thể implement.

---

## 1 thứ pipeline của tôi sẽ vẫn miss nếu outage này xảy ra real

- Pattern: **operator-action-without-guardrail** (cùng failure mode được reproduce)
- Why miss: Pipeline hiện tại không có docker/container event stream collector. Failure mode này không generate application-layer metrics — không có HTTP 5xx, không có latency spike, không có CPU/memory anomaly. Chỉ có container lifecycle events (container stop). Vì pipeline chỉ query Prometheus metrics và application alerts, nó hoàn toàn blind với infrastructure-layer state changes.
- Mitigation idea: Thêm docker event stream collector vào pipeline; fire `multi-service-stop` alert khi ≥2 services trong cùng compose project stop trong vòng 5 giây. Đây cũng là quyết định được ghi trong ADR-001.

---

## 1 quyết định trong ADR mà tôi không hoàn toàn chắc

ADR-001 chọn **two-layer guardrail** (script prevention + pipeline detection). Phần tôi không chắc là trade-off về **signal noise của docker event collector**.

Container stop events xảy ra thường xuyên trong môi trường development (rolling deploy, health check restart, manual stop khi test). Nếu threshold `≥2 services stop trong 5s` được áp dụng đồng đều cho cả dev và production, sẽ có rất nhiều false positives trong dev environment — làm giảm trust vào alerts.

Giải pháp khả năng: chỉ enable docker event alert trên production compose projects (tag-based filtering), hoặc raise threshold lên 3 services. Nhưng mình chưa có data về tần suất rolling deploy trong production stack để biết threshold nào phù hợp.

---

## Cost model verdict cho stack của tôi

- Scenario: Vietnam e-commerce, 50 services, 4 incident/month × 1.5h, $8k/hour downtime cost, $15k/month AIOps
- ROI: 1.28
- Payback: 0.78 tháng
- Verdict: **marginal**

Lý do verdict marginal (không phải worth_it): stack hiện tại còn nhỏ (~50 services) và downtime cost ở mức mid-range e-commerce Vietnam ($8k/hour). Để đạt worth_it cần tăng lên ~5 incidents/month hoặc downtime cost > $10k/hour. Tuy nhiên payback period chỉ 0.78 tháng — nếu stack grow lên 100+ services hoặc có thêm 1-2 high-severity incidents, ROI sẽ cross 1.5 nhanh chóng.
