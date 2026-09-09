# Session picker — thay việc dán ID

## Đã bàn và đồng ý

Người dùng muốn thanh chọn, bấm mở danh sách session cũ và cuộn khi dài.
Giữ ID backend để định danh; không bắt người dùng ghi nhớ hoặc dán UUID.

## Đã implement

- RPC → AgentService → ExecutionStore.list_sessions: trả summary JSON, không
  đưa SQL hay lịch sử hội thoại vào client.
- SQLite: lấy tên từ task đầu tiên, thời gian và run gần nhất. Phân trang keyset
  theo thứ tự tạo, không theo updated_at để activity không làm nhảy trang.
  Migration v3 thêm hai index; không thay ID, không xóa dữ liệu.
- sessions.js + HTML/CSS: dropdown cuộn, 20 dòng/trang và nút tải thêm.
  Dữ liệu render bằng textContent; lỗi tải giữ nguyên session hiện tại;
  response cũ sau đóng/mở bị bỏ qua.
- app.js: chọn phiên rồi mở run gần nhất bằng API sẵn có. Đổi phiên chỉ
  dừng quan sát phiên cũ, không cancel hoặc tự gửi lại task.

## Giới hạn có chủ đích

Chưa có search, rename/delete, danh sách tất cả run hoặc multi-user.
Không gọi model đặt tên. CLI vẫn dùng ID. Cursor áp dụng cho lịch sử hiện tại,
không phải ID lâu dài để dùng qua thao tác rebuild/VACUUM ngoài ứng dụng.
Không lưu danh sách session riêng trong browser.

## Kiểm chứng

Test offline bao phủ danh sách rỗng, phân trang khi có session/run mới, tên và
latest run, restart DB, migration từ v2, tham số RPC, static assets và hành vi
dropdown bằng DOM giả (chọn, lỗi, response muộn, Escape, cursor đứng yên).
Không gọi provider thật, không đọc/sửa .env và không restart server của người dùng.
Visual/keyboard trong trình duyệt thật vẫn cần kiểm tra thủ công.
