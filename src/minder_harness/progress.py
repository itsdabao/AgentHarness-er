"""Fixed presentation templates over public events; never LLM-generated loading text."""

from __future__ import annotations

import json
from typing import Any

LABELS = {
    "run_queued": "Đã nhận task, đang chờ chạy.",
    "run_started": "Run bắt đầu.",
    "run_cancel_requested": "Đã yêu cầu dừng, đang chờ xác nhận.",
    "run_cancelled": "Run đã dừng.",
    "run_completed": "Task hoàn tất.",
    "run_failed": "Run thất bại.",
    "run_limit_exceeded": "Run đã hết ngân sách thực thi.",
    "run_interrupted": "Run bị gián đoạn; không tự chạy lại tool.",
}
REASONS = {
    "PROVIDER_RATE_LIMITED": "provider giới hạn tốc độ/quota",
    "PROVIDER_TIMEOUT": "provider quá thời gian chờ",
    "PROVIDER_UNAVAILABLE": "provider tạm không khả dụng",
    "PROVIDER_CONNECTION_LOST": "mất kết nối provider",
    "MCP_CONNECTION_LOST": "mất kết nối MCP",
    "MCP_TIMEOUT": "MCP quá thời gian chờ",
}


def progress_text(event: dict[str, Any]) -> str:
    kind, payload = event["type"], event.get("payload", {})
    if payload.get("retry_reason"):
        reason = REASONS.get(payload["retry_reason"], "lỗi tạm thời đã được phân loại")
        return (
            f"Sẽ thử lại do {reason}; sau {payload.get('retry_delay_ms', 0)} ms, "
            f"lần {payload.get('attempt', 0) + 1}/{payload.get('max_attempts', '?')}."
        )
    if kind in LABELS:
        return LABELS[kind]
    if kind == "model_request_started":
        return (
            "Đang gửi kết quả tool cho model xử lý."
            if payload.get("phase") == "processing_tool_results"
            else "Đang chờ model phản hồi."
        )
    if kind == "model_response_received":
        return "Provider báo lỗi." if payload.get("error") else "Đã nhận phản hồi model."
    name = json.dumps(payload.get("tool_name", "?"), ensure_ascii=False)
    if kind == "tool_call_requested":
        return f"Model yêu cầu gọi {name}."
    if kind == "tool_execution_started":
        return f"Đang thực thi {name}."
    if kind == "tool_execution_completed":
        return f"Đã nhận kết quả {name}."
    if kind == "tool_execution_failed":
        return f"Thực thi {name} không thành công."
    return "Đã ghi nhận event."
