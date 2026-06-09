# SUBMIT — W2-D2 EOD Checkpoint

## Q1: Confidence của top-1 trong cluster lớn nhất là bao nhiêu?

Cluster lớn nhất (`c-000-000`, 15 alerts) cho 
confidence = **0.9296** với root cause = `payment-svc`.

Nếu phải set threshold cho auto-rollback, mình chọn **0.90**. Lý do: confidence hiện tại 
(0.9296) chỉ dựa trên graph topology + timestamp — chưa có causal inference 
từ metric time-series để corroborate. Với critical service như payment-svc, 
rollback sai còn tệ hơn chờ SRE confirm 2-3 phút.

## Q2: Variant cho classifier — A rule-based / B free LLM / C paid LLM

Chọn: **A — rule-based (kNN top-1 similarity)**

Thực tế: lấy `class` và `actions` từ top-1 similar incident trong history (keyword similarity). 
Chạy ra: class = `connection_pool_exhaustion`, actions = `['Rollback to v3.1. Scale pool 50 → 100 cushion. Add pool monitor alert > 80%.']`.

**Trade-off so với B (free LLM / Groq):**
- Rule-based: deterministic, 0 latency, không phụ thuộc network. Nhược điểm: chỉ tốt khi 
  history đủ đại diện — miss hoàn toàn với incident pattern chưa từng gặp.
- Free LLM: có thể generalize tốt hơn, nhưng thêm dependency ngoài, latency ~1-2s, 
  và cần hallucination guard chặt chẽ.

## Q3: Pipeline gần product nào nhất trong Industry landscape?

Pipeline mình xây (graph + temporal + classifier) gần **Dynatrace Davis** nhất — 
cả 2 đều assume service map đáng tin, dùng topology để rank candidate, 
kết hợp temporal signal.

Trong domain GeekShop (e-commerce, alert volume cao, service map tương đối ổn định), 
lựa chọn này **hợp lý nhưng nên bổ sung**:
1. Khi service map thiếu edge (vd: async Kafka flow checkout→notification), 
   graph bị lệch — cần causal inference làm fallback (gần Causely hơn).
2. Với alert volume cao, nên thêm dedup layer như BigPanda trước khi vào RCA 
   để giảm noise cluster.

Kết luận: giữ graph-based approach, nhưng long-term nên hybrid với causal inference 
khi có đủ metric time-series history.
