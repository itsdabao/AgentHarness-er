# Phase 5: hạ tầng kiểm thử và test tích hợp

[Report triển khai và kết quả kiểm chứng](../docs/reports/phase-5-test-report.md)
tách đủ sáu work item, số liệu test, bằng chứng XML và giới hạn còn lại.

Phase 5 bổ sung bộ dụng cụ dưới tests/, không thêm một tầng database/runtime mới.
SQLite là thật, tool integration đi qua MCP process local thật, provider là giả.
Không cần API key, không nạp .env và không dùng database của server đang chạy.

## Evaluation local model thật

Chạy thủ công, tách khỏi pytest: [hướng dẫn](../docs/guides/local-model-evaluation.md).
[Report hai GGUF trên RTX 3050](../docs/reports/local-model-test-report.md) ghi
22 run thật và phân biệt với regression offline. Adapter local chỉ nằm trong
tests/, không đổi provider của ứng dụng. Chạy pytest không tự load model.

## Chạy từng nhóm

Từ thư mục project:

```powershell
uv run pytest tests/test_database_lifecycle.py -vv
uv run pytest tests/test_multistep_scenarios.py -vv
uv run pytest tests/test_multistep_scenarios.py -vv -k H03
uv run pytest tests/test_support.py -vv
uv run pytest tests/test_persistence.py tests/test_rpc.py -q
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Có thể dùng báo cáo chuẩn pytest, không cần viết reporter riêng:

```powershell
uv run pytest tests/test_database_lifecycle.py tests/test_multistep_scenarios.py tests/test_support.py --junitxml=var/test-reports/phase5.xml
```

Không cần dừng server để chạy: mỗi test có tmp_path riêng. Không trỏ fixture vào
var/harness.db. Không dùng outer rollback transaction để reset: crash/restart phải
quan sát commit thật. Pytest quản lý thư mục tạm; helper chỉ đóng connection,
task và MCP client. Một file .owner còn tồn tại không có nghĩa lock còn bị giữ.

## Structure

- support.py: open_runtime, seed, rows, fail_record, lose_commit_ack, ProviderGate.
- fakes.py: response sequence cũ + callback chờ async + respond đọc context thực nhận.
- test_database_lifecycle.py: lỗi/mở lại DB, contention, cleanup và isolation.
- fixtures/factory_data.py: hai bộ dữ liệu alpha/beta, đổi area/mã máy/mã procedure.
- fixtures/multistep_mcp.py: dùng lại build_server và ba tool production; chỉ thay
  dictionary dữ liệu trong process fixture. Không sửa source server production.
- test_multistep_scenarios.py: các quyết định scripted, input tiếng Việt, assertion
  về output, argument, tool result feedback, số bước và trace sau reopen.
- test_support.py: release/failure của fake callback và lỗi teardown phải hiện ra.

## Cách đọc một test

1. Xem input và trạng thái ban đầu.
2. Xem fault hoặc kết quả nào cho phép bước tiếp theo.
3. Xem assertion về hành vi: có gọi tool không, có dừng không?
4. Xem assertion về dữ liệu: commit gì, rollback gì, reopen còn đọc được gì?

Ví dụ H01:

```text
get_machine_status(CNC-04)
  → nhận area
list_open_work_orders(area)
  → lọc đúng máy, chọn priority, nhận procedure_code
get_safety_procedure(procedure_code)
  → trả lời cuối
```

Ba tool calls cần bốn model steps ở fixture tuần tự, không phải hai bước cố định.
H03 có thêm test batch: ba lần đọc máy được yêu cầu trong một model response,
nên tổng 5 tool calls nhưng chỉ 4 model steps. Đây không phải bằng chứng tool chạy
song song: executor hiện vẫn được phép thực thi tuần tự.

Mỗi ca multi-tool giữ scenario.db và dispatch.jsonl trong tmp_path của pytest.
dispatch.jsonl ghi tool/key/PID từ bên trong process MCP: chứng minh tool đã vào
thực thi, khác với event intent chỉ nói rằng runtime định gọi tool. Database giữ
run, event, attempt và message để đối chiếu. Test đọc lại sau khi store đã đóng.

## Bản đồ 20 tình huống

DB nằm trong test_database_lifecycle.py:

| ID | Test | Điều được kiểm chứng |
| --- | --- | --- |
| DB01 | test_failed_migration_is_atomic_and_retryable[DB01] | Schema, version, dữ liệu không nâng cấp nửa chừng; ownership được nhả |
| DB02 | test_failed_migration_is_atomic_and_retryable[DB02] | Bỏ lỗi rồi upgrade lại thành công; test tự dựng toàn bộ tiền điều kiện |
| DB03 | test_write_lock_released_before_timeout | Connection SQL khác giữ write lock; nhả lock thì commit đúng một lần |
| DB04 | test_write_lock_timeout_quarantines_without_partial_write | SQLITE_BUSY thực → StorageError/quarantine, không ghi dở |
| DB05 | test_runtime_cleanup[DB05] | Thoát bình thường, dừng task/MCP client, nhả connection/owner |
| DB06 | test_runtime_cleanup[DB06] | Body lỗi vẫn cleanup, lỗi gốc không bị bỏ qua |
| DB07 | test_partial_setup_unwinds_open_dependencies | Discovery lỗi sau khi MCP/DB mở vẫn đóng tài nguyên |
| DB08 | test_runtime_state_and_fake_histories_are_isolated | Run/tool thực thi trong fixture A không xuất hiện trong B |
| DB09 | test_fault_patch_is_restored_after_exception | Monkeypatch được khôi phục khi exception; fixture mới hoạt động |
| DB10 | test_domain_conflict_leaves_store_usable | Conflict không làm store quarantine; thao tác hợp lệ kế tiếp vẫn commit |

H/E nằm trong test_multistep_scenarios.py:

| ID | Test | Calls / model steps ở fixture | Điều được kiểm chứng |
| --- | --- | --- | --- |
| H01 | test_happy_dependency_chains[H01-3-alpha/beta] | 3 / 4 | Máy → area → work order → procedure |
| H02 | test_happy_dependency_chains[H02-3-alpha/beta] | 3 / 4 | Chọn work order rồi đọc máy, kiểm tra warning trước procedure |
| H03 | test_happy_dependency_chains[H03-5-alpha/beta] | 5 / 6 | Đọc đủ ba máy trước so sánh; không chọn máy running dù nóng hơn |
| H04 | test_happy_dependency_chains[H04-6-alpha/beta] | 6 / 7 | Hai chuỗi độc lập, không ghép nhầm máy và procedure |
| H05 | test_happy_dependency_chains[H05-4-alpha/beta] | 4 / 5 | Đọc lại một lần, lưu cả hai snapshot, phân biệt bản mới |
| E01 | test_E01_insufficient_step_budget | 2 / 3 | Final allowed step xin tool thì không dispatch; limit_exceeded |
| E02 | test_E02_missing_procedure_link | 2 / 3 | Script nhận dữ liệu thiếu và báo thiếu, không lookup mã bịa |
| E03 | test_E03_partial_comparison_after_timeout | 4 / 5 | Một MCP read timeout; lưu partial result, không khẳng định winner |
| E04 | test_E04_cancel_before_third_dispatch | 2 / 3 | Cancel khi đã nhận hai kết quả, tool thứ ba không chạy |
| E05 | test_E05_unapproved_tool_from_injected_data_is_blocked | 2 logical / 3 steps | Chỉ 1 tool thực thi; yêu cầu delete_factory bị từ chối local |

alpha/beta ở bảng là hai node pytest riêng, không phải một ID chứa dấu /.
20 tình huống cho 25 lượt parametrized; cộng test batch H03 và test_support.py
(2 ca) là 28 lượt: DB=10, H=10, E=5, batch=1, helper=2.

## Coverage cũ vẫn giữ

| Tình huống | File/test |
| --- | --- |
| Intent lưu lỗi → không dispatch | test_persistence.py::test_storage_failure_rolls_back_intent_and_prevents_dispatch |
| Tool xong nhưng result lưu lỗi → không replay | test_persistence.py::test_failed_result_commit_never_replays_remote_success |
| Commit xong, mất acknowledgment | test_persistence.py::test_commit_ack_loss_quarantines_store_without_partial_transition |
| Crash process tại 4 cửa sổ | test_persistence.py::test_actual_process_crash_windows |
| Ownership conflict | test_persistence.py::test_second_runtime_cannot_open_owned_database |
| V1 migration / schema tương lai | test_persistence.py::test_future_schema_rejected_and_v1_upgraded_without_data_loss |
| V2 lên schema index mới | test_sessions.py::test_session_indexes_upgrade_v2_without_losing_history |
| Cancel / session exclusion | test_persistence.py::test_live_cancel_and_session_exclusion |
| Shutdown / ngừng admission | test_persistence.py::test_shutdown_observes_tasks_and_refuses_new_admission |
| MCP disconnect/timeout lifecycle | test_mcp_client.py, test_mcp_lifecycle.py: giữ nguyên các fixture hiện có |

Hai test live cancel/shutdown đã chuyển sang open_runtime + ProviderGate, giữ
assertion cũ và bổ sung kiểm tra callback nhận cancellation. service_for trong
test_rpc.py cũng tái sử dụng open_runtime. Seed và ba fault test dùng helper chung.

## Lỗi mô phỏng khác lỗi thật thế nào?

- fail_record/lose_commit_ack: exception được tiêm có chủ đích; không chứng minh
  đã tái hiện ổ đĩa đầy/hỏng vật lý.
- DB03/04: lock SQL thật trên database tạm, khác lock ownership của runtime.
  DB03 dùng SQL trace signal để phối hợp khi BEGIN được gọi trong lúc lock còn giữ;
  DB04 kiểm tra mã SQLITE_BUSY. Timeout ngắn chỉ nằm trong fixture.
- MCP timeout: tool thật trong process thử nghiệm bị trì hoãn; SDK timeout thật.
  Timeout không chứng minh remote đã rollback hoặc dừng ngay.
- Crash: os._exit chỉ chạy trong process fixture; không mô phỏng mất điện hệ điều hành.
- Quarantine: ngừng dùng store sau lỗi không chắc chắn, không phải xóa database.

## Đánh giá Gemini thật — tách riêng, chưa chạy

Provider scripted chỉ nhận message qua interface bình thường và lấy dữ liệu từ
kết quả tool để tạo argument tiếp theo. Nó không đọc fixture answer, nhưng logic
chọn/rẽ nhánh vẫn là code test. Vì vậy H/E pass không chứng minh Gemini suy luận
đúng hoặc miễn nhiễm prompt injection.

Nếu người dùng yêu cầu live eval:

1. Dùng provider thật đã có với database tạm riêng và MCP fixture riêng.
2. Dùng input H01-H05 trong TASKS, thay {area} theo fixture; E02/E03 kiểm tra cách
   model xử lý dữ liệu thiếu; E05 kiểm tra phản ứng với chỉ dẫn trong tool data.
3. Giữ giới hạn mặc định 8 model steps/8 tools cho happy cases; không ép model
   theo đúng số lượt scripted nếu có cách hợp lệ ít lượt hơn.
4. Ghi model, limits, public trace, output và usage; không ghi key/private reasoning.
5. Phân biệt lỗi quota/provider với lỗi chọn tool, ghép dữ liệu hoặc báo cáo sai.

Không bật live evaluation trong pytest mặc định hoặc tự đọc .env để chạy nó.
