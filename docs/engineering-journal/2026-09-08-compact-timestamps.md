# Web: thời gian gọn hai dòng

## Yêu cầu và triển khai

Thay chuỗi ISO dài ở Bắt đầu/Kết thúc bằng một ô nhỏ: giờ AM/PM phía trên,
ngày/tháng/năm phía dưới. Giữ màu sắc/font của giao diện hiện tại.

- client.js: formatter dùng Intl, mặc định theo múi giờ trình duyệt.
- app.js: render thẻ time; tooltip giữ UTC offset và timestamp gốc.
- index.html/style.css: ghi rõ quy ước ngày, hai dòng không ngắt giữa chuỗi.
- Null hiện Chưa có; dữ liệu không hợp lệ không bị biến thành ngày giả.
- Chỉ đổi presentation; timestamp trong snapshot/JSON/database không thay đổi.

## Kiểm chứng

25 test JavaScript và 8 test terminal/Web pass. Ca mới kiểm tra timezone,
đổi ngày qua nửa đêm, AM/PM và input thiếu/không hợp lệ.
Chưa kiểm tra visual bằng browser thật; không gọi model hay sửa .env.
