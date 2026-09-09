# Sửa lệch interface của AgentLoop

## Phát hiện

Smoke test qua RPC dừng trước Gemini: AgentHarness truyền run_id nhưng AgentLoop.run
không nhận tham số này. Source tại thời điểm kiểm tra là loop đồng bộ, trong khi
harness, provider, executor và event sink đều async. Không có bằng chứng xác định
vì sao file đổi; không suy đoán ai hoặc thao tác nào gây ra.

Test core sẵn có tái hiện cùng TypeError trước khi vá. Đây là lỗi nội bộ khác với
HTTP 404 của Gemini trong smoke test trước đó.

## Đã sửa

- Chỉ sửa runtime ở core/loop.py: dùng async/await và EventEmitter từ ports.
- Nhận context/recorder/run ID đúng interface; tái sử dụng build_context và cancellable.
- Giữ một deadline chung, truyền cancellation vào provider và executor.
- Gán execution_id, lưu message/continuation qua event và cộng usage được trả về.
- Giữ lỗi provider đã phân loại, không nuốt StorageError hoặc thêm retry ở loop.
- Thêm regression test kiểm tra context, recorder, deadline, IDs, usage và event payload.

Không sửa .env, provider, service hoặc cấu hình model. Không gọi API Gemini trong
lượt sửa này. Lỗi interface đã sửa không đồng nghĩa lỗi HTTP 404 đã được giải quyết.

## Kiểm chứng

- Trước vá: test multi-step core fail với unexpected keyword argument run_id.
- Sau vá: nhóm regression core/context/persistence/MCP lifecycle/provider: 76 passed.
- Toàn suite: 129 passed trong 54.00 giây, gồm RPC/socket/MCP và regression test mới.
- Sau bổ sung type guard cho JSON trong test, nhóm core chạy lại: 14 passed.
- Ruff format --check và Ruff check: passed; Mypy không có lỗi trong 46 source files.
- Chưa chạy lại smoke test Gemini thật; không thay đổi trạng thái nghiệm thu live.

## Rút kinh nghiệm

Không chỉ thêm tham số để hết TypeError: contract còn bao gồm async I/O, event
projection và cancellation. Chạy mypy và test multi-step trước khi tốn request
provider thật; đối chiếu kết quả offline với live riêng biệt.
