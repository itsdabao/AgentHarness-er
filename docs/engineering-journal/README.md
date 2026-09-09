# Nhật ký thiết kế và triển khai

Lưu lại quá trình bàn phương án, triển khai, kiểm chứng và rút kinh nghiệm của project.
Nhật ký bổ sung cho `docs/phases/`; không thay thế scope hoặc acceptance criteria.

## Cách ghi

- Mỗi chủ đề có một file `YYYY-MM-DD-phase-N-chu-de.md`.
- Phân biệt rõ: **đề xuất**, **đã đồng ý**, **đã implement**, **đã kiểm chứng**, **hoãn**.
- Ghi nguyên nhân và ví dụ cụ thể, không chỉ liệt kê tên file thay đổi.
- Khi implement khác phương án đã bàn, ghi khác ở đâu và vì sao.
- Khi sửa xong, bổ sung kết quả vào chính entry đó; giữ lại phát hiện ban đầu.
- Chỉ ghi commit, ngày triển khai và kết quả test khi có bằng chứng. Nếu tái dựng
  từ cuộc trao đổi, nói rõ nguồn; không suy đoán thời gian hay coi đề xuất là đã duyệt.
- Nhật ký không phải bản sao lưu source code. Git giữ lịch sử code; nhật ký giữ lý do.

## Mục lục

- [Đánh giá local model và so sánh Gemini](2026-09-09-local-model-evaluation.md)
- [README giới thiệu project độc lập](2026-09-09-project-readme.md)
- [Sắp xếp lại tài liệu](2026-09-09-docs-organization.md)

- [Phase 2: retry, deadline, cancellation và trace](2026-09-06-phase-2-review.md)
- [Hướng dài hạn: MCP session lâu dài và async I/O](2026-09-06-mcp-session-lifecycle.md)
- [Mẫu entry mới](TEMPLATE.md)
- [Phase 3: persistence, lifecycle và context](2026-09-07-phase-3-persistence-context.md)
- [Phase 4: RPC, Gemini và tiến trình thực thi](2026-09-07-phase-4-rpc-gemini.md)
- [Phase 4: sửa lệch interface async của loop](2026-09-07-phase-4-loop-contract-fix.md)
- [Phase 4.5: terminal điều khiển và quan sát (kế hoạch)](2026-09-07-phase-4.5-terminal-interface.md)
- [Phase 4.5: mở rộng CLI + Web Simple/Detailed và triển khai](2026-09-08-phase-4.5-cli-web.md)
- [Phase 4.5: sơ đồ pipeline và event inspection](2026-09-08-phase-4.5-pipeline.md)
- [Phase 4.5: danh sách chọn session cũ](2026-09-08-session-picker.md)
- [Web: thời gian gọn hai dòng](2026-09-08-compact-timestamps.md)
- [Phase 5 bonus: test support dùng chung (kế hoạch)](2026-09-07-phase-5-bonus-test-support.md)
