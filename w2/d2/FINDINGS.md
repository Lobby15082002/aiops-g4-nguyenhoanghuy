# FINDINGS — W2-D2 RCA

## Cluster chính: c-000-000

Cluster lớn nhất có 15 alert từ 6 service:
`cart-svc, checkout-svc, edge-lb, notification-svc, payment-svc, recommender-svc`.

**Root cause:** `payment-svc`  
**Class:** `connection_pool_exhaustion`  
**Confidence:** `0.9296`

### Lý do

payment-svc ranked #1 by Graph RCA (PageRank+timestamp score=0.9296). Most similar past incident: INC-2025-11-08 (similarity=1.0) which had root_cause_class='connection_pool_exhaustion'. Incident summary: Payment-svc v3.2 deploy at 09:42 leak DB pool. Pool 50/50 used trong 5 phút. Downstream checkout cascade. Notification queue backed up.

Graph RCA dùng PageRank trên **reverse graph** — service được nhiều caller phụ thuộc vào thì score cao. 
Kết hợp với timestamp score (service alert sớm nhất = culprit), `payment-svc` 
liên tục đứng đầu cả 2 metric, confidence = 0.9296.

## Confidence — có dám auto-remediate không?

Với confidence = **0.9296**, câu trả lời là **chưa dám auto-rollback**. 
Threshold an toàn cho auto-remediation thường là ≥ 0.90 với ít nhất 2 corroborating signals 
(graph + causal). Hiện pipeline chỉ có graph + retrieval, chưa có causal inference từ time-series. 
Recommendation: dùng output này để **ưu tiên SRE investigate** (human-in-the-loop), 
chưa trigger auto-rollback.

## Tại sao retrieval-only đã đủ (không chọn bonus)?

Retrieval-only (kNN từ incident history) đủ vì:
1. Dataset chỉ có 29 incident lịch sử — quá nhỏ để train decision tree hay TF-IDF có ý nghĩa.
2. Keyword similarity đã capture được signal chính: service overlap + severity match.
3. Kết hợp với Graph RCA làm primary signal, retrieval chỉ đóng vai trò enrichment 
   (lấy class + action) — không cần embedding phức tạp hơn ở scale này.

Nếu dataset lịch sử tăng lên > 500 incidents, TF-IDF hoặc sentence-transformer sẽ cho 
precision cao hơn đáng kể.
