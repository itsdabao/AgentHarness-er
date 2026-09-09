"""Thin JSON-RPC 2.0 transport. Domain failures live in the documented result envelope."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any, Literal

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .core.models import ExecutionLimits, Run
from .core.ports import RunConflict, StorageError
from .service import AgentService
from .web_routes import mount_web

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
ServiceFactory = Callable[[], AbstractAsyncContextManager[AgentService]]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateSession(Params):
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SessionId(Params):
    session_id: Identifier


class Sessions(Params):
    before: int | None = Field(default=None, ge=1, le=2**53 - 1)
    limit: int = Field(default=20, ge=1, le=100)


class RunId(Params):
    run_id: Identifier


class Limits(Params):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_tool_calls: int = Field(default=8, ge=1, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=300, allow_inf_nan=False)


class SubmitTask(SessionId):
    content: str = Field(min_length=1, max_length=16000)
    constraints: list[str] | None = Field(default=None, max_length=32)
    limits: Limits = Field(default_factory=Limits)


class Events(RunId):
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class RPCRequest(Params):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


METHODS: dict[str, type[Params]] = {
    "create_session": CreateSession,
    "get_session": SessionId,
    "list_sessions": Sessions,
    "submit_task": SubmitTask,
    "get_run": RunId,
    "list_run_events": Events,
    "cancel_run": RunId,
}


def public_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            k: public_value(v, secrets)
            for k, v in value.items()
            if not k.startswith("_") and k not in {"provider_data", "thoughtSignature"}
        }
    if isinstance(value, list):
        return [public_value(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def envelope(data: Any = None, *, code: str | None = None, message: str = "") -> dict[str, Any]:
    return {
        "ok": code is None,
        "data": data if code is None else None,
        "error": None
        if code is None
        else {"code": code, "message": message, "retryable": False, "details": {}},
    }


def snapshot(run: Run) -> dict[str, Any]:
    result = run.to_dict()
    result.pop("events", None)
    return result


async def dispatch(service: AgentService, method: str, params: Params) -> Any:
    if method == "list_sessions":
        assert isinstance(params, Sessions)
        return await service.list_sessions(params.before, params.limit)
    if method == "create_session":
        assert isinstance(params, CreateSession)
        session = await service.create_session(params.metadata)
        return {"session_id": session.session_id, "created_at": session.created_at}
    if method == "get_session":
        assert isinstance(params, SessionId)
        session = await service.get_session(params.session_id)
        return {
            "session_id": session.session_id,
            "metadata": session.metadata,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "message_count": len(session.messages),
        }
    if method == "submit_task":
        assert isinstance(params, SubmitTask)
        return snapshot(
            await service.submit_task(
                params.session_id,
                params.content,
                constraints=params.constraints,
                limits=ExecutionLimits(**params.limits.model_dump()),
            )
        )
    assert isinstance(params, RunId)
    if method == "list_run_events":
        assert isinstance(params, Events)
        return await service.list_run_events(params.run_id, params.after_sequence, params.limit)
    if method == "cancel_run":
        return snapshot(await service.cancel_run(params.run_id))
    return snapshot(await service.get_run(params.run_id))


def create_app(factory: ServiceFactory, *, secrets: tuple[str, ...] = ()) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory() as service:
            app.state.service = service
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    mount_web(app)

    @app.get("/health")
    async def health() -> JSONResponse:
        service: AgentService = app.state.service
        ready = service.is_ready
        return JSONResponse(envelope({"ready": ready}), status_code=200 if ready else 503)

    @app.post("/rpc")
    async def rpc(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            return JSONResponse(
                envelope(code="ORIGIN_REJECTED", message="Cross-origin requests are disabled."),
                status_code=403,
            )

        def protocol_error(code: int, message: str, request_id: Any = None) -> JSONResponse:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )

        body = bytearray()
        try:
            with anyio.fail_after(5):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        return JSONResponse(envelope(code="REQUEST_TOO_LARGE"), status_code=413)
            raw = json.loads(body)
            json.dumps(raw, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return protocol_error(-32700, "Parse error")
        except TimeoutError:
            return JSONResponse(envelope(code="REQUEST_TIMEOUT"), status_code=408)
        try:
            call = RPCRequest.model_validate(raw)
        except ValidationError:
            return protocol_error(-32600, "Invalid request; batch calls are not supported")
        notification = "id" not in raw
        if call.method not in METHODS:
            return (
                Response(status_code=204)
                if notification
                else protocol_error(-32601, "Method not found", call.id)
            )
        try:
            params = METHODS[call.method].model_validate(call.params)
        except ValidationError:
            return (
                Response(status_code=204)
                if notification
                else protocol_error(-32602, "Invalid params", call.id)
            )
        try:
            data = await dispatch(app.state.service, call.method, params)
            result = envelope(data)
        except KeyError:
            result = envelope(code="NOT_FOUND", message="Session or run was not found.")
        except RunConflict:
            result = envelope(code="CONFLICT", message="This session already has an active run.")
        except StorageError:
            result = envelope(code="RUNTIME_UNAVAILABLE", message="Runtime cannot record new work.")
        except ValueError:
            result = envelope(code="VALIDATION_ERROR", message="Request violates runtime limits.")
        except Exception:
            result = envelope(
                code="INTERNAL_ERROR", message="The operation could not be completed."
            )
        if notification:
            return Response(status_code=204)
        return JSONResponse(
            public_value({"jsonrpc": "2.0", "id": call.id, "result": result}, secrets)
        )

    return app
