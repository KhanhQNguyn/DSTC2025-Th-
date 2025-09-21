# 📂 Danh sách file & folder cần thiết để chạy 

**Quy trình chạy lần lượt từng cell, code gồm 2 phần chính là cell chạy dữ 
liệu realtime và cell test trên stress test**

**LƯU Ý CÁC FILE CẦN CÓ PHẢI ĐỂ TRONG CÙNG 1 THƯ MỤC THÌ CODE MỚI TẢI VÀ 
CHẠY ĐƯỢC**

*Cell chạy dữ liệu realtime* nhóm chỉ để chạy vòng lặp 1 lần duy nhất sau 
này có thể đổi vì nhóm sử dụng dữ liệu EOD nên không cần vòng lặp nhiều lần

*Cell test trên stress test* chạy trong khoảng thời gian từ tháng `25/03/
2025 đến 16/04/2025` nên *không tải dữ liệu* mà làm trên dữ liệu lịch sử

## 1. File cấu hình
- config.json  
  Chứa username, password, tham số rule (RSI_buy_threshold, vol_spike_mult, 
macd_diff_thresh, rule_min_score, alert_min_alloc, cooldown_hours …).  
 Nếu chưa có, Block 1 sẽ tự động tạo file mặc định. Tuy nhiên nhóm chỉ để cho 
nó không bị lỗi khi chạy thôi nên ưu tiên vẫn có sẵn file

- service_account.json (tuỳ chọn)  
  File key của Google Service Account (dùng nếu bật Google Sheets).

**Link gg sheet để theo dõi : https://docs.google.com/spreadsheets/d/17FRrF63TFE3bmAseoV4vQK5EA9nYaTaRRnLyd1F8MGU/edit?gid=414442089#gid=414442089**
---

## 2. File trạng thái runtime
- runtime_state.json  
  Lưu trạng thái chạy trước (api_used, last_universe, last_alerts …). (Vì tài 
khoản có giới hạn số lần gọi api mỗi tháng nên lưu để kiểm soát )  
   Nếu chưa có, Block 1 sẽ tự động tạo file mặc định.

---

## 3. Folder universe (Block 2 – Universe Selection)
- universe/universe_list.csv – danh sách 200 tickers lọc được.  
- universe/universe_metadata.json – metadata (ngày chạy, số tickers).  

---

## 4. Folder alerts (Block 3–4 – Alerts Engine)
- alerts/alerts_today.csv – alerts mới nhất trong ngày.  
- alerts/alerts_log.csv – log toàn bộ alerts từ trước đến nay.  
- alerts/alerts_summary.json – tóm tắt alerts (dùng gửi Telegram/Google 
Sheets).  

---

## 5. Folder signals (Block 3 – Signal Engine)
- signals/signal_list.csv – danh sách toàn bộ tín hiệu (RSI, MACD, Volume 
Spike …).  
- signals/signal_metadata.json – metadata signals.  


---

## 6. Các folder dữ liệu bổ sung (nằm trong folder dữ liệu chạy cảnh báo trên github )
 
 Cần *giải nén file zip* trước khi chạy, để có thêm các thư mục sau:  
- universe/ (có sẵn data universe mẫu).  
- signals/ (có sẵn data tín hiệu).  
- backtestddpg/ (data dùng cho backtest mô hình).  
- data/ (dữ liệu raw cần cho một số block).  
- monitor/ (chứa file theo dõi giám sát hệ thống).  

---
