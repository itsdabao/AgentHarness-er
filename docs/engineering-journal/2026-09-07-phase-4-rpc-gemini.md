# Phase 4: RPC, Gemini và tiến trình thực thi

## Đã chốt và áp dụng

Theo trao đổi, ưu tiên cancellation/race, crash recovery, trace đáng tin, provider
adapter đúng format và demo tái lập. Scope là FastAPI JSON-RPC qua HTTP, Gemini
mặc định, event polling khoảng 500 ms. Terminal tương tác vẫn ở Phase 4.5.

- app.py mở HTTP client, SQLite và MCP subprocess, kiểm tra đủ tool trước khi
  phục vụ; đóng service trước dependencies.
- rpc.py cung cấp sáu operations, JSON envelopes, validation, pagination,
  redaction và mapping lỗi. rpc_client.py không tự gửi lại task khi mất response.
- providers/gemini.py sở hữu timeout/retry. Retry plan được ghi trước khi chờ.
  Không dùng lỗi trong lời văn model để quyết định retry.
- providers/gemini_codec.py chuyển wire format và giữ tool exchange hợp lệ.
  Giữ opaque signature/non-thought parts để resume; không lưu private thought text.
- ModelExecutionContext đưa deadline/cancel/event callback vào provider. ProviderError
  giữ lỗi đã phân loại; StorageError đi ra để runtime dừng khi không ghi được trace.
- Message có provider_data JSON nội bộ, codec đọc được record cũ thiếu field.
  Usage thêm input/output tokens quan sát được. Không cộng provider attempt vào model_steps.
- progress.py dùng câu hardcode từ event. Không thêm TUI hoặc stream từng token.
- Các example cung cấp real-provider demo, offline fake-model server rõ nhãn và
  context comparison opt-in tối đa sáu request.

## Khác với phương án sơ bộ và lý do

Dùng Gemini REST qua HTTPX thay cho SDK provider: phần tích hợp đủ nhỏ, kiểm soát
được retry thật và không có automatic tool execution. Vẫn dùng SDK MCP hiện có.
Gemini 429 không kèm Retry-After/RetryInfo sẽ fail rõ ràng; mã 429 một mình không
đủ để biết rate limit ngắn hạn hay quota đã cạn.

Input budget là conservative UTF-8 size estimate với output headroom, không phải
token count chính xác. Opaque provider continuation tăng kích thước context và
không được lộ ra RPC. Token counters chỉ phản ánh usage đã nhận, không phải hóa đơn.

## Kiểm chứng

- Regression core/context/persistence/lifecycle: 53 passed sau đổi provider port.
- Gemini/RPC tests ban đầu: 26 passed.
- Toàn suite cuối, gồm HTTP server thật và demo success/failure/cancel:
  128 passed trong 52.07 giây. Sau sửa import/type annotation trong test,
  chạy lại bốn edge/socket tests: 4 passed trong 8.19 giây.
- Ruff check: passed; formatter không còn thay đổi. Mypy: không có lỗi trong
  46 source files. Tất cả bảy file JSON phase parse thành công.
- Lệnh minder-rpc --help hoạt động; context evaluation mặc định trả not_evaluated.
- Kiểm tra môi trường: GEMINI_API_KEY chưa có, .env chưa có.
- Live Gemini smoke: chưa chạy. Không gọi provider thật bằng test key.
- Context comparison mặc định in not_evaluated; benchmark trả phí chưa chạy.

## Giới hạn và bước nghiệm thu còn lại

Cần đặt GEMINI_API_KEY ở môi trường chạy server rồi thực hiện demo thật. Không
đánh dấu toàn bộ Phase 4 đã nghiệm thu trước bước đó. Context comparison tùy chọn
không chứng minh cải thiện lost-in-the-middle khi chưa đo.

Các giới hạn: một process/DB owner, không auth/tenancy, không search run IDs,
không auto-resubmit, không remote rollback hay exactly-once. Khi thực hiện
Phase 4.5/5, tái sử dụng RPC client/progress và các fixture hiện có.
