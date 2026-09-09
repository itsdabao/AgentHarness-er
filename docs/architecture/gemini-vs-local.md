# Gemini API và local model trong Minder Agent Harness

Ngày đối chiếu: 09/09/2026.

## Kết luận cho project hiện tại

Giữ Gemini là provider mặc định của ứng dụng; dùng local model như một nhánh
evaluation riêng trước khi quyết định tích hợp. Không tự fallback sang local.

Hai file local đã chạy được trên RTX 3050 4 GB. Qwen2.5 Q4 nhanh và nhẹ hơn
Qwen3 Q8 trong các ca đã đo, nhưng cả hai còn sai với nhiều task tổng quát.
Chưa có benchmark Gemini trên cùng bộ task, nên **không có cơ sở nói Gemini
nhanh hơn, chính xác hơn bao nhiêu hoặc model local đã thay thế được Gemini**.

## Khác nhau ở đâu

| Tiêu chí | Gemini API trong ứng dụng | Local trong lượt evaluation |
| --- | --- | --- |
| Nơi chạy model | Dịch vụ Google | Máy người dùng, llama.cpp CUDA |
| Provider adapter | GeminiProvider trong src/ | LocalEvalProvider trong tests/, chưa nối app |
| Giao thức model | Gemini generateContent REST | llama-server chat completions trên loopback |
| Model đang dùng | Chọn bằng GEMINI_MODEL của server | Chỉ định đúng đường dẫn GGUF và SHA256 |
| Phần dùng chung | AgentService, AgentLoop, MCPToolExecutor, SQLite | Các thành phần tương tự, không viết loop mới |
| Cần key/quota | Có | Không cần key Gemini; bị giới hạn tài nguyên máy |
| GPU của người dùng | Không cần cho inference Gemini | Qwen2.5 dùng GPU; Qwen3 chia GPU/CPU |
| Dữ liệu gửi tới model | Request gửi tới provider ngoài máy | Request của runner gửi 127.0.0.1 |
| Khả năng offline | Cần kết nối tới Gemini | Inference có thể chạy từ weights/runtime đã tải; tools vẫn tùy môi trường |
| Chi phí | Phụ thuộc model, gói/quota; không đo trong lượt này | Không có phí Gemini request; vẫn có điện, phần cứng và công vận hành |
| Các lỗi cần xử lý | Credentials, 429, timeout, kết nối, model/settings | Thiếu VRAM/RAM, server chết, template/tool parsing, timeout |
| Retry hiện có | Adapter có policy phân loại lỗi và deadline | Adapter evaluation không retry; chưa phải adapter production |
| Context | Budget và continuation theo Gemini adapter | Cấu hình 4096 token trong server test |
| CLI/Web | Đã nối qua RPC | Chưa nối; evaluation gọi AgentService trực tiếp |

Gemini có rate limits theo project/model/tier, không phải cứ đổi API key là có
quota độc lập. Không hardcode hạn mức hoặc giả định mọi 429 là cùng một nguyên nhân.
[Tài liệu rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)

Cả Gemini function calling và llama-server đều trả quyết định tool để ứng dụng
xử lý. Trong project này, harness mới là nơi kiểm soát thực thi MCP, không giao
cho model quyền gọi tùy ý. [Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling),
[llama.cpp function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)

## Bằng chứng hiện có

| Phép so sánh | Gemini | Qwen2.5 3B Q4 | Qwen3 4B Q8 |
| --- | --- | --- | --- |
| Tám task baseline, chấm cả schema và chuỗi tool | Chưa chạy cùng bộ này | 1/8 đạt | 2/8 đạt |
| Một ca cancel trong evaluation này | Chưa chạy | Đạt, 37 ms local cancellation | Đạt, 24 ms local cancellation |
| Đọc một máy, diagnostic | Chưa đo | Đạt, 2.98 s | Đạt, 7.91 s |
| Ba tool phụ thuộc, task hướng dẫn rõ | Chưa đo | Đạt, 5.19 s | Đạt, 18.84 s |
| VRAM sau diagnostic, toàn GPU | Không đo | Khoảng 2.04 GiB | Khoảng 3.19 GiB |

Nguồn: [report local](../reports/local-model-test-report.md), có raw JSON và giới hạn
phép đo. Diagnostic được thêm sau baseline nên không cộng vào một bảng xếp hạng
khách quan. Hai model khác kích thước, quantization và cách offload; tốc độ này
không tách riêng được tác động của từng yếu tố.

[Tài liệu giao diện](../guides/interfaces.md#kiểm-chứng) ghi nhận smoke backend Gemini
trước đây, nhưng không cung cấp phép đo đối chứng với tập local này. Các test
mock HTTP của Gemini kiểm tra adapter, không chứng minh chất lượng suy luận.
Trong lượt này không đọc .env và không gửi request Gemini.

## Bài học về harness

**Đổi provider không tự sửa được quyết định sai.** Qwen3 có ca trả đúng tên máy
nhưng chưa đọc đủ ứng viên, và ca lấy đúng dữ liệu nhưng sai JSON schema.
Run completed chỉ là trạng thái thực thi; muốn tin output cần kiểm chứng nghiệp vụ.

**Tool calling hỗ trợ ở protocol không có nghĩa luôn đúng về ngữ nghĩa.**
Schema chặn kiểu dữ liệu sai, nhưng một chuỗi như "CNC Area" vẫn có thể hợp lệ
về kiểu string và sai về dữ liệu thực tế. Allowlist cũng không chứng minh model
đã chọn đúng tool, đúng thời điểm hay đúng tham số.

**Task rõ quan hệ dữ liệu giúp ích trong các ca đã thử.** Cả hai model hoàn thành
D_guided khi task nói lấy area từ trạng thái máy rồi lấy procedure_code từ job.
Đây là chỉ dẫn cách làm, không phải đưa đáp án vào prompt. Cần kiểm tra thêm
fixture beta và task chưa xem trước khi biến nó thành policy.

**Local không bỏ được retry/cancel/budget.** Nó chỉ đổi loại lỗi: từ quota và
mạng bên ngoài sang tài nguyên máy và vòng đời server. Không nên bỏ các ranh giới
an toàn vì model ở cùng máy.

## Khi nào chọn cách nào

- Phát triển và gây lỗi có kiểm soát: tiếp tục dùng fake provider trong unit/
  integration tests; nhanh, ổn định và không phụ thuộc model.
- Thử tool calling thật mà không dùng quota: dùng Qwen2.5 Q4 cho task nhỏ,
  có hướng dẫn rõ và kiểm tra output.
- Khám phá model lớn hơn trong bộ file hiện có: Qwen3 Q8 dùng hybrid GPU/CPU;
  chấp nhận chậm hơn trong các ca đã đo, không kỳ vọng tự khắc phục mọi lỗi.
- Chạy ứng dụng hiện tại: giữ Gemini vì adapter và wiring đã có, không phải vì
  đã chứng minh nó thắng local trên tập benchmark này.

## Nếu đưa local vào ứng dụng sau này

1. Tách adapter local đã được kiểm thử dưới LLMProvider; không thay AgentLoop.
2. Cấu hình provider/model/endpoint rõ ở server, mặc định Gemini không đổi.
3. Kiểm tra context/output limits, structured tool-call parsing, lỗi và cancellation.
4. Ghi provider/model vào trace; phân biệt lỗi model với lỗi tool.
5. Không đem opaque Gemini continuation sang local. Cần policy chuyển lịch sử
   và kiểm tra tương thích trước khi đổi provider trong cùng session.
6. Nếu làm fallback, xác định lỗi nào cho phép chuyển và xử lý tool outcome chưa rõ.
   Không chạy lại cả task một cách mù quáng sau khi mất response.

Để so sánh chất lượng công bằng hơn, chạy cùng fixtures và rubric đã đóng băng
trên Gemini với ngân sách được duyệt, nhiều lần/model, lưu settings/usage/latency,
tách điểm schema, dữ liệu và dependency. Không dùng số liệu khác bài test để suy
ra tỷ lệ thắng hoặc chi phí tiết kiệm.
