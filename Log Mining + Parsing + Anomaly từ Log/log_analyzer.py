#!/usr/bin/env python3
import sys
import os
import re
from datetime import datetime
from collections import Counter
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

def parse_timestamp(log_line):
    """
    Hàm trích xuất timestamp chính xác dựa trên cấu trúc ảnh thực tế của HDFS và BGL.
    """
    tokens = log_line.split()
    if len(tokens) < 3:
        return None

    # 1. Trường hợp HDFS: Token 0 có 6 chữ số (YYMMDD), Token 1 có 6 chữ số (HHMMSS)
    if re.match(r'^\d{6}$', tokens[0]) and re.match(r'^\d{6}$', tokens[1]):
        try:
            return datetime.strptime(f"20{tokens[0]} {tokens[1]}", "%Y%m%d %H%M%S")
        except ValueError:
            pass

    # 2. Trường hợp BGL: Token 0 là nhãn/dấu (-), Token 1 là chuỗi 10 chữ số (Epoch timestamp)
    if re.match(r'^\d{10}$', tokens[1]):
        try:
            return datetime.fromtimestamp(int(tokens[1]))
        except (ValueError, OverflowError):
            pass
            
    # Dự phòng: Kiểm tra nếu Token 0 chính là Epoch timestamp (BGL dạng lược bỏ dấu gạch)
    if re.match(r'^\d{10}$', tokens[0]):
        try:
            return datetime.fromtimestamp(int(tokens[0]))
        except (ValueError, OverflowError):
            pass

    return None

def clean_log_message(log_line):
    """
    Loại bỏ phần Header (Timestamp, Node ID, Component) để Drain3 chỉ xử lý 
    phần nội dung text thuần túy, tránh bị bùng nổ (explode) số lượng template ảo.
    """
    tokens = log_line.split()
    if len(tokens) < 3:
        return log_line

    # Nếu là HDFS: Bỏ Ngày (col 0), Giờ (col 1), PID (col 2), Log Level (col 3) -> Lấy từ col 4 trở đi
    if re.match(r'^\d{6}$', tokens[0]) and re.match(r'^\d{6}$', tokens[1]):
        # Giữ lại tên component (ví dụ: dfs.DataNode) + nội dung phía sau
        return " ".join(tokens[4:])

    # Nếu là BGL: Bỏ Dấu (col 0), Epoch (col 1), Date (col 2), NodeID (col 3), Time_ISO (col 4), Node_Vật_lý (col 5), Log_Level (col 6)
    if re.match(r'^\d{10}$', tokens[1]):
        # Lấy từ nội dung thông điệp thực sự (thường bắt đầu từ cột thứ 7 hoặc 8 trở đi)
        return " ".join(tokens[7:])

    return log_line

def main():
    if len(sys.argv) < 2:
        print("Sử dụng: python log_analyzer.py <path_to_logfile>")
        sys.exit(1)

    log_file_path = sys.argv[1]
    if not os.path.exists(log_file_path):
        print(f"Lỗi: Không tìm thấy file {log_file_path}")
        sys.exit(1)

    # Khởi tạo Drain3 Miner
    config = TemplateMinerConfig()
    if os.path.exists("drain3.ini"):
        config.load("drain3.ini")
    else:
        # Cấu hình nóng một số tham số tối ưu nếu không có file ini
        config.masking_instructions = []
    
    miner = TemplateMiner(config=config)

    total_lines = 0
    valid_time_lines = 0
    log_records = []  # Lưu tuple: (timestamp, template_id)

    # Đọc luồng dữ liệu của log file
    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            total_lines += 1
            
            ts = parse_timestamp(line_str)
            cleaned = clean_log_message(line_str)
            
            # Đưa nội dung đã làm sạch qua Drain3 để lấy ID template
            result = miner.add_log_message(cleaned)
            template_id = result["cluster_id"]
            
            if ts:
                valid_time_lines += 1
                log_records.append((ts, template_id))

    if not log_records:
        print("Lỗi: Không trích xuất được timestamp hợp lệ nào từ định dạng file này.")
        sys.exit(1)

    # Xác định mốc thời gian lớn nhất làm mốc "Hiện tại" của log
    max_time = max(rec[0] for rec in log_records)
    min_time = min(rec[0] for rec in log_records)
    
    # Tính mốc 1 giờ trước đó bằng timedelta
    from datetime import timedelta
    one_hour_ago = max_time - timedelta(hours=1)
    
    # Tính tổng thời gian hệ thống ghi log (quy ra giờ) để tính toán trung bình lịch sử
    total_duration_hours = (max_time - min_time).total_seconds() / 3600.0
    if total_duration_hours < 0.01:
        total_duration_hours = 1.0

    # Phân nhóm đếm tần suất
    history_counts = Counter([rec[1] for rec in log_records])
    recent_counts = Counter([rec[1] for rec in log_records if rec[0] >= one_hour_ago])
    baseline_counts = Counter([rec[1] for rec in log_records if rec[0] < one_hour_ago])

    # Lấy thông tin text template từ cấu trúc cây Drain3
    template_texts = {cluster.cluster_id: cluster.get_template() for cluster in miner.drain.clusters}
    unique_templates_count = len(template_texts)

    # --- IN KẾT QUẢ RA STDOUT ---
    print("=" * 70)
    print(f" LOG ANALYZER REPORT FOR: {os.path.basename(log_file_path)}")
    print("=" * 70)
    print(f"Tổng số dòng log đọc được      : {total_lines}")
    print(f"Số dòng log parse được thời gian: {valid_time_lines}")
    print(f"Số lượng Log Template Unique  : {unique_templates_count}")
    print(f"Khoảng thời gian của tập dữ liệu: Từ {min_time} đến {max_time}")
    print(f"Mốc cửa sổ 1 giờ gần nhất      : Từ {one_hour_ago}")
    print("-" * 70)

    # 1. Top-5 template (count + % tổng)
    print(" TOP-5 MOST FREQUENT TEMPLATES:")
    top_5 = history_counts.most_common(5)
    for rank, (tid, count) in enumerate(top_5, 1):
        pct = (count / valid_time_lines) * 100
        text = template_texts.get(tid, "N/A")
        print(f"  {rank}. [ID {tid:02d}][Freq: {count:5d}] [{pct:5.2f}%] -> {text}")
    print("-" * 70)

    # 2. Template tăng đột biến trong 1 giờ gần nhất
    print(" TEMPLATES SPIKING IN THE LAST HOUR (So với trung bình lịch sử):")
    spikes_found = False
    historical_hours = (one_hour_ago - min_time).total_seconds() / 3600.0

    for tid, rec_count in recent_counts.items():
        base_count = baseline_counts.get(tid, 0)
        avg_historical_per_hour = base_count / historical_hours if historical_hours > 0 else base_count

        # Tiêu chí đột biến: Xuất hiện > 5 lần trong giờ cuối VÀ gấp 3 lần trung bình lịch sử
        if rec_count > 5 and rec_count > (3 * avg_historical_per_hour):
            spikes_found = True
            text = template_texts.get(tid, "N/A")
            print(f"  > [ID {tid:02d}] Giờ cuối: {rec_count} dòng | Trung bình quá khứ: {avg_historical_per_hour:.2f} dòng/giờ")
            print(f"    Nội dung: {text}")
            
    if not spikes_found:
        print("  (Không phát hiện dấu hiệu tăng đột biến bất thường)")
    print("-" * 70)

    # 3. New templates (chưa xuất hiện trước giờ gần nhất)
    print(" NEW TEMPLATES DETECTED IN THE LAST HOUR:")
    new_templates = [tid for tid in recent_counts if tid not in baseline_counts]
    
    if new_templates:
        for tid in new_templates:
            text = template_texts.get(tid, "N/A")
            print(f"  [NEW] [ID {tid:02d}] Xuất hiện {recent_counts[tid]} lần ở giờ cuối -> {text}")
    else:
        print("  (Không có cấu trúc log mới nào xuất hiện ở giờ cuối)")
    print("=" * 70)

if __name__ == "__main__":
    main()