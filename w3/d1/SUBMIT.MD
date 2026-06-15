# W3-D1 Submission — Lobby

## 3 điều em học được

1. **SLI nên phản ánh trải nghiệm người dùng.** Trước đây em thường nhìn CPU hay memory để đánh giá hệ thống, nhưng bài này giúp em hiểu rằng điều quan trọng là những gì người dùng thực sự cảm nhận được như tỷ lệ lỗi hay độ trễ.

2. **Burn rate có ý nghĩa hơn error rate đơn thuần.** Cùng một mức lỗi nhưng với các SLO khác nhau thì mức độ ảnh hưởng sẽ khác nhau. Burn rate giúp chuẩn hóa theo SLO nên dễ đánh giá mức độ nghiêm trọng hơn.

3. **MWMBR cần cả cửa sổ ngắn và dài.** Window ngắn giúp phát hiện nhanh nhưng dễ nhiễu, còn window dài ổn định hơn nhưng phản ứng chậm. Kết hợp cả hai giúp cân bằng giữa tốc độ và độ chính xác.

## 1 điều em vẫn chưa rõ

Em vẫn chưa hiểu cách xác định SLO target trong thực tế. Trong bài tập có thể thử nhiều giá trị rồi đánh giá kết quả, nhưng ở môi trường production thì việc chọn một mức SLO phù hợp ngay từ đầu có vẻ khó hơn nhiều.

## 1 trade-off em chưa chắc chắn

Em giảm threshold Tier 1 của API từ 14.4 xuống 7 để đạt mục tiêu phát hiện sự cố nhanh hơn. Tuy nhiên em chưa chắc điều này có làm tăng false positive trong môi trường thực tế hay không, vì dữ liệu thử nghiệm chưa phản ánh hết các tình huống traffic thực.

## Validation Report

* noise_reduction_pct: 86.4%
* mttd_delta_s: 60s
* false_negative: 0
* verdict: pass
