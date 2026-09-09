# Phase 5 — Báo cáo triển khai và kiểm chứng

Ngày lập báo cáo: **09/09/2026**. Lượt kiểm chứng được báo cáo bắt đầu lúc
**19:23:12 ngày 08/09/2026, UTC+07:00**, trước khi cuộc trao đổi được tiếp tục.
Không coi đây là một lượt chạy test mới ngày 09/09.

## 1. Kết luận

**Phase 5 đã triển khai phạm vi kiểm chứng offline đã duyệt.** Phase này không
chỉ tạo một helper “test harness”: nó bao gồm hạ tầng test dùng chung, fixture
database, fault injection, fake provider có điều khiển, kiểm chứng lifecycle
database và các tình huống tích hợp agent nhiều tool.

Kết quả mới nhất: **172 passed, 0 failed, 0 errors, 0 skipped**.
Trong đó có **28 lượt test mới của Phase 5**, ngoài baseline 144 test trước đó.
Ruff check/format và Mypy đều pass.

Đây là bằng chứng cho hành vi đã được kiểm tra trong môi trường thử nghiệm,
không phải khẳng định hệ thống không còn bug hoặc đã sẵn sàng cho mọi tải production.
Gemini thật, thiết bị nhà máy thật và kiểm thử giao diện trực quan không nằm
trong lượt chạy này.

## 2. Phase 5 thực tế gồm những gì?

Đối chiếu sáu work item trong [JSON Phase 5](../phases/phase-5-bonus-test-support.json):

| Phần | Đã triển khai | File chính | Tác dụng |
| --- | --- | --- | --- |
| W1 — Runtime test dùng chung | open_runtime mở store, MCP nếu cần, rồi service; đóng theo thứ tự ngược | [support.py](../../tests/support.py) | Mỗi test không phải dựng lại toàn bộ hệ thống; cleanup vẫn chạy khi có lỗi |
| W2 — Database seed và isolation | seed session/run/task state, SQLite riêng theo tmp_path, mở lại dữ liệu đã commit | [support.py](../../tests/support.py), [test_database_lifecycle.py](../../tests/test_database_lifecycle.py) | Test không lẫn dữ liệu; restart test kiểm tra persistence thật |
| W3 — Fault injection và coverage map | fail_record, lose_commit_ack, phục hồi monkeypatch theo scope; giữ crash fixtures cũ | [support.py](../../tests/support.py), [test_persistence.py](../../tests/test_persistence.py), [test guide](../../tests/README.md) | Có thể gây lỗi đúng thời điểm và biết mỗi assertion chứng minh điều gì |
| W4 — Fake provider có điều khiển | Sequence cũ, async callback, respond đọc context và ProviderGate | [fakes.py](../../tests/fakes.py), [support.py](../../tests/support.py), [test_support.py](../../tests/test_support.py) | Kiểm tra chờ/cancel/failure và tool feedback mà không cần API key |
| W5 — Database lifecycle/fault cases | 10 tình huống về migration, SQL lock, cleanup, isolation và lỗi domain | [test_database_lifecycle.py](../../tests/test_database_lifecycle.py) | Kiểm chứng transaction/lifecycle, không chỉ CRUD thành công |
| W6 — Tích hợp nhiều tool | 5 happy case, 5 edge case, hai bộ dữ liệu và một biến thể batch | [test_multistep_scenarios.py](../../tests/test_multistep_scenarios.py), [factory_data.py](../../tests/fixtures/factory_data.py), [multistep_mcp.py](../../tests/fixtures/multistep_mcp.py) | Kiểm tra chuỗi phụ thuộc, liên kết dữ liệu, giới hạn, cancel và allowlist |

Các phần này đều thuộc **test engineering và kiểm chứng tích hợp**. Không có
bốn tầng TestHarness/DatabaseHarness/ToolHarness/ModelHarness mới trong production.
Các cơ chế persistence, retry, cancellation và replay guard là cơ chế đã tồn tại;
Phase 5 bổ sung cách dựng tình huống và bằng chứng kiểm tra chúng.

Ngoài code test, đã có hướng dẫn chạy, mapping 20 scenario ID, nhật ký quyết định
và checklist đánh giá model thật tùy chọn. Checklist đã viết; live evaluation
chưa được thực hiện.

## 3. Môi trường và phương pháp

| Thành phần | Thực tế sử dụng |
| --- | --- |
| Máy kiểm chứng | Windows, PowerShell |
| Python / uv | Python 3.13.12 / uv 0.12.2 |
| Node | v24.14.0, dùng cho JavaScript contract tests |
| Database | SQLite file thật, riêng trong thư mục tạm của từng test |
| MCP ở nhóm multi-tool | Process stdio riêng, dùng ba tool read-only hiện có |
| Dữ liệu nhà máy | Fixture alpha/beta, không phải dữ liệu thiết bị thật |
| Model ở nhóm Phase 5 | Fake provider có kịch bản, nhận kết quả tool qua interface bình thường |
| Provider adapter trong regression | Kiểm thử payload/lỗi bằng fixture; không gọi Gemini thật |

Không đọc/sửa .env, không dùng database server đang hoạt động, không restart
server và không phát sinh request model thật trong lượt kiểm chứng.

Luồng multi-tool được kiểm tra:

```text
Input test → AgentService → AgentLoop → provider giả
                               ↕ tool requests/results
                        MCP executor → process MCP fixture
                               ↕
                       SQLite events / attempts / messages
                               ↓
                    Đóng store → mở lại → đối chiếu
```

Có ba nguồn bằng chứng khác nhau:

- Provider call history: model giả thực sự nhận những tool results nào.
- dispatch.jsonl: tool/key/PID được ghi bên trong MCP process khi tool vào thực thi.
- SQLite: trạng thái run, request/result, event sequence và transcript đã commit.

Event intent chỉ chứng minh ý định dispatch; log phía MCP mới bổ sung bằng chứng
tool đã vào thực thi. Timeout hoặc cancel không tự chứng minh tác dụng từ xa đã
được rollback.

## 4. Kết quả và cách đếm

Bằng chứng máy đọc được: [JUnit XML toàn bộ suite](artifacts/phase-5-full-suite.xml).
Từng testcase và thời gian của nó nằm trong file này.

| Nhóm | Số lượt | Kết quả |
| --- | ---: | --- |
| Database lifecycle mới | 10 | PASS |
| 5 happy case × alpha/beta | 10 | PASS |
| 5 edge case tích hợp | 5 | PASS |
| Batch các lần đọc máy độc lập | 1 | PASS |
| Helper: release/failure và teardown error | 2 | PASS |
| **Tổng Phase 5 mới** | **28** | **PASS** |
| Regression còn lại | 144 | PASS |
| **Toàn bộ pytest suite** | **172** | **PASS** |

20 tình huống đã duyệt tạo thành 25 lượt do happy cases chạy hai bộ dữ liệu.
Thêm một ca batch và hai ca helper thành 28 lượt; không phải 28 yêu cầu mới.
Các test cũ chuyển sang helper chung không được tính là test mới.

- Pytest console: **172 passed trong 93.73 giây**.
- Thời gian testsuite trong XML: **93.591 giây**; đây là phép đo của reporter,
  khác phạm vi đo tổng thời gian được console hiển thị.
- Ruff check: **PASS**.
- Ruff format --check: **PASS**, 82 file tại lượt kiểm tra.
- Mypy: **PASS**, 58 source files được kiểm tra.
- JavaScript contract suite được gọi bên trong một pytest case; không cộng
  số test Node thêm vào con số 172.

Số liệu 113.51 giây trong nhật ký triển khai là của lượt trước, không mâu thuẫn
với lượt report này. Các con số thời gian không phải benchmark latency production.
Report không đo line/branch coverage và không tuyên bố coverage 100%.

## 5. Những tình huống đã được kiểm chứng

### Database — DB01 đến DB10

| ID | Tình huống và kết quả đã quan sát |
| --- | --- |
| DB01 | Lỗi giữa migration: schema/version không cập nhật nửa chừng, session fixture còn nguyên, ownership được nhả |
| DB02 | Bỏ lỗi giả lập rồi mở lại: migration thành công, session cũ đọc được |
| DB03 | Connection khác giữ SQL write lock: khi nhả lock trong thời gian chờ, thao tác commit thành công đúng một lần |
| DB04 | Quá thời gian chờ lock: nhận SQLITE_BUSY → StorageError, không ghi dở; store quarantine theo policy hiện tại |
| DB05 | Thoát runtime bình thường: run đang chờ bị cancel, đóng store/MCP client và giải phóng owner |
| DB06 | Body test phát sinh exception: vẫn cleanup và quan sát được lỗi gốc |
| DB07 | Discovery thất bại sau khi mở DB/MCP: các dependency được dọn, DB mở lại được |
| DB08 | Run/tool trong runtime A không làm xuất hiện dữ liệu hoặc fake call history trong B |
| DB09 | Fault patch được khôi phục sau exception; fixture mới hoạt động bình thường |
| DB10 | Conflict nghiệp vụ không làm store quarantine; thao tác hợp lệ sau đó vẫn commit |

DB03 dùng tín hiệu SQL trace để phối hợp lúc BEGIN được gọi khi connection khác
đang giữ lock; DB04 kiểm tra mã SQLITE_BUSY thực. Các test này không phải kiểm thử
tải cao hay bằng chứng xử lý mọi loại lỗi khóa database.

### Happy cases — H01 đến H05

| ID | Luồng | Tool calls | Model steps trong fixture tuần tự |
| --- | --- | ---: | ---: |
| H01 | Máy → area → work order → procedure | 3 | 4 |
| H02 | Chọn work order → đọc máy → kiểm tra warning → procedure | 3 | 4 |
| H03 | Đọc work orders → kiểm tra ba máy → chọn máy cảnh báo nóng nhất → procedure | 5 | 6 |
| H04 | Hai chuỗi máy/work order/procedure, tổng hợp riêng từng máy | 6 | 7 |
| H05 | Máy → work order → procedure → đọc lại máy → so sánh snapshot | 4 | 5 |

Mỗi ca có alpha/beta thay đổi area và mã liên kết. Assertion kiểm tra:

- Argument bước sau và output phải khớp dữ liệu fixture đã trả.
- Result xuất hiện trong provider input với đúng tool_call_id.
- Intent/result có thứ tự đúng ở fixture tuần tự.
- Dữ liệu máy, work order và procedure không ghép chéo.
- Trạng thái và output còn đọc được sau khi đóng/mở lại store.

Ca batch riêng của H03 có **5 tool calls nhưng 4 model steps**. Một model response
có thể yêu cầu nhiều lần đọc độc lập; điều đó không có nghĩa executor thực thi
song song. Hai chuỗi ở H04 cũng không bị mô tả là có phụ thuộc lẫn nhau.

### Edge cases — E01 đến E05

| ID | Kết quả run | Kết quả kiểm chứng |
| --- | --- | --- |
| E01 | limit_exceeded | Budget 3 model steps không đủ hoàn thành chuỗi; chỉ hai tool được dispatch, không có output thành công |
| E02 | completed | Script báo thiếu procedure_code; không gọi procedure với mã bịa |
| E03 | completed | Một read bị MCP_TIMEOUT; lưu các kết quả còn lại và báo incomplete, không khẳng định máy thắng toàn cục |
| E04 | cancelled | Cancel sau hai kết quả đã nhận; tool thứ ba không dispatch, trace trước đó vẫn còn |
| E05 | completed | Model giả cố tình xin tool ngoài allowlist; request bị từ chối local, không tới MCP |

**Test PASS nghĩa là hành vi khớp mong đợi**, không có nghĩa mọi run đều completed.
Run completed cũng có thể trả lời rằng dữ liệu thiếu hoặc không thể kết luận.

E02/E03 chỉ chứng minh loop truyền lỗi/dữ liệu thiếu và tiếp nhận kết luận của
script. E05 chứng minh allowlist chặn được yêu cầu không được phép. Chúng không
chứng minh Gemini tự xử lý đúng thiếu dữ liệu hoặc chống prompt injection.

## 6. Phần cũ được tái sử dụng, không bị tính thành tính năng mới

- Hai test live cancel/shutdown trong test_persistence.py chuyển sang
  open_runtime + ProviderGate; giữ assertion cũ và thêm kiểm tra cancellation tới fake.
- service_for trong test_rpc.py dùng chung runtime setup.
- Seed và các test lỗi lưu intent/result, mất commit acknowledgment dùng helper
  có scope thay vì lặp lại logic patch.
- Giữ nguyên coverage crash process bốn cửa sổ, replay guard, recovery, ownership,
  migration cũ, MCP disconnect và lifecycle.

Tên testcase đầy đủ nằm trong [bản đồ coverage](../../tests/README.md) và
test_coverage của [JSON Phase 5](../phases/phase-5-bonus-test-support.json).

## 7. Giới hạn và phần chưa làm

| Chưa được chứng minh / không nằm trong scope | Vì sao cần phân biệt |
| --- | --- |
| Gemini tự chọn đúng chuỗi tool | Provider trong Phase 5 là scripted; live eval mới chỉ có checklist |
| Chống mọi prompt injection | E05 kiểm tra allowlist, không đánh giá toàn bộ hành vi model |
| Lỗi ổ đĩa vật lý hoặc mất điện | Storage faults được tiêm; crash fixture chỉ kết thúc process thử nghiệm |
| Remote tool luôn dừng/rollback khi cancel | Timeout/cancel local không đảm bảo tác dụng ở hệ thống ngoài |
| Khả năng chịu tải, multi-worker, multi-user | Các ca deterministic trên máy local không phải load/stress test |
| Web hiển thị đẹp hoặc đã tích hợp fixture nhiều máy | Phase 5 không thêm UI hoặc tự đưa dữ liệu fixture vào Web đang chạy |
| Phần trăm code coverage | Lượt này không chạy công cụ đo coverage |

Code production và policy retry/cancel/persistence không bị đổi trong Phase 5.
Đây là cải thiện khả năng kiểm chứng và bảo trì, không phải thêm các tầng production
mang tên Database Harness hoặc Model Harness.

## 8. Tái hiện và bước tiếp theo

Chạy toàn bộ bằng chứng:

```powershell
uv run pytest -q --junitxml=docs/reports/artifacts/phase-5-full-suite.xml
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Lệnh XML ghi lại artifact cùng tên; nếu muốn giữ nguyên bằng chứng đính kèm report,
dùng một tên file mới. Sau khi code thay đổi, kết quả cũ là snapshot lịch sử,
không tự đại diện cho phiên bản mới.

Để hiểu một chuỗi trước:

```powershell
uv run pytest tests/test_multistep_scenarios.py -vv -k H01
```

Bước tiếp theo hợp lý là live eval H01 với Gemini thật, MCP fixture riêng và DB tạm,
sau khi người dùng cho phép request model. Đánh giá kết quả model riêng với kết quả
test harness, không ép một số bước cố định nếu có cách thực hiện hợp lệ khác.
