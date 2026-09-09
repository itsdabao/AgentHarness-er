"""One real provider over REST. HTTPX performs no implicit application retries."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from time import monotonic
from typing import Any

import anyio
import httpx

from ..core.cancellation import CancellationToken, OperationCancelled, cancellable
from ..core.models import (
    JSONValue,
    Message,
    ModelResponse,
    RuntimeError,
    ToolDefinition,
    as_jsonable,
)
from ..core.ports import ModelExecutionContext, ProviderError
from .gemini_codec import build_request, failure, parse_response


def retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        if raw is not None:
            try:
                delay = float(raw)
            except ValueError:
                delay = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
            return max(0, delay) if math.isfinite(delay) else None
        for detail in response.json().get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("google.rpc.RetryInfo"):
                delay = float(detail["retryDelay"].removesuffix("s"))
                return max(0, delay) if math.isfinite(delay) else None
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        pass
    return None


def classify_http(response: httpx.Response) -> RuntimeError:
    status = response.status_code
    codes = {
        400: ("PROVIDER_INVALID_REQUEST", "The provider rejected the request."),
        401: ("PROVIDER_AUTHENTICATION", "Provider credentials are invalid."),
        403: ("PROVIDER_PERMISSION", "Provider access was denied."),
        404: ("PROVIDER_MODEL_NOT_FOUND", "The configured model was not found."),
        429: ("PROVIDER_RATE_LIMITED", "Provider quota or rate limit was reached."),
    }
    if status in codes:
        code, message = codes[status]
    elif status >= 500:
        code, message = "PROVIDER_UNAVAILABLE", "The provider is temporarily unavailable."
    else:
        code, message = "PROVIDER_ERROR", "The provider returned an unexpected HTTP status."
    return RuntimeError(
        code, message, status == 429 or 500 <= status < 600, {"http_status": status}
    )


class GeminiProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash",
        max_attempts: int = 2,
        request_timeout: float = 30,
        retry_delay: float = 0.5,
        input_budget: int = 32768,
        output_tokens: int = 2048,
    ) -> None:
        if not api_key or not re.fullmatch(r"[a-zA-Z0-9._-]+", model):
            raise ValueError("Gemini requires a key and a valid model identifier.")
        if max_attempts not in (1, 2) or request_timeout <= 0 or retry_delay < 0:
            raise ValueError("Invalid provider attempt/timeout policy.")
        if input_budget < 1 or output_tokens < 1 or output_tokens >= input_budget:
            raise ValueError("Invalid provider input/output budget.")
        self.client, self._key, self.model = client, api_key, model
        self.max_attempts, self.request_timeout = max_attempts, request_timeout
        self.retry_delay, self.input_budget, self.output_tokens = (
            retry_delay,
            input_budget,
            output_tokens,
        )

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        async def discard(kind: str, payload: dict[str, Any] | None = None) -> None:
            pass

        ctx = context or ModelExecutionContext(
            CancellationToken(), monotonic() + self.request_timeout, discard, 1
        )
        request = build_request(messages, tools, self.model, self.output_tokens)
        # Conservative text-only estimate, explicitly NOT a tokenizer or exact count.
        estimated = (
            len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1024
        )
        if estimated + self.output_tokens > self.input_budget:
            raise failure("CONTEXT_BUDGET_EXCEEDED", "Serialized request exceeds provider budget.")
        identity: dict[str, JSONValue] = {
            "provider": "gemini",
            "model": self.model,
            "step": ctx.step,
            "record_type": "provider_attempt",
            "max_attempts": self.max_attempts,
            "input_estimate": estimated,
            "budget_measure": "utf8_bytes_plus_1024_estimate",
            "output_headroom": self.output_tokens,
        }

        def remaining() -> float:
            return self.request_timeout if ctx.deadline is None else ctx.deadline - monotonic()

        def check() -> None:
            if ctx.cancellation.is_cancelled:
                raise OperationCancelled("Provider operation cancelled.")
            if remaining() <= 0:
                raise TimeoutError("Provider deadline exhausted.")

        for attempt in range(1, self.max_attempts + 1):
            check()
            await ctx.emit(
                "model_request_started", identity | {"attempt": attempt, "phase": "dispatch_intent"}
            )
            check()  # Recording is awaited and consumes the same deadline.
            delay: float | None = self.retry_delay
            response: httpx.Response | None = None
            try:
                response = await cancellable(
                    partial(
                        self.client.post,
                        f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                        headers={"x-goog-api-key": self._key},
                        json=request,
                        timeout=min(self.request_timeout, remaining()),
                    ),
                    min(self.request_timeout, remaining()),
                    ctx.cancellation,
                )
            except (httpx.TimeoutException, TimeoutError):
                error = RuntimeError("PROVIDER_TIMEOUT", "Provider request timed out.", True)
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                error = RuntimeError(
                    "PROVIDER_CONNECTION_LOST", "Provider connection failed.", True
                )
            except OperationCancelled:
                raise
            except Exception:
                error = RuntimeError("PROVIDER_ERROR", "Unexpected provider transport failure.")
            else:
                check()
                if response.is_success:
                    try:
                        # Never retain credential echoes or raw error bodies.
                        data = json.loads(response.text.replace(self._key, "[REDACTED]"))
                        result = parse_response(data, self.model)
                    except (ValueError, ProviderError) as exc:
                        error = (
                            exc.error
                            if isinstance(exc, ProviderError)
                            else RuntimeError(
                                "MALFORMED_MODEL_RESPONSE", "Provider returned invalid JSON."
                            )
                        )
                    else:
                        await ctx.emit(
                            "model_response_received",
                            identity
                            | {
                                "attempt": attempt,
                                "phase": "succeeded",
                                "usage": as_jsonable(result.usage),
                            },
                        )
                        check()
                        return result
                else:
                    error = classify_http(response)
                    if response.status_code == 429:
                        delay = retry_after(response)  # No blind retry for exhausted quota.
            retry = (
                error.retryable
                and attempt < self.max_attempts
                and delay is not None
                and delay < remaining()
                and not ctx.cancellation.is_cancelled
            )
            await ctx.emit(
                "model_response_received",
                identity
                | {
                    "attempt": attempt,
                    "phase": "failed",
                    "error": as_jsonable(error),
                    "retry_scheduled": retry,
                    "retry_reason": error.code if retry else None,
                    "retry_delay_ms": round((delay or 0) * 1000) if retry else None,
                },
            )
            check()
            if not retry:
                raise ProviderError(
                    replace(error, details=error.details | {"attempt_count": attempt})
                )
            assert delay is not None
            if delay >= remaining():
                raise ProviderError(error)
            # Emitted the failure/retry plan BEFORE waiting. SDK/network adds no retries.
            await cancellable(partial(anyio.sleep, delay), remaining(), ctx.cancellation)
        raise AssertionError("unreachable")
