# Phase 4.5 — CLI và Web UI

Hai client dùng cùng HTTP RPC. Không client nào tự gọi model/tool hoặc mở database.
CLI dành cho thao tác nhanh và script; Web dành cho quan sát và giải thích hệ thống.
Đây là giao diện local, không phải một sản phẩm có đăng nhập/phân quyền.

## Khởi động

~~~powershell
uv sync --locked
uv run --env-file .env minder-rpc
~~~

Mở http://127.0.0.1:8000 trong trình duyệt. CLI chạy trong terminal thứ hai:

~~~powershell
uv run minder-cli
~~~

Server đọc key/model từ environment. uv --env-file nạp .env; ứng dụng không tự
load file đó. Không đưa API key vào trình duyệt. Model có thể chọn qua GEMINI_MODEL;
các lượt smoke trước đã chạy được gemini-3.5-flash-lite, không phải bằng chứng mọi
model/key/quota luôn dùng được.

Để thử giao diện không tốn request provider:

~~~powershell
uv run python examples/phase4_offline_server.py --port 8000
~~~

Đây là model GIẢ, vẫn dùng HTTP, SQLite và MCP subprocess thật. Không chạy cả hai
server trên cùng port. Offline mock hiểu UNKNOWN và delay_ms=5000 trong task để
tạo ca lỗi hoặc tool chậm; câu trả lời có fake_model=true.

## CLI: tương tác và one-shot

Trong console:

~~~text
session new
task Kiểm tra CNC-04
run cancel
run show <run_id>
run watch <run_id> [after_sequence]
run events <run_id> [after_sequence]
session use <session_id>
watch stop
help
quit
~~~

Task tự bắt đầu watch trong chế độ tương tác. Input vẫn được nhận khi polling đang
chạy; đây là console theo dòng, không phải full-screen TUI nên event có thể xuất
hiện giữa các dòng đang gõ. Reader thread không giữ process sống sau quit/EOF.

One-shot (các option đặt trước command):

~~~powershell
uv run minder-cli --json session new
uv run minder-cli --session <session_id> --watch task "Kiểm tra CNC-04"
uv run minder-cli --json run show <run_id>
uv run minder-cli --verbose run watch <run_id>
uv run minder-cli --url http://127.0.0.1:8000 run cancel <run_id>
~~~

--json xuất JSONL: mỗi dòng có kind/data, data là giá trị public RPC; prompts/help
ở stderr. --verbose mở payload; bản đọc nhanh giới hạn text và ghi rõ truncation.
JSON giữ nguyên giá trị. Exit code 1 nếu client lỗi hoặc watched run kết thúc khác
completed; task hoàn thành bằng thông báo tool lỗi vẫn có thể là completed.
Không --watch thì task chỉ gửi một lần rồi trả ID; không chờ kết quả.

## Web: một trang, hai cách nhìn

**Chọn session cũ:** bấm thanh chọn cạnh nút **+ Session mới** để mở danh sách
do server lưu. Danh sách cuộn được, tải 20 phiên mỗi trang; bấm **Tải thêm session**
để xem tiếp. Xếp phiên mới tạo trước, không đổi thứ tự khi run đang cập nhật.
Tên lấy từ nhiệm vụ đầu tiên; mỗi dòng có thời gian cập nhật, trạng thái run gần
nhất và đuôi ID để phân biệt. ID đầy đủ vẫn có trong chế độ Chi tiết.

Bấm một dòng sẽ mở session và theo dõi run gần nhất nếu có. Gửi task tiếp theo
tạo một run mới trong session đó; không chạy lại task cũ. Đổi session không cancel
run cũ. Mở lại dropdown sẽ tải danh sách mới; lỗi tải không xóa session đang chọn.
Escape hoặc bấm ngoài đóng danh sách; Tab/Enter dùng để chọn bằng bàn phím.

Sau cập nhật này, khởi động lại server bằng lệnh ở trên rồi Ctrl+F5 trên Web.
Migration v3 chỉ bổ sung index tra cứu, không xóa lịch sử.

Đơn giản (mặc định): chọn/tạo session, nhập task, gửi/dừng, trạng thái, tiến trình,
kết quả và thông báo lỗi.

Chi tiết mở thêm:

- Run ID, trạng thái, usage quan sát được và giới hạn runtime.
- Timeline: events của một tool execution được nhóm theo execution_id.
- Chọn event để xem JSON và thành phần kiến trúc liên quan.
- Mở run theo ID, dừng watch, kết nối lại, đọc lịch sử theo trang.
- Copy run ID/public JSON để dùng trong CLI.

Đổi mode không gọi RPC, không đổi session/run/cursor và không restart/cancel task.
Chỉ lưu lựa chọn mode trong localStorage; không tạo kho lịch sử thứ hai.
Mode không phải quyền truy cập: người dùng local đều có cùng quyền RPC.

Sơ đồ là giải thích ownership; tô sáng dựa vào event đang chọn, không mô phỏng
suy nghĩ hay đo từng hàm. Provider và MCP là hai nhánh từ loop; SQLite ghi trace.
Không stream từng token, không có phần trăm tiến độ suy đoán.

### Pipeline kiến trúc

Trong chế độ Chi tiết, pipeline có mũi tên gọi đi/trả về, nhánh Provider → LLM,
nhánh MCP executor → MCP server/tools và đường kết quả quay về loop.
SQLite là kho lưu trace, không phải bước cuối mới được gọi sau tool.

- Mặc định theo event mới nhất đã nhận qua polling, không tự giả lập trạng thái.
- Chọn event ở timeline sẽ ghim vào event đó; nhãn ghi rõ event đã chọn.
- Nút **Về event mới nhất** bỏ ghim, không gọi lại API hay tạo run mới.
- Bấm một khối để xem trách nhiệm và tối đa 8 event liên quan gần nhất.
- Trạng thái có màu lẫn chữ/ký hiệu; retry waiting và cancellation tách khỏi error.
- Stop watch/mất kết nối giữ event cuối và ghi rõ không còn theo dõi.
- Màn hình nhỏ cuộn ngang trong sơ đồ; các khối là button dùng được bằng bàn phím.

Dispatch intent chỉ chứng minh ý định request đã được ghi, không xác nhận remote
tool/model đã hoàn thành. Màu của event không phải trạng thái sức khỏe của mọi
thành phần trên đường đi. Không suy ra suy nghĩ riêng của LLM.

## Watch, lỗi và giới hạn

Polling mặc định 500 ms, mỗi request client tối đa 10 giây. Terminal state phải do
server xác nhận, sau đó client đọc hết các trang event đã commit. Cursor riêng từng
run; chuyển run bỏ qua response cũ. RPC lỗi thì dừng auto-poll và giữ dữ liệu cuối,
người dùng bấm watch để thử lại.

Web giữ tối đa 300 events gần nhất trong màn hình, có thông báo khi cắt phần cũ;
history inspector vẫn đọc public JSON từng trang 100 events. CLI --verbose/readable
có truncation rõ ràng; --json không cắt payload.

Cancel acknowledgment có thể là cancel_requested. Completion thắng race thì giữ
completed. Không khẳng định tool từ xa đã rollback; outcome_unknown nằm trong
trace. Watch stop, quit, đóng tab chỉ dừng quan sát; HTTP server riêng vẫn chạy.

Mất response sau submit/create/cancel có thể không biết kết quả; không tự gửi lại.
Web có danh sách session và mở run gần nhất; chưa có tìm kiếm hoặc danh sách mọi
run trong một session. CLI vẫn dùng ID để reattach. Chọn session khác không cancel
session cũ. Restart client bắt đầu từ cursor 0, CLI nhận cursor tường minh.

Web dùng textContent thay vì render tool output thành HTML, CSP không cho script
inline, chỉ phục vụ static directory trong package. RPC từ browser khác origin bị
từ chối; đây không phải hệ thống auth, không public expose server.

## Kiểm chứng

~~~powershell
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy
node --test tests/web_client.test.mjs
uv build
~~~

Node chỉ dùng cho test JavaScript; chạy ứng dụng không cần Node/npm. Nếu máy không
có Node, pytest sẽ skip nhóm JS và cần chạy riêng trước nghiệm thu.
Test gồm cursor/dedup, responsive cancel, lost submit không replay, same-origin,
static assets và mode switch. Socket fixture chạy CLI one-shot và interactive
cancel qua process thật với model giả/MCP thật.

Chưa dùng browser automation để xác minh visual layout/keyboard, chưa chạy paid
live demo qua các client mới. Phase 4 live success/failure/cancel/restart trước đó
là bằng chứng backend, không thay thế validation riêng của giao diện này.
