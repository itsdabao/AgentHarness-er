"""Evaluation-only llama-server adapter; not wired into the application."""

from __future__ import annotations

import json
from collections.abc import Sequence
from time import perf_counter
from typing import Any

import httpx

from minder_harness.core import Message, ModelResponse, ToolCall, ToolDefinition
from minder_harness.core.models import RuntimeError
from minder_harness.core.ports import ModelExecutionContext, ProviderError


def encode_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    encoded = []
    for message in messages:
        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content
            if isinstance(message.content, str) or message.content is None
            else json.dumps(message.content, ensure_ascii=False),
        }
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        encoded.append(item)
    return encoded


def decode_response(data: dict[str, Any]) -> ModelResponse:
    choice = data["choices"][0]
    if choice["finish_reason"] == "length":
        raise ValueError("Output truncated at generation limit")
    message = choice["message"]
    calls = []
    for item in message.get("tool_calls") or []:
        arguments = json.loads(item["function"]["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        calls.append(ToolCall(item["id"], item["function"]["name"], arguments))
    content = message.get("content")
    if not calls and (not isinstance(content, str) or not content.strip()):
        raise ValueError("No final text or tool calls")
    usage = data.get("usage", {})
    # Core treats tool decisions and final answers as mutually exclusive.
    # Accompanying text remains in the evaluation wire log.
    return ModelResponse(
        content=None if calls else content,
        tool_calls=tuple(calls),
        usage={
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    )


class LocalEvalProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        payload = {
            "model": "local-eval",
            "messages": encode_messages(messages),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": 0,
            "seed": 42,
            "max_tokens": 512,
            "stream": False,
            "cache_prompt": False,
        }
        record: dict[str, Any] = {"request": payload}
        self.calls.append(record)
        started = perf_counter()
        try:
            # AgentLoop already bounds this await with its deadline/cancellation.
            response = await self.client.post("/v1/chat/completions", json=payload)
            record["http_status"] = response.status_code
            response.raise_for_status()
            data = response.json()
            record["response"] = data
            return decode_response(data)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            record["error"] = str(exc)
            raise ProviderError(RuntimeError("LOCAL_PROVIDER_ERROR", str(exc))) from exc
        finally:
            record["elapsed_seconds"] = round(perf_counter() - started, 4)
