# README giới thiệu project độc lập

- Ngày ghi: 09/09/2026
- Trạng thái: đã triển khai
- Nguồn: người dùng yêu cầu README đơn giản, tổng quan; chi tiết liên kết sang docs.

## Lúc bàn

Proposal ban đầu đặt README theo góc nhìn người chấm assessment, với coverage,
demo và evidence. Người dùng đổi hướng: giới thiệu Minder Agent Harness như một
project độc lập, không tổ chức README quanh bài nộp hoặc lịch sử phase.

## Lúc implement

- README còn 63 dòng: mục đích, khả năng, quick start, giới hạn local và link docs.
- Tách phần MCP và persistence trước đây trong README thành hai guide; giữ
  ví dụ sử dụng, retry/cancel, recovery và giới hạn context.
- Quick start phân biệt model giả và Gemini thật; không ghi model giả là live AI.
- Cập nhật mục lục docs và cách giới thiệu trong project brief, architecture.
  File architecture/submission.md giữ tên cũ để bảo toàn link nhưng trình bày
  quyết định thiết kế của project; sửa câu CLI và test helpers còn chưa làm.
- Assessment gốc và nhật ký lịch sử không bị viết lại. Không đổi tên package,
  code runtime, cấu hình, key hoặc dữ liệu.

## Kiểm chứng

- Kiểm tra 39 link file nội bộ trong bảy file Markdown thuộc phạm vi
  kiểm tra, không có đường dẫn bị thiếu. Anchor không nằm trong phép kiểm này.
- git diff --check cho README/docs không báo lỗi whitespace; có cảnh báo
  Git chuyển LF sang CRLF theo cấu hình hiện có.
- Không chạy lại test runtime, server hoặc API provider vì chỉ thay tài liệu.

## Bài học

README là điểm bắt đầu, không phải bản sao của toàn bộ tài liệu kỹ thuật.
Giữ chi tiết trong guide và kết quả có ngày chạy trong report giúp README
ngắn, ít bị lỗi thời và không làm mất lịch sử triển khai.
