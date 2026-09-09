# Kế hoạch các phase

[Về mục lục tài liệu](../README.md)

Mỗi JSON giữ scope, work items, acceptance và evidence của phase tương ứng.
Xem trường status trong JSON để biết trạng thái; bảng dưới chỉ là mục lục.

| Phase | Nội dung |
| --- | --- |
| [0 — Project skeleton](phase-0-project-skeleton.json) | Cấu trúc Python/uv và nền tảng dự án |
| [1 — Core agent loop](phase-1-core-agent-loop.json) | Core types, vòng lặp, events và debug |
| [2 — MCP integration](phase-2-mcp-integration.json) | Tool boundary, MCP lifecycle, retry và cancellation |
| [3 — Persistence/lifecycle](phase-3-persistence-lifecycle.json) | SQLite, session/run, recovery và context |
| [4 — RPC/provider/demo](phase-4-rpc-provider-demo.json) | RPC, provider thật và demo hệ thống |
| [4.5 — CLI/Web](phase-4.5-terminal-interface.json) | Giao diện điều khiển và quan sát qua RPC |
| [5 — Test support/integration](phase-5-bonus-test-support.json) | Helper dùng chung, database faults và chuỗi nhiều tool |

Giữ nguyên số phase và các ID acceptance/scenario để truy ngược được nhật ký,
test và báo cáo. Không đổi tên JSON chỉ vì tiêu đề hoặc giao diện của phase thay đổi.
