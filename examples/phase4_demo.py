"""RPC demo; server uses real Gemini. Fake-server mode is explicitly labeled separately."""

from __future__ import annotations

import argparse
import json
from time import monotonic

import anyio
import httpx

from minder_harness.progress import progress_text
from minder_harness.rpc_client import RPCClient


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--session")
    parser.add_argument("--run", help="Inspect a known run without submitting another task")
    parser.add_argument("--scenario", choices=["success", "failure", "cancel"], default="success")
    parser.add_argument("--json", action="store_true", help="Print only JSON records")
    args = parser.parse_args()
    prompts = {
        "success": "Call get_machine_status for CNC-04, then summarize its status.",
        "failure": "Call get_machine_status for machine UNKNOWN. Report the tool error honestly.",
        "cancel": "Call get_machine_status for CNC-04 with delay_ms=5000, then summarize.",
    }
    async with httpx.AsyncClient(base_url=args.url, timeout=10) as http:
        client = RPCClient(http)
        if args.run:
            run_id = args.run
        else:
            session = args.session or (await client.call("create_session"))["session_id"]
            # No auto-resubmission on timeout: the server may have accepted this task.
            run = await client.call(
                "submit_task", session_id=session, content=prompts[args.scenario]
            )
            run_id = run["run_id"]
            print(json.dumps({"session_id": session, "run_id": run_id, "status": run["status"]}))
        cursor = 0
        cancel_sent = False
        started = monotonic()
        with anyio.fail_after(180):
            while True:
                state = await client.call("get_run", run_id=run_id)
                terminal = state["status"] not in {"queued", "running", "cancel_requested"}
                while True:
                    page = await client.call(
                        "list_run_events", run_id=run_id, after_sequence=cursor
                    )
                    for event in page["events"]:
                        print(
                            json.dumps(event, ensure_ascii=True)
                            if args.json
                            else f"{event['sequence']:03d} {progress_text(event)}"
                        )
                        if (
                            args.scenario == "cancel"
                            and not args.run
                            and not cancel_sent
                            and event["type"] == "tool_execution_started"
                        ):
                            result = await client.call("cancel_run", run_id=run_id)
                            print(json.dumps({"cancel_ack": result["status"]}))
                            cancel_sent = True
                    cursor = page["next_after_sequence"]
                    if not page["has_more"]:
                        break
                if terminal:
                    print(json.dumps(state, ensure_ascii=True))
                    break
                # If the model never calls a tool, still demonstrate in-flight cancellation.
                if args.scenario == "cancel" and not args.run and not cancel_sent:
                    if monotonic() - started > 3:
                        result = await client.call("cancel_run", run_id=run_id)
                        print(json.dumps({"cancel_ack": result["status"]}))
                        cancel_sent = True
                await anyio.sleep(0.5)


if __name__ == "__main__":
    anyio.run(main)
