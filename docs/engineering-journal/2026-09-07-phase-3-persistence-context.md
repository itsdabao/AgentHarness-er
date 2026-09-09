# Phase 3 — Persistence, lifecycle và context có giới hạn

- Ngày: 2026-09-07.
- Trạng thái: đã implement và kiểm chứng.
- Nguồn: phase-3-persistence-lifecycle.json và yêu cầu “thực hiện phase 3 đi”.
- Không triển khai RPC/provider thật hoặc benchmark chất lượng model của Phase 4.

## Từ phương án đã bàn đến implementation

- Chọn SQLite + aiosqlite, một ExecutionStore. Không ORM, Redis, queue ghi nền
  hay một repository cho mỗi bảng. Hai migration nhỏ có user_version.
- EventEmitter chuyển thành awaitable; giữ AgentHarness/AgentLoop duy nhất.
  Core không import SQLite hay MCP SDK. Event sink lưu messages trong lúc chạy,
  thay vì chờ kết thúc rồi dump toàn bộ session.
- AgentService quản lý submit/status/cancel/events và task/token đang chạy.
  RPC sau này gọi các operation này, không viết lại lifecycle.
- Transactions ngắn được serialize; state transition và event cùng commit.
  Không giữ DB transaction trong lúc gọi provider hoặc MCP.
- ToolCall có execution_id nội bộ; mỗi attempt lưu intent trước dispatch và result
  sau đó. ID do provider trả về chỉ để correlation, không phải khóa toàn hệ thống.
- Guard durable dùng server_scope + tool name + canonical arguments. Cần cấu hình
  scope ổn định theo server/quyền truy cập, không ghi credential vào scope.
- Store lỗi/commit chưa xác định thì bị quarantine; không gửi thêm tool call,
  không đưa StorageError vào retry nghiệp vụ, không báo đã lưu thành công.
- OS-held owner lock ngăn runtime thứ hai mở DB. Không xóa lock file khi crash;
  OS tự nhả khóa. Cùng một store chỉ dùng với một supervisor.
- Recovery: queued/running thành interrupted; cancel_requested thành cancelled;
  terminal giữ nguyên. Không tự khởi động lại retry timer hoặc replay tool.
- Reconciliation là operation operator-only có ghi chú, chỉ cho run terminal.
  Không đưa thành agent tool, không khẳng định exactly-once.

## JSON state và context

Lịch sử đầy đủ nằm trong messages/events/attempts. TaskState là JSON nhỏ có version,
revision, objective, constraints, observations và open_questions; nguồn user input/
message được giữ lại. Observations có thời gian và mang nghĩa lịch sử, không cache
tình trạng máy hiện tại. Chỉ giữ tối đa 8 observation gần nhất đủ vừa state budget.

Context builder là hàm thuần trên core values, không query DB hay dùng provider SDK.
Giữ system instructions, user request hiện tại, constraints và nhóm tool exchange
đầy đủ. Bỏ bớt phần lịch sử cũ/observation tùy chọn; required context quá lớn trả
CONTEXT_BUDGET_EXCEEDED trước khi gọi provider.

Budget hiện tại: state 6000 JSON characters, context 24000 JSON characters gồm
tool definitions. Đây không phải tokenizer. Model-step event lưu state snapshot,
revision, message references và policy đã dùng. Không gọi LLM để summarize mỗi bước,
không lưu private chain-of-thought, không tuyên bố đã chữa lost in the middle.

## Những điểm phải xử lý thêm khi implement/test

1. Đổi callback sync sang async làm thay đổi tất cả callers trong loop, harness,
   executor và tests. Dùng lại cơ chế emit hiện có, không tạo event hierarchy mới.
2. Await ghi model response cũng tiêu thời gian: cần kiểm tra lại deadline/cancel
   sau ghi trước khi chấp nhận final output.
3. Tool có thể đã chạy dù chưa commit kết quả. Test subprocess os._exit ở bốn mốc:
   trước intent, sau intent, sau tác dụng bên ngoài, sau result commit.
4. Khi commit đã xong nhưng acknowledgement lỗi, không đoán transaction thất bại.
   Quarantine store, báo lỗi caller; reopen đọc lại state/event đã commit thực tế.
5. Cancel có thể thắng trước terminal completion dù final answer đã vào audit.
   Continuation không lấy final answer của run chưa completed; audit body vẫn giữ.
6. Resume sửa một view của transcript, không rewrite lịch sử: missing tool results
   có marker not_executed/cancelled/outcome_unknown hoặc kết quả đã ghi nhưng chưa
   được chấp nhận. Không bịa successful result hoặc gọi lại tool còn thiếu.
7. Intent/result dùng các event tool_execution_started/completed/failed hiện có,
   thêm record_type=attempt_intent/attempt_result. Phân biệt với logical-call events
   để không đếm một tool call thành nhiều logical Usage.tool_calls.
8. Full-suite ban đầu lộ budget fixture reconnect quá sát chi phí cold-start Python.
   Log cho thấy process thứ hai đã mở nhưng chưa tools/list/dispatch trước deadline.
   Tách startup khỏi phép đo request; reconnect fixture dùng 5s, short timeout cases
   vẫn 2s. Giữ exact call/discovery/process counts và watchdog; không đổi runtime
   retry budget để làm test pass.

## Kiểm chứng

- 29 tests mới về persistence/context đã pass trong lượt targeted gần nhất.
- Demo offline thực tế: completed, resume completed cùng session nhưng run_id mới,
  và cancelled. Chạy ba process riêng trên một database tạm, không đụng DB người dùng.
- Lượt cuối: uv run --locked --offline pytest -q — **98 passed in 65.29s**.
- uv run --locked --offline ruff check . và ruff format --check . đều pass
  (44 files formatted); mypy pass (35 source files).
- Nhóm MCP stdio chạy riêng: 23 passed. Demo complete/resume/cancel đều pass.
- GitNexus phiên này chưa index Celestiny; đã rà caller trực tiếp và chạy regressions.
- Không commit/push; giữ nguyên các thay đổi có sẵn trong worktree.

## Giới hạn còn lại và bài học

Một process, một event loop, một supervisor/store. OS file lock không phải distributed
lock; SQLite demo không nhắm tới shared network filesystem. Session history vẫn load
vào RAM trước khi chọn context, nên workload lớn cần tối ưu truy vấn/retention.

Async adapters phải nhường event loop. Local cancellation không rollback tác dụng
remote, shutdown cleanup có budget riêng. DB transaction không thể gộp nguyên tử
với remote tool execution; unresolved intent phải được xử lý bảo thủ.

Không thêm layer chỉ vì tên kiến trúc. Những phần tách riêng ở đây đều có nhiệm vụ
cụ thể: core contracts/context, SQLite transaction/ownership, application task lifecycle.
Phase 4 còn RPC, provider thật, token accounting và đánh giá context tùy chọn.
