# Sắp xếp lại tài liệu

## Yêu cầu

Gom docs/ theo chức năng, dễ tra cứu và chuẩn bị liên kết từ README chính.

## Đã thực hiện

- requirements/: assessment gốc và project brief.
- architecture/: overview chi tiết và bản submission ngắn.
- guides/: RPC và CLI/Web.
- Giữ phases/ và engineering-journal/ để không làm đổi ID hoặc lịch sử.
- Chuyển raw JUnit XML vào reports/artifacts/, giữ report Markdown tại reports/.
- Thêm docs/README.md và phases/README.md làm mục lục; README chính liên kết
  tới mục lục trung tâm, không viết lại toàn bộ README trong lượt này.
- Cập nhật link tương đối, đường dẫn trong JSON phase và lệnh xuất báo cáo.

## Giới hạn

Không thay đổi runtime, test code, scope phase hoặc nội dung chuyên môn của tài liệu.
Không chạy lại test để thay bằng chứng cũ. Nội dung assessment và XML được kiểm tra
bằng SHA-256 trước/sau di chuyển; không chỉnh sửa hai file này.
Các ghi chú có ngày trong journal vẫn là lịch sử, không tự trở thành trạng thái mới.

## Kiểm chứng

- 64 link local trong docs, README chính và tests/README.md đều trỏ tới đích tồn tại.
- 7 JSON phase parse thành công; các tham chiếu file docs trong JSON tồn tại.
- Không còn đường dẫn cũ trong vùng Markdown/JSON/code đã kiểm tra.
- SHA-256 của assessment DOCX và JUnit XML không đổi sau khi di chuyển.
- Không chạy lại test ứng dụng vì không thay đổi runtime hoặc code test.
