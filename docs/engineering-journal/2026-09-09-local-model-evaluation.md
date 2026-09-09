# Đánh giá local model và so sánh Gemini

- Ngày: 09/09/2026
- Trạng thái: đã chạy evaluation và viết report
- Nguồn: người dùng yêu cầu tự thử hai GGUF, tạo report và tài liệu so sánh API/local.

## Lúc bàn

Máy RTX 3050 Laptop 4 GB VRAM, RAM 16 GB. Folder models có Qwen2.5 3B Q4_K_M
và Qwen3 4B Instruct 2507 Q8_0. Dự kiến model nhỏ thử trước, model Q8 dùng
CPU/GPU hybrid. Giữ nguyên Gemini mặc định và không dùng API trả phí.

## Triển khai

- Tải llama.cpp b10867 CUDA 12.4 từ release chính chủ; đối chiếu SHA256 hai
  archive, giải nén portable dưới .tools/. Không cài hệ thống hoặc đổi PATH lâu dài.
- Skill gitnexus-exploring được dùng để tìm điểm tái sử dụng. Repo chưa có index
  nên đọc contract/provider/test helper trực tiếp, không query repo khác.
- Tạo LocalEvalProvider trong tests/, encode tool exchanges và nhận quyết định
  thật từ model qua HTTP loopback. Không đổi AgentLoop hoặc application wiring.
- Tái sử dụng open_runtime, target_for và factory fixtures. Mỗi case có DB riêng,
  ghi JSON trace sau khi kiểm tra dữ liệu persist qua reopen.
- Chạy chín case/model: năm happy task, ba edge task và một cancellation.
- Sau baseline mới thêm hai diagnostic/model để phân biệt task tổng quát với
  một bước hoặc chỉ dẫn dependency rõ. Giữ kết quả hai nhóm riêng.
- Đọc metadata GGUF và hash để nhận diện weights; không khẳng định provenance
  chỉ bằng tên file. Thêm ignore cho binary runtime/weights, giữ log test trong report.

## Phát hiện và điều chỉnh

Model có thể trả completed nhưng sai nghiệp vụ, đoán area/procedure hoặc trả
array thay object. Không sửa output để làm pass.

Baseline grader đếm dictionary reads như toàn bộ tool invocations: sai ở H05
vì delay_ms=10000 bị từ chối trước dictionary access. Sửa check cho lần chạy mới,
thêm regression test và ghi rõ trong report; không sửa artifact baseline.

Phép đo startup ban đầu gồm cả hash; diagnostic tách health-ready khỏi hash.
Thêm GPU snapshots và verbosity để xác minh offload. Diagnostic giới hạn CORS
thay vì default wildcard; server vẫn chỉ mở loopback trong thời gian test.

## Kết quả

- Baseline nghiệp vụ: Qwen2.5 1/8, Qwen3 2/8 theo strict rubric.
- Cancel khi đang chờ HTTP: cả hai đạt; terminal snapshot khoảng 37 ms và 24 ms.
- Diagnostic: cả hai đạt một tool và ba tool với hướng dẫn rõ.
- 184 pytest offline pass, Ruff/format/Mypy pass. Không cộng local eval vào pytest.
- Không gọi Gemini nên tài liệu so sánh không có tuyên bố thắng thua định lượng.
- Hai server local đã thoát; không đổi key, .env hoặc database ứng dụng.

[Report và artifacts](../reports/local-model-test-report.md),
[so sánh Gemini/local](../architecture/gemini-vs-local.md),
[hướng dẫn chạy lại](../guides/local-model-evaluation.md).

## Bài học

Phải chấm cả nguồn dữ liệu và thứ tự lấy dữ liệu, không chỉ JSON cuối có vẻ đúng.
Một prompt cải tiến sau khi xem lỗi là diagnostic, không phải bằng chứng held-out.
Log tên dispatch không bảo đảm nó đo đúng thời điểm dispatch: cần đọc fixture.
Adapter local dùng được cho evaluation chưa đồng nghĩa đã sẵn sàng nối production.
