# Phase 5 bonus: test support dùng chung

## Đã thống nhất trong cuộc trao đổi

Sau Phase 4, gom phần hỗ trợ test còn cần cải thiện thành một bonus phase:
runtime setup dùng chung, database seed/isolation helpers, fault injection và
hành vi fake provider tái sử dụng. RPC, provider thật, retry, token accounting và
demo end-to-end đã thuộc Phase 4 nên không đưa lại vào bonus.

## Điều chỉnh khi viết kế hoạch

- Các test crash, rollback, mất acknowledgment khi commit và MCP process chết đã
  có. Bonus chủ yếu gom phần lặp và lập bản đồ coverage.
- Giữ tmp_path với SQLite file riêng cho mỗi test. Không bắt buộc thêm reset():
  test crash/restart cần commit thật và mở lại cùng file; outer transaction rollback
  sẽ làm sai điều kiện cần kiểm thử. Cleanup đóng tài nguyên, giữ lịch sử runtime.
- FakeLLMProvider đã có response sequence. Tái sử dụng nó; chỉ thêm helper lỗi
  hoặc chờ có điều khiển ở nơi có nhu cầu dùng chung. Không bắt buộc tạo bốn class mới.
- Fault injection dùng monkeypatch có scope và fixture subprocess hiện có. Test
  ownership lock không được coi là bằng chứng đã test mọi loại SQLite contention.
- Runtime helper dùng async context manager/fixture để dọn tài nguyên cả khi setup
  thất bại; mỗi test vẫn thể hiện rõ input, fault và assertion của mình.

## Đã cập nhật

- Tạo docs/phases/phase-5-bonus-test-support.json, phụ thuộc Phase 4, đánh dấu
  optional và planned, có work items, acceptance criteria và validation plan.
- Liên kết bonus phase trong project brief, README và mục lục nhật ký.

## Trạng thái triển khai và kiểm chứng

Phần trên ghi lại kế hoạch ban đầu. Kết quả triển khai sau đó nằm ở mục bên dưới.

## Triển khai ngày 2026-09-08

Người dùng duyệt mở rộng thành 10 ca database, 5 happy case nhiều tool và 5 ca khó,
sau đó yêu cầu apply Phase 5.

### Đã làm

- tests/support.py: async lifecycle bằng AsyncExitStack, seed/rows, scoped storage
  faults, mất commit acknowledgment và ProviderGate chờ bằng tín hiệu.
- tests/fakes.py: giữ response sequence/call inspection, thêm async callback và
  respond theo message thật mà loop truyền vào. Không có provider framework mới.
- Hai test cancel/shutdown trong test_persistence.py dùng runtime/gate mới, không
  chờ sleep để canh thời điểm; giữ assertion và thêm kiểm tra cancellation tới fake.
- service_for trong test_rpc.py tái sử dụng setup. Seed và ba test storage fault
  dùng helper; crash subprocess và replay guard hiện có giữ nguyên.
- test_database_lifecycle.py: 10 ca, bao gồm SQLITE_BUSY thật từ connection khác,
  migration bị lỗi giữa transaction, cleanup và cô lập state/fake history.
- fixtures/factory_data.py và multistep_mcp.py: alpha/beta dataset trong process
  thử nghiệm riêng; dùng nguyên ba tool và build_server hiện có.
- test_multistep_scenarios.py: chuỗi dùng area/procedure từ kết quả trước, chọn
  theo priority/status, so sánh, hai chuỗi, snapshot thay đổi và năm edge cases.
- tests/README.md và test_coverage trong JSON: map đủ 20 ID tới node test thực.

### Khác biệt so với con số ban đầu

20 tình huống nghiệp vụ cho 25 lượt test vì mỗi happy case chạy alpha và beta.
Thêm 1 ca H03 batch (5 tool calls nhưng 4 model steps) và 2 kiểm tra helper:
release/failure callback, teardown error phải được báo. Tổng 28 test mới.
Không viết lại các crash/replay/commit tests chỉ để tăng số lượng.

### Phát hiện trong lúc implement

- Timeout từ adapter mang mã MCP_TIMEOUT, không phải TOOL_TIMEOUT của lỗi ở
  ranh giới loop. Assertion được sửa theo contract đang chạy; không sửa runtime.
- Hai snapshot khi recheck đều còn trong trace; kết quả mới không được ghi đè
  dữ liệu audit cũ.
- Lỗi domain conflict vẫn cho store tiếp tục dùng; SQLITE_BUSY ngoài nhóm domain
  khiến store quarantine theo policy hiện tại. Không tự thêm retry database.
- E05 cố tình cho fake model làm theo injection để thử allowlist. Pass chỉ chứng
  minh unapproved tool không dispatch, không chứng minh Gemini chống injection.

### Kiểm chứng

- Nhóm mới: 28 passed trong 45.82 giây.
- Full regression: 172 passed trong 113.51 giây, baseline trước Phase 5 là 144.
- Ruff check sạch, 81 file đúng format; Mypy không có lỗi trong 58 source files.
- SQLite và local MCP thật, model scripted. Không gọi Gemini, không đọc/sửa .env,
  không mở database của server người dùng, không restart server.
- Code production không đổi. Live-model evaluation vẫn optional, chưa chạy.
