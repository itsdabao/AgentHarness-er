# Phase 2 — Retry, deadline, cancellation và trace

- Ngày ghi: 2026-09-06.
- Trạng thái hiện tại: P2-R1 đến P2-R6 đã được triển khai theo quyết định bổ sung bên dưới.
- Nguồn: tái dựng từ cuộc trao đổi, kết quả kiểm tra của lượt review trước và
  đọc lại code hiện tại. Không gán ngày/commit triển khai khi chưa có bằng chứng.
- Yêu cầu ban đầu: giải thích, đề xuất và lưu nhật ký; không sửa source code.
  Sau đó người dùng đồng ý xử lý các phần còn lại và hỏi thêm về discovery một lần.
- File liên quan: `src/minder_harness/mcp/{client,contracts,executor}.py`,
  `src/minder_harness/core/{loop,ports}.py`, `tests/test_mcp.py`,
  `docs/phases/phase-2-mcp-integration.json`.

## Lúc bàn

Agent Loop giữ vai trò model → tool → result. MCP adapter sở hữu retry.
Chỉ retry lỗi tạm thời đã nhận diện khi tool an toàn để lặp lại, chưa cancel
và còn budget. Mặc định hai attempts, gồm lần đầu và một lần retry; delay 200ms.
Lỗi nghiệp vụ, quyền truy cập, arguments sai và lỗi chưa rõ không tự retry.
Tool có side effect và kết quả chưa rõ phải được bảo vệ khỏi replay.
Discovery, các attempts và thời gian chờ đều phải tôn trọng deadline của run.
Tái sử dụng core contracts/events và SDK; giữ code trực quan, tránh framework retry riêng.

## Lúc implement

- Có allowlist, discovery cache, JSON Schema validation và retry loop nhỏ trong executor.
- `ToolExecutionContext` truyền cancellation, thời gian còn lại và callback event.
- MCP SDK được bọc trong client; mock factory server có ba tools đọc dữ liệu.
- File MCP ban đầu dài được tách thành contracts, client, executor và exports.
- `_uncertain_calls` lưu tên tool + arguments để chặn lần gọi giống hệt khi outcome mơ hồ.
- 25 test pass và smoke test discovery qua stdio đã được báo cáo ở lượt triển khai.
  Đây là kết quả lịch sử, không phải bộ kiểm tra mới chạy trong lượt ghi nhật ký.

## Review: khác biệt giữa ý định và hành vi

### P2-R1 — Deadline bị tính lại sau discovery

Đã tái hiện bằng fake client: còn 10ms, discovery mất khoảng 50ms, executor vẫn
dispatch tool và trả thành công. Deadline được tạo trong `_execute_discovered()`
sau discovery, sử dụng lại toàn bộ `context.timeout_seconds` ban đầu.

Đề xuất:

- Tạo mốc deadline một lần lúc vào `execute()`; truyền mốc đó xuyên suốt.
- Kiểm tra cancel/deadline trước discovery và trước mỗi dispatch.
- Discovery trong run dùng timeout nhỏ hơn giữa timeout cấu hình và thời gian còn lại;
  truyền cancellation tới client discovery.
- Sau discovery/validation và sau chờ retry, kiểm tra lại budget.
- Discovery chủ động trước khi run bắt đầu vẫn dùng timeout setup riêng.

Lợi ích: hết thời gian thì không phát sinh thêm tool call; số đo latency có ý nghĩa.
Đây là kiểm soát budget và scheduling, không phải cam kết dừng mọi cleanup đúng từng ms.
Nên sửa ngay; chưa cần một deadline framework hoặc thay toàn bộ core sang async.

### P2-R2 — Phân loại lỗi retry quá rộng

Đã tái hiện: `PermissionError('denied')` trở thành `MCP_CONNECTION_LOST`,
`retryable=True`, do classifier coi mọi `OSError` là lỗi kết nối.

Đề xuất:

- Nhận diện lỗi permission/auth và lỗi cấu hình như executable không tồn tại;
  trả mã lỗi cụ thể, không retry.
- Bỏ quy tắc mọi `OSError` đều retryable. Dùng tập lỗi kết nối tạm thời đã biết
  và errno phù hợp với transport đang hỗ trợ.
- Lỗi timeout/kết nối được phân loại transient vẫn phải đi qua kiểm tra tool safety,
  attempt budget, deadline và cancellation của executor.
- Lỗi nghiệp vụ, malformed/protocol và lỗi chưa nhận diện mặc định không retry.
- Với nhóm exception, một lỗi transient không được tự động che lấp lỗi vĩnh viễn
  hoặc chưa rõ trong cùng nhóm; giữ thông tin nguyên nhân để debug.
- Chỉ map các exception SDK thực tế có bằng chứng; không đoán từ chuỗi message.
- Test permission, executable thiếu, connection reset, timeout, lỗi chưa rõ và nhóm lỗi.

Lợi ích: tránh gọi lại vô ích, chẩn đoán lỗi đúng; giữ một classifier nhỏ.
Nên sửa ngay; không cần provider fallback hoặc thư viện retry mới.

### P2-R3 — Cancel sau dispatch chưa được coi là outcome mơ hồ

Đã tái hiện ở executor bằng fake client: lần đầu ném `TOOL_CANCELLED`, lần sau
gọi cùng side-effect tool/arguments vẫn được dispatch. Đây là bằng chứng lỗ hổng
guard, không phải bằng chứng một máy thật đã bị thay đổi hai lần.

Ví dụ: gửi lệnh tạo work order → server có thể đã tạo → user bấm cancel khi
client đang chờ response. Cancel việc chờ không hoàn tác work order đã tạo.
Nếu gửi lại, có thể tạo hai work orders. Mock server hiện chỉ có read-only tools;
tình huống side effect dùng fixture để kiểm chứng policy.

Đề xuất:

- Tách eligibility retry khỏi sự không chắc chắn về outcome.
- Dùng metadata nhỏ như `outcome_unknown` trong error details hiện có; không cần
  tạo một hierarchy exception mới. Client ghi nhận theo giai đoạn request.
- Nếu biết chắc lỗi xảy ra trước dispatch, không đánh dấu outcome mơ hồ.
- Nếu request có thể đã gửi nhưng không nhận kết quả đáng tin cậy, đánh dấu outcome
  chưa rõ; khi không chứng minh được thì xử lý thận trọng.
- Với tool không an toàn để lặp lại, outcome mơ hồ kích hoạt guard kể cả cancel
  hoặc lỗi đọc/giải mã response, không phụ thuộc riêng `retryable`.
- Giữ guard nhỏ trong bộ nhớ và test model yêu cầu lại bằng call ID mới.
  Guard tên+arguments chỉ bảo vệ lời gọi trùng khớp; không bảo đảm chống mọi thao tác
  tương đương về nghiệp vụ, chẳng hạn arguments khác nhưng cùng tạo một work order.
- Ghi rõ phạm vi guard hiện là executor instance; không tự xóa guard khi run cancel.
  Durable storage và cơ chế xác minh/giải quyết outcome thuộc Phase 3 trở đi.

Lợi ích: không coi cancel là bằng chứng remote chưa chạy; không phát lại lệnh ghi
khi chưa rõ kết quả. Đánh đổi là có thể chặn một lệnh thực tế chưa hoàn tất cho đến
khi outcome được xác minh. Nên sửa phần guard hiện tại ngay.

### P2-R4 — Attempt trace thành công khi tool thực tế báo lỗi

Đã tái hiện: final `ToolResult.status == 'failed'` nhưng attempt metadata ghi
`status == 'succeeded'` khi response có `is_error=True`.

Đề xuất:

- Xét `response.is_error` trước khi ghi trạng thái attempt.
- Tool báo lỗi: attempt `failed`, `error_code=MCP_TOOL_ERROR`; giữ nội dung lỗi
  cho model và không retry tự động.
- Tool thành công: attempt `succeeded`.
- Chỉ phát event attempt tiếp theo bắt đầu sau khi kiểm tra lại cancel/deadline,
  sát lúc dispatch. Hiện event được phát ngay sau retry wait, trước guard đầu vòng.
- Giữ một final ToolResult và hệ event hiện có; dùng metadata cùng correlation ID.

Lợi ích: trace phản ánh kết quả tool thay vì chỉ việc nhận được response.
Nên sửa ngay; không cần tách thêm event transport thành công.

### P2-R5 — Bằng chứng timeout/cancel thực tế còn thiếu

Các test lỗi hiện chủ yếu ném exception từ fake client. Chúng xác minh policy,
nhưng chưa chứng minh timeout/cancel hoạt động với server thực sự chậm hoặc ngắt kết nối.
Client hủy task rồi await cleanup không có bound riêng; chưa có bằng chứng tái hiện
hang ở lượt review trước, nên đây là rủi ro cần kiểm chứng, không phải lỗi hang đã xác nhận.

Đề xuất:

- Bổ sung integration tests với SDK và fixture server chậm, ngắt kết nối, cancel
  trong lúc chờ; kiểm tra thời gian trả về, số lần dispatch và cleanup.
- Test có watchdog để suite không treo khi implementation lỗi; phân biệt timeout
  thao tác với khoảng grace nhỏ dành cho cleanup.
- Dựa trên behavior quan sát được, giới hạn cleanup qua cơ chế SDK/transport phù hợp.
  Chỉ thêm `wait_for` không đủ chứng minh bounded shutdown nếu task bỏ qua cancellation
  hoặc `asyncio.run()` vẫn chờ task khi đóng event loop.
- Kiểm tra SDK có retry ẩn làm nhân số dispatch không; không khẳng định khi chưa kiểm chứng.

Nên kiểm chứng trước khi đóng Phase 2; chỉ thay lifecycle client sâu hơn nếu test chứng minh cần.

### P2-R6 — Planning nhắc Retry-After nhưng code chỉ dùng delay cố định

Đề xuất ưu tiên: giữ delay 200ms cho demo stdio; khi được đồng ý, cập nhật planning
để ghi Retry-After là phần mở rộng cho transport có tín hiệu tương ứng.
Nếu giữ yêu cầu này trong scope, phải đọc giá trị từ adapter, validate giá trị,
kiểm tra wait còn nằm trong deadline và có test; không tự retry business error.
Ở lượt đề xuất chưa thay planning. Không cần thêm backoff/jitter để giải quyết các lỗi trên.

## Quyết định và kết quả sau sửa

### Quyết định bổ sung — 2026-09-06

Người dùng hỏi cách discovery/lưu tool một lần và đồng ý: “mấy cái còn lại ok á. xử đi”.
Giữ catalog cache theo executor và explicit refresh. Sửa budget discovery nếu nó
xảy ra trong run. Kết quả tool vẫn đọc mới; không dùng cache kết quả cho trạng thái máy.
Triển khai các sửa đổi retry, outcome guard, trace, kiểm chứng SDK và cập nhật scope Retry-After.

### Implementation thực tế

| Entry | Đã thay đổi |
| --- | --- |
| P2-R1 | Tạo deadline ngay khi vào execute; discovery nhận remaining timeout và cancellation; kiểm tra lại sau discovery; các attempts dùng cùng deadline. |
| P2-R2 | Tách permission/configuration; nhận diện lỗi transport và errno cụ thể; map MCP REQUEST_TIMEOUT/CONNECTION_CLOSED; nhóm exception ưu tiên lỗi không retryable, giữ causes. |
| P2-R3 | Client đánh dấu outcome_unknown theo giai đoạn dispatch; executor guard dựa vào outcome kể cả cancel/malformed; lỗi chắc chắn trước dispatch không làm khóa guard. |
| P2-R4 | Attempt business error ghi failed và MCP_TOOL_ERROR; event bắt đầu retry phát sau khi kiểm tra budget, sát dispatch. |
| P2-R5 | Dùng AnyIO CancelScope cùng task group thay native task.cancel; tái sử dụng bounded cleanup của SDK. Fixture stdio có watchdog, đếm invocation và kiểm tra server PID đã thoát. |
| P2-R6 | Planning và README ghi HTTP Retry-After/HTTP retry là phần để sau; demo stdio giữ delay cố định. |

Các file chính: MCP contracts/client/executor, tests/test_mcp.py,
tests/test_mcp_client.py, tests/fixtures/mcp_failure_server.py và mcp_probe.py.
README, architecture và phase-2 JSON đã đồng bộ. AnyIO được khai báo dependency trực tiếp;
không thêm thư viện retry hoặc agent framework.

### Khác biệt/phát hiện trong khi implement

- Cache catalog ở executor đã có từ implementation đầu. Test mới xác nhận chỉ
  backend list một lần cho đến explicit refresh, còn kết quả tool được lấy mới.
- SDK `ClientSession.validate_tool_result()` có thể gọi `tools/list` trên connection
  mới để lấy output schema. Vì client mở connection mỗi operation, không được nói
  “chỉ một discovery request trên wire”. Giữ mô hình connection hiện tại;
  session reuse là cải tiến lifecycle riêng.
- SDK có vòng gọi lại khi nhận InputRequiredResult. Đặt input_required_max_rounds=0
  để không nhân số attempts ngoài executor; integration test xác nhận đúng một tool call.
- SDK stdio có cleanup shield, chờ process thoát và escalation kết thúc process tree.
  Native asyncio task.cancel có thể phá shield, nên dùng AnyIO cancellation cùng SDK.
  Không thêm framework shutdown. Cleanup grace có thể làm thời gian trả về dài hơn
  timeout request; không bắt đầu attempt mới sau deadline run.
- Nếu exception xảy ra sau khi đã vào SDK call_tool, outcome được đánh dấu thận trọng
  là chưa rõ, kể cả lỗi decode hoặc teardown. Đây không phải bằng chứng đã có side effect.
- Guard vẫn giới hạn trong executor instance và exact name/arguments; persistence,
  concurrency giữa nhiều executor và reconciliation thuộc phase sau.

### Kiểm chứng

- Regression tests: deadline sau discovery, cancel trước discovery, cache/refresh,
  kết quả tool mới, permission/unknown errors, trace business error, deadline hết
  trong retry wait và model replay với call ID mới.
- Client tests: mapping SDK errors, exception groups và executable không tồn tại
  được đánh dấu chắc chắn chưa dispatch.
- Client/integration suite đã chạy: 22 tests pass, gồm server chậm, server cố tình
  block, cancel sau dispatch, connection drop, lần đầu drop rồi retry thành công,
  business error, InputRequired và discovery timeout. Watchdog 20 giây mỗi probe;
  assertion thời gian thao tác kèm cleanup dưới 8 giây và server process đã thoát.
- Full suite cuối: **57 tests passed** trong 39.53 giây (`uv run pytest -q`).
  Ruff lint pass, 28 file đúng format, mypy pass trên 22 source files.
  Phase-2 JSON parse thành công và `git diff --check` không báo lỗi whitespace.
- Chưa commit/push. Đây là bằng chứng cho fixture stdio/in-process đã kiểm tra,
  không phải cam kết cho mọi HTTP server hay mọi task không cooperative.

## Bài học

- Test pass chỉ chứng minh các tình huống đã kiểm tra, không tự chứng minh đạt mọi acceptance criteria.
- Deadline cần bao trùm setup trong run, thực thi và chờ; không được reset sau một bước chậm.
- Retryable, kết quả thực thi chưa rõ, và quyền gọi tool là ba quyết định khác nhau.
- Nhận response thành công không có nghĩa tool thực hiện thành công.
- Tách file cải thiện khả năng đọc; vẫn phải kiểm tra behavior ở ranh giới giữa các file.
- Kết luận hoàn thành trước đó quá sớm; cần đối chiếu yêu cầu với bằng chứng thực thi
  và ghi rõ phần chưa được kiểm chứng trước khi đóng phase.
