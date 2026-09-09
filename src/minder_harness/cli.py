"""Local RPC console: interactive commands or one-shot JSON operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import unicodedata
from contextlib import suppress
from threading import Event, Thread
from typing import Any

import httpx

from .observation import ACTIVE, accept_page, watch_run
from .progress import progress_text
from .rpc_client import RPCClient, RPCError

HELP = """session new | session use <id>
task <text>                 Submit once and watch (interactive)
run show <id> | run watch <id> [after_sequence]
run cancel [id] | run events <id> [after_sequence]
watch stop | help | quit
Closing this client does not cancel server runs.
One-shot options go before the command: --session ID --watch --json --verbose."""


class CommandError(ValueError):
    """Local validation failed before dispatch."""


def safe_text(value: str, limit: int = 1600) -> str:
    clean = "".join(
        c if c in "\n\t" or not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}"
        for c in value
    )
    return clean if len(clean) <= limit else clean[:limit] + "\n[truncated; use --json]"


class TerminalClient:
    def __init__(
        self,
        rpc: RPCClient,
        *,
        json_mode: bool = False,
        verbose: bool = False,
        session: str | None = None,
        interval: float = 0.5,
    ) -> None:
        self.rpc, self.json_mode, self.verbose = rpc, json_mode, verbose
        self.session, self.interval = session, interval
        self.run_id: str | None = None
        self.cursors: dict[str, int] = {}
        self.watching: asyncio.Task[dict[str, Any] | None] | None = None
        self.last_state: dict[str, Any] | None = None
        self.failed = False

    def display(self, kind: str, data: dict[str, Any]) -> None:
        if kind in {"snapshot", "terminal"}:
            previous = self.last_state
            self.last_state = data
            if kind == "snapshot" and previous == data:
                return
        if self.json_mode:
            print(json.dumps({"kind": kind, "data": data}, ensure_ascii=True), flush=True)
        elif kind == "event":
            if (
                not self.verbose
                and data["payload"].get("record_type") in {"attempt_intent", "attempt_result"}
                and not data["payload"].get("retry_reason")
            ):
                return
            message = progress_text(data)
            if self.verbose:
                message += " " + json.dumps(data["payload"], ensure_ascii=False)
            elif data["type"] == "tool_call_requested":
                message += " " + json.dumps(data["payload"].get("arguments"), ensure_ascii=False)
            print(safe_text(f"{data['sequence']:03d} {message}"), flush=True)
        else:
            print(safe_text(json.dumps(data, ensure_ascii=False, indent=2)), flush=True)

    def error(self, operation: str, exc: Exception) -> None:
        self.failed = True
        # Do not include raw HTTP exceptions (URLs/response bodies may contain secrets).
        uncertain = operation in {"task", "session new", "run cancel"} and not isinstance(
            exc, RPCError
        )
        message = (
            "Outcome unconfirmed if the request reached the server; do not auto-resubmit."
            if uncertain
            else "Observation stopped; last confirmed state/cursor retained."
        )
        if isinstance(exc, RPCError):
            message = "Server rejected the operation; no automatic retry."
        if isinstance(exc, CommandError):
            message = str(exc)
        record = {
            "operation": operation,
            "error": exc.code if isinstance(exc, RPCError) else type(exc).__name__,
            "message": message,
        }
        if self.json_mode:
            print(json.dumps({"kind": "client_error", "data": record}), flush=True)
        else:
            print(safe_text(json.dumps(record)), file=sys.stderr, flush=True)

    async def stop_watch(self) -> None:
        if self.watching is not None:
            self.watching.cancel()
            with suppress(asyncio.CancelledError):
                await self.watching
            self.watching = None

    async def watch(self, run_id: str) -> None:
        await self.stop_watch()
        self.run_id = run_id
        self.last_state = None

        async def observe() -> dict[str, Any] | None:
            try:
                return await watch_run(self.rpc, run_id, self.cursors, self.display, self.interval)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.error("watch", exc)
                return None

        self.watching = asyncio.create_task(observe())

    async def execute(self, line: str, *, interactive: bool = True, watch: bool = False) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise CommandError("Invalid quoting; use help for command syntax.") from exc
        if not parts:
            return True
        if parts == ["quit"]:
            await self.stop_watch()
            return False
        if parts == ["help"]:
            print(HELP, file=sys.stderr)
        elif parts == ["session", "new"]:
            result = await self.rpc.call("create_session")
            await self.stop_watch()
            self.session, self.run_id = result["session_id"], None
            self.display("session", result)
        elif len(parts) == 3 and parts[:2] == ["session", "use"]:
            result = await self.rpc.call("get_session", session_id=parts[2])
            await self.stop_watch()
            self.session, self.run_id = result["session_id"], None
            self.display("session", result)
        elif parts[0] == "task" and len(parts) > 1:
            if self.session is None:
                raise CommandError("Select a session first: session new / session use <id>")
            result = await self.rpc.call(
                "submit_task", session_id=self.session, content=" ".join(parts[1:])
            )
            self.run_id = result["run_id"]
            self.display("submitted", result)
            if interactive or watch:
                await self.watch(self.run_id)
        elif len(parts) in {2, 3} and parts[:2] == ["run", "cancel"]:
            run_id = parts[2] if len(parts) == 3 else self.run_id
            if run_id is None:
                raise CommandError("Select a run first: run watch <id>")
            self.display("cancel_ack", await self.rpc.call("cancel_run", run_id=run_id))
        elif len(parts) == 3 and parts[:2] == ["run", "show"]:
            self.display("run", await self.rpc.call("get_run", run_id=parts[2]))
        elif len(parts) in {3, 4} and parts[:2] == ["run", "watch"]:
            if len(parts) == 4:
                cursor = int(parts[3])
                if cursor < 0:
                    raise CommandError("Cursor must be nonnegative")
                self.cursors[parts[2]] = cursor
            await self.watch(parts[2])
        elif len(parts) in {3, 4} and parts[:2] == ["run", "events"]:
            cursor = int(parts[3]) if len(parts) == 4 else 0
            if cursor < 0:
                raise CommandError("Cursor must be nonnegative")
            # Independent history cursor; never advance a watch cursor.
            while True:
                page = await self.rpc.call(
                    "list_run_events", run_id=parts[2], after_sequence=cursor
                )
                events, cursor = accept_page(page, cursor)
                if self.json_mode:
                    self.display("event_page", page)
                else:
                    for event in events:
                        self.display("event", event)
                if not page["has_more"]:
                    break
                await asyncio.sleep(0)
        elif parts == ["watch", "stop"]:
            await self.stop_watch()
        else:
            raise CommandError("Invalid command; use help")
        return True


async def interactive_input(console: TerminalClient) -> None:
    # A daemon reader avoids blocking async polling and does not hold exit on Windows.
    # This is line-oriented, not a full-screen terminal editor.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()
    closed = Event()

    def read() -> None:
        while not closed.is_set():
            line = sys.stdin.readline()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            except RuntimeError:
                return
            if not line:
                return

    Thread(target=read, daemon=True, name="terminal-input").start()
    try:
        while True:
            print("minder> ", end="", file=sys.stderr, flush=True)
            line = await queue.get()
            if not line:
                break
            try:
                if not await console.execute(line):
                    break
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                words = line.strip().split()
                operation = "task" if words[:1] == ["task"] else " ".join(words[:2])
                console.error(operation, exc)
    finally:
        closed.set()
        await console.stop_watch()


async def run_cli(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.url, timeout=10, follow_redirects=False) as http:
        console = TerminalClient(
            RPCClient(http), json_mode=args.json, verbose=args.verbose, session=args.session
        )
        try:
            if args.command:
                await console.execute(shlex.join(args.command), interactive=False, watch=args.watch)
                if console.watching is not None:
                    await console.watching
            else:
                print("Minder RPC console. Type help. Quit does not cancel runs.", file=sys.stderr)
                await interactive_input(console)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            operation = "task" if args.command[:1] == ["task"] else " ".join(args.command[:2])
            console.error(operation, exc)
        finally:
            await console.stop_watch()
        failed_run = console.last_state is not None and console.last_state[
            "status"
        ] not in ACTIVE | {"completed"}
        return 1 if console.failed or failed_run else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--session")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    try:
        raise SystemExit(asyncio.run(run_cli(parser.parse_args())))
    except KeyboardInterrupt:
        print("Client closed; server runs are not cancelled.", file=sys.stderr)


if __name__ == "__main__":
    main()
