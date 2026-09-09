from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from queue import Queue
from threading import Thread

import httpx
import pytest

from minder_harness.rpc_client import RPCClient


def interactive_cli_cancel(port: int, sid: str, env: dict[str, str]) -> None:
    with tempfile.TemporaryFile() as errors:
        cli = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "minder_harness.cli",
                "--url",
                f"http://127.0.0.1:{port}",
                "--session",
                sid,
                "--json",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert cli.stdin is not None and cli.stdout is not None
        stdout = cli.stdout
        lines: Queue[str] = Queue()

        def read() -> None:
            for line in stdout:
                lines.put(line)
            lines.put("")

        reader = Thread(target=read, daemon=True)
        reader.start()
        try:
            cli.stdin.write("task Check CNC-04 delay_ms=5000\n")
            cli.stdin.flush()
            cancel_sent = False
            while True:
                import json

                line = lines.get(timeout=20)
                assert line, "CLI exited before terminal state"
                record = json.loads(line)
                if record["kind"] == "event":
                    if record["data"]["payload"].get("record_type") == "attempt_intent":
                        if not cancel_sent:
                            cli.stdin.write("run cancel\n")
                            cli.stdin.flush()
                            cancel_sent = True
                if record["kind"] == "terminal":
                    assert cancel_sent and record["data"]["status"] == "cancelled"
                    break
            cli.stdin.write("quit\n")
            cli.stdin.flush()
            assert cli.wait(timeout=10) == 1  # Terminal cancelled, not a successful task.
        finally:
            if cli.poll() is None:
                cli.terminate()
                cli.wait(timeout=5)
            cli.stdin.close()
            reader.join(timeout=2)
            stdout.close()


@pytest.mark.anyio
async def test_socket_server_and_demo_commands(tmp_path: Path) -> None:
    # Bind port zero for a local test port. Server startup is checked before any submission.
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    env = os.environ | {"PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryFile() as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "examples/phase4_offline_server.py",
                "--port",
                str(port),
                "--database",
                str(tmp_path / "socket.db"),
            ],
            stdout=log,
            stderr=log,
            env=env,
        )
        try:
            import anyio

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2) as http:
                with anyio.fail_after(20):
                    while True:
                        assert server.poll() is None, "Offline demo server exited during startup"
                        try:
                            if (await http.get("/health")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await anyio.sleep(0.05)
                client = RPCClient(http)
                sid = (await client.call("create_session"))["session_id"]
                for scenario in ("success", "failure", "cancel"):
                    completed = await anyio.to_thread.run_sync(
                        partial(
                            subprocess.run,
                            [
                                sys.executable,
                                "examples/phase4_demo.py",
                                "--url",
                                f"http://127.0.0.1:{port}",
                                "--session",
                                sid,
                                "--scenario",
                                scenario,
                                "--json",
                            ],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            env=env,
                            timeout=20,
                        )
                    )
                    assert completed.returncode == 0, completed.stderr
                    import json

                    records = [json.loads(line) for line in completed.stdout.splitlines()]
                    expected = "cancelled" if scenario == "cancel" else "completed"
                    assert records[-1]["status"] == expected
                    if scenario == "failure":
                        assert any(r.get("type") == "tool_execution_failed" for r in records)
                completed = await anyio.to_thread.run_sync(
                    partial(
                        subprocess.run,
                        [
                            sys.executable,
                            "-m",
                            "minder_harness.cli",
                            "--url",
                            f"http://127.0.0.1:{port}",
                            "--session",
                            sid,
                            "--json",
                            "--watch",
                            "task",
                            "Check CNC-04",
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=env,
                        timeout=20,
                    )
                )
                assert completed.returncode == 0, completed.stderr
                cli_records = [json.loads(line) for line in completed.stdout.splitlines()]
                assert cli_records[-1]["kind"] == "terminal"
                assert cli_records[-1]["data"]["status"] == "completed"
                await anyio.to_thread.run_sync(partial(interactive_cli_cancel, port, sid, env))
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
