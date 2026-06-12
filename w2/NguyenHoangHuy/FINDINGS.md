# FINDINGS.md

## 1. Similarity function cho Layer 2

Em dùng **hybrid similarity** = `0.7 * cosine(log/trace dims) + 0.3 * jaccard(service one-hot)`.

Ban đầu em định dùng cosine cho cả 32 chiều luôn cho đơn giản, nhưng test thử thấy có vấn đề: 14/32 chiều là one-hot service (toàn 0/1), mà phần lớn incident chỉ "động" vào 1-2 service trong 14 service. Khi nhiều chiều = 0 ở cả 2 bên, cosine vẫn cho ra số khá cao dù 2 incident chả liên quan gì về service — vì cosine không phân biệt được "cả 2 đều bằng 0" với "cả 2 đều bằng 1". Ví dụ thử tay với E01: nếu dùng cosine 32 chiều, INC-2025-07-19 (root cause "eviction", chả liên quan gì pool exhaustion) vẫn ra similarity gần bằng INC-2025-11-08 (connection_pool_exhaustion, đúng pattern) — vì 2 incident này tình cờ overlap vài service.

Nên em tách riêng: phần log/trace (18 chiều, giá trị liên tục 0-1, đo "mức độ giống về triệu chứng") dùng cosine; phần service (14 chiều one-hot) dùng Jaccard — Jaccard chỉ tính trên các vị trí có ít nhất 1 bên = 1, nên không bị "loãng" bởi các service không liên quan. Trọng số 70/30 vì em nghĩ "incident giống nhau về log/trace là tín hiệu mạnh hơn việc đúng y service" — 2 incident cùng root cause có thể xảy ra ở service khác nhau (ví dụ connection_pool_exhaustion có thể ở payment-svc hoặc ở service khác), nhưng log signature (`ConnectionPool: timeout acquiring connection`) thì gần như không đổi.

Chạy E01 với best_sim=0.4967 (INC-2025-11-08), so với nếu chỉ dùng pure cosine 32 chiều em test ra ~0.55 (vì cosine bị "kéo" lên do nhiều chiều service trùng 0). Hybrid cho điểm thấp hơn nhưng em nghĩ đúng hơn về mặt ý nghĩa — dù điểm thấp hơn lại khiến confidence_raw tụt xuống dưới CONFIDENCE_FLOOR (nói ở câu 4).

## 2. Outcome-weighted voting thay đổi ranking thế nào

Em demo bằng **E01**.

Nếu chỉ xếp theo similarity thuần (pure top-1 neighbour), top-3 neighbours của E01 là:
- INC-2025-11-08 (sim=0.4967, outcome=success, actions=rollback_service + increase_pool_size)
- INC-2025-07-19 (sim=0.4851, outcome=success, actions=page_oncall)
- INC-2025-10-15 (sim=0.4653, outcome=success, actions=page_oncall)

Nếu chỉ lấy top-1 → action = `rollback_service`/`increase_pool_size` (đúng theo expected.json).

Nhưng khi voting có outcome weight (`vote = sim * outcome_weight`), nhìn `action_votes` trong audit:
rollback_service:    weighted_score = 1.132  (raw_count=3)
increase_pool_size:  weighted_score = 0.9505 (raw_count=2)
page_oncall:         weighted_score = 0.9504 (raw_count=2)

`rollback_service` vẫn thắng (1.132 > 0.9505), vì cả 3 neighbour support nó đều outcome=success → outcome_weight=1.0 không "trừ điểm" ai. Trong case này outcome-weighting không đổi kết quả vì cả 29 history toàn outcome=success/partial cho các action liên quan connection_pool_exhaustion — chưa có failed case nào trong top-3 để thấy rõ tác động "trừ điểm action từng fail".

Em nghĩ tác động của outcome-weighting sẽ rõ hơn nếu test với incident mà top-1 neighbour có outcome="failed" — lúc đó `vote = sim * 0 = 0`, action đó bị loại hoàn toàn dù similarity cao nhất. Trong 8 eval incident hiện tại em chưa thấy case nào top-1 neighbour outcome=failed, nên hiệu ứng này chưa được "test" thật bởi eval set — đây cũng là 1 gap em nhận ra khi viết phần này.

## 3. EU calculation đầy đủ — case E01

Candidate set (từ `action_votes`): `rollback_service`, `increase_pool_size`, `page_oncall`.

| Action | p_success | benefit | cost_penalty | blast_penalty | EU |
|---|---|---|---|---|---|
| rollback_service | 0.6667 | 1.0 | 10/15=0.6667 | (1/4)*0.3=0.075 | 0.6667*1.0 - 0.6667 - 0.075 = **-0.075** |
| increase_pool_size | 0.4478 | 1.0 | 1/15=0.0667 | (1/4)*0.3=0.075 | 0.4478*1.0 - 0.0667 - 0.075 = **0.3062** |
| page_oncall | 0.4478 | 0.35 | 0 | 0 | 0.4478*0.35 - 0 - 0 = **0.1567** |

Theo EU, `increase_pool_size` (0.3062) thắng `page_oncall` (0.1567) thắng `rollback_service` (-0.075). Ranked: `increase_pool_size` > `page_oncall` > `rollback_service`.

Điều thú vị: `rollback_service` có `weighted_score` cao nhất ở Layer 2 (1.132, là `top_action`) nhưng EU lại **âm** — vì `cost_penalty` của nó (10 phút / 15 = 0.667) quá nặng so với `p_success` (0.6667). Trong khi `increase_pool_size` rẻ hơn (1 phút) nên EU dương dù `p_success` thấp hơn (0.4478).

Tuy nhiên kết quả cuối cùng KHÔNG phải `increase_pool_size` — vì cả `increase_pool_size` và `page_oncall` đều bị chặn ở **confidence gate** (em nói rõ ở câu 4), nên engine fallback về `page_oncall` theo nhánh "nothing passes gate".

`confidence_raw = best_sim * consensus = 0.4967 * 0.3732 ≈ 0.1854`. Với `increase_pool_size`: `confidence_adjusted = 0.1854 * 0.4478 ≈ 0.083 < CONFIDENCE_FLOOR(0.20)` → bị global gate chặn → loại. Tương tự `page_oncall` không được xét trong loop chính (code bỏ qua page_oncall trong vòng for) → cuối cùng `selected_action = page_oncall`.

## 4. Khi nào engine escalate — và đúng/sai so với ground truth

Engine của em hiện tại **escalate (page_oncall) ở cả 8/8 incident**. Đối chiếu `expected.json`:

- **E02, E04, E05, E06, E07, E08**: page_oncall nằm trong `accepted_actions` (hoặc không có `must_not_action`) → **đúng** (6/8).
- **E01**: `must_not_action = page_oncall`, expected = rollback_service/increase_pool_size → **SAI**, vi phạm.
- **E03**: `must_not_action = page_oncall`, expected = restart_pod/rollback_service → **SAI**, vi phạm.

Em check lại nguyên nhân: với cả 8 incident, `best_sim` dao động 0.38-0.84, nhưng `consensus_score` thường thấp (0.28-0.66) vì action_votes chia khá đều giữa nhiều action (do nhiều history cùng pattern nhưng action khác nhau, ví dụ connection_pool_exhaustion có cả `rollback_service`, `increase_pool_size`, và cả `page_oncall` từ history khác). `confidence_raw = best_sim * consensus` → ra số rất nhỏ (0.07-0.66), sau khi nhân thêm `p_success` (0.15-1.0) thì hầu hết rơi dưới `CONFIDENCE_FLOOR = 0.20`.

Với E01 — đây là case mà em thấy rõ nhất là **false escalation**: `is_ood=False`, `best_sim=0.4967` (không hề thấp), top-3 neighbours toàn outcome=success, và root_cause_class của neighbour gần nhất (INC-2025-11-08) khớp y hệt connection_pool_exhaustion — một case mà theo brief, "engine phải auto-recommend, không escalate". Nhưng vì `CONFIDENCE_FLOOR=0.20` được áp dụng *sau khi* đã nhân `best_sim * consensus * p_success` (3 lần nhân làm số tụt rất nhanh), nên action hợp lý bị loại oan.

→ Kết luận: cơ chế confidence gate của em hiện tại **quá conservative**, escalate cả những case có evidence rõ ràng. Đây là bug/limitation thật mà em phát hiện qua audit.jsonl, không phải giả định.

## 5. Loại incident dễ làm engine sai nhất + đề xuất cải tiến

Theo phân tích ở câu 4, loại incident dễ sai nhất với engine hiện tại là **incident có nhiều historical precedent với actions KHÁC NHAU** (kể cả khi tất cả đều outcome=success) — vì `consensus_score` bị chia loãng giữa các action, kéo `confidence_raw` xuống thấp, rồi bị global confidence gate chặn dù `best_sim` không thấp (E01, E03 là ví dụ cụ thể: best_sim 0.49-0.54 nhưng vẫn bị escalate oan).

Theo em, có thể cải tiến bằng cách tách riêng hai khái niệm: **"hệ thống có nhận diện được incident này hay không"** và **"hệ thống có đủ tự tin để chọn action phù hợp hay không"**.

Hiện tại, việc đánh giá confidence đang kết hợp cả chỉ số `best_sim` với `consensus`/`p_success` trong `CONFIDENCE_FLOOR`. Điều này dẫn đến một số trường hợp chưa hợp lý. Ví dụ, một incident có `best_sim` cao nghĩa là hệ thống nhận diện khá rõ đây là loại sự cố nào, nhưng lại có nhiều phương án xử lý khả thi nên `consensus` thấp. Khi đó incident vẫn bị xem như OOD và bị escalate, mặc dù bản chất hệ thống không hề "không biết" incident này là gì.

Theo em, nên sử dụng `best_sim` để đánh giá mức độ nhận diện incident (đã có `OOD_THRESHOLD` ở Layer 2), còn `consensus` hoặc `p_success` để đánh giá độ chắc chắn khi lựa chọn action. Nhờ vậy có thể phân biệt rõ hai tình huống:

* **Không nhận diện được incident** → xem là OOD và escalate.
* **Nhận diện được incident nhưng có nhiều action phù hợp** → vẫn có thể chọn một action ít tốn kém hoặc ít rủi ro hơn thay vì ngay lập tức page/escalate.

Cách tách biệt này sẽ giúp logic ra quyết định rõ ràng hơn và giảm các trường hợp escalate không cần thiết.


