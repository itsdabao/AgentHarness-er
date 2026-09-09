# Chạy evaluation với local GGUF

Đây là công cụ test thủ công, không đổi provider mặc định của minder-rpc và không
gọi Gemini. Đọc [report đã chạy](../reports/local-model-test-report.md) và
[so sánh Gemini/local](../architecture/gemini-vs-local.md) trước khi dùng kết quả.

## Chuẩn bị

- Python 3.13, uv và dependencies phát triển: uv sync --locked.
- Hai weights trong models/: qwen2.5-3b-instruct-q4_k_m.gguf và
  qwen3-4b-instruct-2507-q8_0.gguf.
- Windows x64, NVIDIA driver tương thích CUDA 12.4.
- Bản portable [llama.cpp b10867](https://github.com/ggml-org/llama.cpp/releases/tag/b10867).

Trong workspace đã tải và giải nén:

~~~text
.tools/llama-b10867-cuda12/
  runtime/llama-server.exe
  cuda/cublas64_12.dll
  cuda/cublasLt64_12.dll
  cuda/cudart64_12.dll
~~~

Máy clone repo mới phải tự chuẩn bị binary/weights; .tools/ và *.gguf bị gitignore.
Không cần cài runtime vào Windows hoặc đổi PATH vĩnh viễn.

Hai archive chính chủ đã đối chiếu SHA256 với GitHub release:

| Archive | SHA256 |
| --- | --- |
| llama-b10867-bin-win-cuda-12.4-x64.zip | 29b3b8989b1b4479660f3dd60af8d928976947a05846cdaae815f810176b6692 |
| cudart-llama-bin-win-cuda-12.4-x64.zip | 8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6 |

Giải nén archive llama vào runtime/ và cudart vào cuda/. Runner chỉ bổ sung
cuda/ vào PATH của process con. Không tải weights mới trong runner.

## Chạy từ gốc repo

Đóng ứng dụng đang chiếm nhiều VRAM nếu cần. Mỗi lệnh tự mở server 127.0.0.1:18081,
đợi ready, chạy từng case rồi đóng đúng process mình tạo, kể cả khi lỗi.
Không chạy hai lệnh cùng lúc.

~~~powershell
uv run python tests/eval_local_models.py --server .tools/llama-b10867-cuda12/runtime/llama-server.exe --cuda-dir .tools/llama-b10867-cuda12/cuda --model models/qwen2.5-3b-instruct-q4_k_m.gguf --gpu-layers 99 --output var/local-eval/qwen25-01

uv run python tests/eval_local_models.py --server .tools/llama-b10867-cuda12/runtime/llama-server.exe --cuda-dir .tools/llama-b10867-cuda12/cuda --model models/qwen3-4b-instruct-2507-q8_0.gguf --gpu-layers 24 --output var/local-eval/qwen3-01
~~~

Mặc định chạy H01–H05, E_missing, E_unknown, E_injection và C_cancel.
Tên output phải mới: runner không ghi đè kết quả cũ. Mỗi case có SQLite/MCP
fixture riêng, không mở var/harness.db.

Để chạy hai diagnostic, dùng một output mới và thêm:

~~~text
--case D_single --case D_guided
~~~

Có thể dùng --case H01 để chạy riêng một case. Diagnostic không nằm trong bộ
mặc định vì được thiết kế sau khi xem baseline.

Server test không có UI, CORS giới hạn một origin loopback. Không public expose
hoặc dùng như service nhiều người. Chỉ mở trong thời gian evaluation.

## Đọc kết quả

- summary.json: model hash/metadata, settings, GPU snapshots và bảng kết quả.
- CASE.json: request/response thật, run snapshot, events, attempts và grade.
- CASE/trace.db: DB để kiểm tra local; không đưa vào Git.
- CASE/dispatch.jsonl: dictionary-read log của fixture, không phải mọi lần vào tool.
- server.log: thông tin CUDA, số layer offload, bộ nhớ và timing.

Exit code 0 nghĩa runner chạy xong, **không có nghĩa mọi case đạt**.
Xem grade.passed và từng check. Nonzero là lỗi runner/startup/I/O cần xử lý.

Output model phải là JSON object đúng task. Không tự bỏ markdown fences,
ép array thành object hay sửa field để tăng pass rate. Nội dung assistant đi kèm
tool call vẫn có trong raw response, nhưng core chỉ nhận tool decision ở turn đó.

Giới hạn hiện tại: HTTP timeout 120 s/request; run 240 s, 12 steps, 12 tools;
output 512 token, context 4096. Loop kiểm soát deadline/cancel, adapter không retry.
temperature=0 và seed=42 không đảm bảo bit-for-bit trên mọi runtime/phần cứng.

Baseline 09/09 dùng check server_attempt_count quá chặt; runner hiện dùng
server_read_count_consistent. Đồng thời đã tách health-ready khỏi thời gian hash,
thêm metadata/GPU samples và giới hạn CORS. Raw baseline không bị sửa. Vì vậy
không kỳ vọng summary chạy mới giống từng field với artifact lịch sử.

## Kiểm tra code mà không load model

~~~powershell
uv run pytest tests/test_local_eval.py -q
uv run ruff check .
uv run mypy
~~~

tests/local_provider.py là adapter evaluation; tests/eval_local_models.py là
runner và rubric; tests/inspect_local_gguf.py đọc metadata mà không load weights.
Đây không phải provider production và không được dùng làm fallback tự động.
