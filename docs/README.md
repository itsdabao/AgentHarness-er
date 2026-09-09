# Tài liệu Minder Agent Harness

Mục lục trung tâm để đọc dự án và liên kết từ README chính.

## Bắt đầu từ đâu?

| Bạn muốn | Đọc |
| --- | --- |
| Hiểu mục tiêu và phạm vi dự án | [Project brief](requirements/project-brief.md) |
| Hiểu các thành phần và ranh giới | [Kiến trúc chi tiết](architecture/overview.md) |
| Đọc ngắn gọn quyết định thiết kế và giới hạn | [Design decisions](architecture/submission.md) |
| Khởi động và sử dụng CLI/Web | [Hướng dẫn giao diện](guides/interfaces.md) |
| Gọi RPC, cấu hình provider và chạy demo | [Hướng dẫn RPC](guides/rpc.md) |
| Tích hợp tool và quản lý MCP client | [MCP tools và lifecycle](guides/mcp.md) |
| Hiểu lưu trữ, recovery và context | [Persistence và run lifecycle](guides/persistence.md) |
| Xem từng phase cần làm gì | [Danh sách phase](phases/README.md) |
| Chạy test và tìm testcase | [Hướng dẫn kiểm thử](../tests/README.md) |
| Đọc kết quả và giới hạn kiểm chứng | [Report Phase 5](reports/phase-5-test-report.md) |
| Xem hai local model chạy thực tế trên GPU 4 GB | [Report local model](reports/local-model-test-report.md) |
| So sánh Gemini API và local | [Gemini và local model](architecture/gemini-vs-local.md) |
| Chạy lại evaluation bằng GGUF | [Hướng dẫn local evaluation](guides/local-model-evaluation.md) |
| Xem đã bàn gì, triển khai khác ở đâu | [Engineering journal](engineering-journal/README.md) |

## Cấu trúc

```text
docs/
├── README.md                 # Mục lục này
├── requirements/             # Yêu cầu gốc và project brief
│   ├── assessment.docx
│   └── project-brief.md
├── architecture/             # Thiết kế và quyết định về ranh giới
│   ├── overview.md
│   ├── submission.md
│   └── gemini-vs-local.md
├── guides/                   # Cách cấu hình, chạy và sử dụng
│   ├── rpc.md
│   ├── interfaces.md
│   ├── mcp.md
│   ├── persistence.md
│   └── local-model-evaluation.md
├── phases/                   # Kế hoạch, acceptance, trạng thái và evidence
│   ├── README.md
│   └── phase-*.json
├── reports/                  # Báo cáo đọc được, theo từng lần kiểm chứng
│   ├── phase-5-test-report.md
│   ├── local-model-test-report.md
│   └── artifacts/            # Bằng chứng máy đọc được, ví dụ JUnit XML
└── engineering-journal/      # Lịch sử thảo luận và triển khai, theo ngày
    ├── README.md
    ├── TEMPLATE.md
    └── YYYY-MM-DD-*.md
```

Hướng dẫn test chi tiết vẫn ở tests/README.md, gần code test; không sao chép thêm
một bản vào guides/ để tránh hai bản lệch nhau.

## Cách giữ tài liệu nhất quán

- **requirements:** project brief mô tả mục tiêu và giới hạn dự án. Assessment
  giữ lại làm nguồn tham chiếu ban đầu, không phải hướng dẫn sử dụng project.
- **architecture:** mô tả thiết kế và tradeoff. File submission.md giữ tên cũ
  để không phá link; nội dung là bản tóm tắt quyết định thiết kế.
- **guides:** ghi thao tác có thể làm theo. Lệnh shell mặc định chạy từ gốc repo,
  không phải từ thư mục chứa file Markdown.
- **phases:** JSON là nơi giữ scope, acceptance và trạng thái từng phase; mục lục
  không lặp lại trạng thái để tránh thông tin cũ.
- **reports:** ghi rõ ngày chạy, môi trường, kết quả và giới hạn. Raw artifact
  đặt trong artifacts/; dùng tên mới nếu cần giữ nhiều lượt chạy.
- **engineering-journal:** giữ lịch sử và phân biệt đề xuất/đã duyệt/đã làm.
  Không dùng một entry cũ làm kết luận về trạng thái hiện tại.
- Dùng link Markdown tương đối cho điều hướng; đường dẫn trong JSON và lệnh
  shell được tính từ gốc repo. Di chuyển file phải cập nhật cả hai loại.

## Nguồn tham chiếu ban đầu

[Assessment gốc](requirements/assessment.docx) được giữ nguyên nội dung, chỉ đổi tên/vị trí.
Tên gốc: “Celesnity Technical Take-Home Assessment — 2026 (1).docx”.
Việc sắp xếp thư mục không thay đổi nội dung đề, scope, kết quả kiểm chứng hoặc
đồng nghĩa rằng mọi tài liệu lịch sử đã được cập nhật thành mô tả hiện trạng.
