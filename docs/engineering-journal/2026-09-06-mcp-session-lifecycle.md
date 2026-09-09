# Hướng dài hạn — MCP session lâu dài và async I/O

- Ngày: 2026-09-06.
- Trạng thái: đã đồng ý và implement; xem phần kiểm chứng bên dưới.
- Nguồn: người dùng muốn tính hướng lâu dài để tránh sửa kiến trúc nhiều lần.
- Liên quan: MCP client/executor, core ports/loop/harness và planning Phase 3–4.

## Hiện trạng khi bàn phương án

Executor cache catalog nhưng mỗi operation mở SDK Client mới. SDK có thể gọi
tools/list để lấy output schema trên session mới. Core ports đang synchronous;
client chạy anyio.run mỗi operation. Cache hiện tại chưa loại bỏ overhead kết nối.

## Hướng đề xuất

Ứng dụng sở hữu một MCP session mỗi server/configuration trong một process và
một ngữ cảnh quyền truy cập. Không chia sẻ xuyên credentials nếu sau này có auth.

Startup: connect → discovery → lọc allowlist → ready, với startup timeout riêng.
Run: dùng connection/catalog đã có; model, tool, retry và mọi thời gian chờ trong
run dùng cùng deadline. Kết quả tool vẫn lấy mới.
Shutdown: ngừng nhận run mới, xử lý run đang chạy rồi đóng SDK client/process.
SDK context được mở và đóng trong cùng task quản lý lifecycle.

Chuyển core I/O ports, loop và harness sang async cùng một đợt. Models giữ format
trung lập; SDK ở adapter. Tránh duy trì hai loop sync/async hoặc thêm thread riêng
chỉ để giữ lớp bọc synchronous hiện tại.

## Discovery, reconnect và cancellation

- Discovery một lần khi tạo connection, tái sử dụng schema cache của SDK.
- Sau reconnect, discovery lại và áp dụng lại allowlist. Explicit refresh hoặc
  thông báo catalog thay đổi có thể làm mới cache khi phù hợp.
- Một khóa nhỏ ngăn nhiều run cùng reconnect. Thời gian chờ khóa/reconnect vẫn
  tiêu deadline và phải phản hồi cancellation.
- Reconnect không tự replay tool. Executor vẫn là chủ retry tool trong budget
  đã có; không reset deadline hoặc thêm retry loop lồng nhau.
- Startup có giới hạn; chưa sẵn sàng thì báo lỗi/trạng thái rõ ràng.
- Cancel một run chỉ hủy request của run đó, không mặc định đóng shared session.
  Late result không được đưa vào model hoặc đổi trạng thái terminal.
- Connection thật sự hỏng thì đánh dấu unavailable và kết thúc các request bị
  ảnh hưởng với lỗi phù hợp; reconnect không chứng minh thao tác cũ chưa chạy.
- Bound concurrency trong process. Giữ policy tối đa một active run/agent session;
  không giả định server luôn chạy nhiều tool đồng thời hay cancel được mọi tool.

Agent session (conversation, lưu DB) khác MCP session (SDK connection, RAM).
Restart ứng dụng tạo MCP connection mới. Phase 3 persist outcome guard và history;
không phục hồi socket từ DB hoặc tự phát lại thao tác mơ hồ.

## Phạm vi đề xuất

1. Async core I/O, long-lived MCP client, startup/readiness/shutdown.
2. Reconnect/refresh có giới hạn và cancel từng request trên shared session.
3. Chỉnh tests hiện có; kiểm chứng nhiều calls dùng cùng connection, không discovery
   lại khi catalog ổn định, reconnect refresh, cancel A không tự đóng connection
   của B, shutdown không để process/task mồ côi.
4. Phase 3 thêm ExecutionStore/recovery; Phase 4 gắn async RPC/provider.

Không cần connection pool, distributed workers hoặc framework lifecycle riêng.
Session reuse giảm overhead; run timeout quá ngắn vẫn có thể hết giờ.

## Đánh đổi và trạng thái quyết định

Chi phí là chỉnh async signatures và tests Phase 1–2 một lần. Lợi ích là SDK
connection reuse, cancellation và RPC dùng cùng mô hình async rõ ràng.
Ở thời điểm đề xuất chưa sửa source code hoặc phase JSON. Sau đó người dùng
đồng ý bằng “apply đi” và yêu cầu “continue”; phần dưới ghi lại triển khai thực tế.

## Đã implement

- Core provider/executor ports, AgentLoop và AgentHarness chuyển sang async. Dùng
  chung helper cancellation/deadline; không duy trì loop sync thứ hai.
- SDKMCPClient dùng async context manager ở cấp ứng dụng. Một task sở hữu việc
  mở/đóng SDK session; startup timeout riêng mặc định 5 giây, capacity mặc định 8.
- Discovery một lượt phân trang mỗi connection; SDK giữ output-schema cache.
  Executor lấy catalog đã cache, vẫn lọc allowlist; không cache kết quả tool.
- Reconnect dùng một lock, tạo lại session và catalog. Catalog version gắn với
  từng lần validate; tránh dùng schema cũ khi request khác vừa refresh. Chỉ executor
  retry tool (tối đa 2 attempts mặc định); reconnect không replay hay reset deadline.
- Cancel từng request không đóng session chung. Pending requests nhận lỗi khi
  connection đóng. aclose idempotent, chờ cleanup của SDK trong đúng owner task.
- Reuse một hàm validate tool/schema cho lần đầu và sau reconnect; không thêm
  framework retry, connection pool, event hierarchy hay worker infrastructure.
- Harness RAM chặn hai run cùng session trên cùng instance, kiểm tra lại cancel/
  deadline trước khi nhận output. RPC admission/draining và durable state vẫn chưa có.

## Khác biệt và bài học từ lúc test

1. Không chỉ cache list tool ở executor: phải giữ chính SDK session để tránh SDK
   discovery lại khi validate output. Tests stdio đếm server_started và tools_list,
   không suy đoán từ số lần gọi wrapper.
2. Run A hết thời gian chờ reconnect không được cancel startup mà run B đang dùng.
   Owner task giữ budget startup riêng; các waiter giữ deadline của chính mình.
3. Test shutdown phát hiện SDK có thể để request chờ tới timeout sau khi session
   đã đóng. Đã bổ sung tín hiệu interruption vào helper chờ hiện có để trả lỗi sớm.
4. Chính sách “shutdown ngừng nhận run, drain/cancel rồi đóng client” cần application
   service ở Phase 4. Hiện client close chỉ abort I/O còn lại và cleanup; không tự
   nhận trách nhiệm quản lý tất cả run của ứng dụng.
5. Automatic catalog-change subscription chưa làm: explicit refresh và reconnect
   là hai điểm refresh hiện tại. Không chia sẻ client giữa event loops/credentials.
6. Schema/retry safety sau reconnect được kiểm lại trước dispatch; có tool mới
   không đồng nghĩa có quyền chạy. Outcome mơ hồ vẫn không được replay.
7. GitNexus MCP của phiên làm việc chưa index Celestiny (chỉ có repo khác), nên
   rà callers bằng source search và regression tests; không dùng graph repo khác.

## Kiểm chứng

- 57 regression tests cũ đã pass sau khi chuyển async.
- Thêm test stdio gọi 3 lần nhưng chỉ 1 process/1 discovery; lỗi rồi reconnect
  tạo đúng 2 process/2 discovery, không nhân đôi retry.
- 11 tests lifecycle kiểm tra cancel isolation, capacity timeout trước dispatch,
  stale catalog, thay đổi schema/tool/safety sau reconnect, provider cancel/deadline,
  concurrent run exclusion, reconnect single-flight và shutdown (tool/discovery).
- Lượt kiểm tra cuối: `uv run --locked --offline pytest -q` — **69 passed in 44.94s**.
- `uv run --locked --offline ruff check .`, `ruff format --check .` và `mypy`
  đều pass; mypy kiểm 24 source files. Tất cả phase JSON parse thành công.
- Không commit/push. Các thay đổi có sẵn trong worktree được giữ nguyên.

## Giới hạn còn lại

Phase 3 persist session/events/attempts/outcome guard và xử lý restart; Phase 4 gắn
RPC/provider thật, readiness/admission/draining. Chưa kiểm thử HTTP hay production
load. Local cancellation không rollback tác dụng phía server; code async vẫn phải
nhường event loop. SDK cleanup có thể dài hơn request timeout trong giới hạn riêng.
