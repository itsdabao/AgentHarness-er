from __future__ import annotations

from time import sleep

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

MACHINES: dict[str, dict[str, str | float]] = {
    "CNC-04": {
        "machine_id": "CNC-04",
        "status": "warning",
        "temperature_c": 78.4,
        "area": "machining",
    },
    "PRESS-02": {
        "machine_id": "PRESS-02",
        "status": "running",
        "temperature_c": 61.2,
        "area": "forming",
    },
}

WORK_ORDERS: dict[str, list[dict[str, str]]] = {
    "machining": [
        {
            "work_order_id": "WO-1042",
            "machine_id": "CNC-04",
            "priority": "high",
            "summary": "Inspect spindle vibration",
        }
    ],
    "forming": [],
}

SAFETY_PROCEDURES: dict[str, dict[str, str | list[str]]] = {
    "LOCKOUT-TAGOUT": {
        "procedure_code": "LOCKOUT-TAGOUT",
        "title": "Lockout tagout",
        "steps": [
            "Notify affected workers.",
            "Shut down and isolate the equipment.",
            "Apply personal locks and tags.",
            "Verify zero-energy state before work begins.",
        ],
    }
}


def build_server() -> MCPServer:
    server = MCPServer(
        name="minder-mock-factory",
        description="Deterministic read-only factory data for the Agent Harness demo.",
    )

    @server.tool(annotations=READ_ONLY)
    def get_machine_status(
        machine_id: str,
        delay_ms: int = 0,
    ) -> dict[str, str | float]:
        """Return the current status for a known machine.

        delay_ms adds deterministic latency for timeout and cancellation demos.
        """
        if delay_ms < 0 or delay_ms > 5_000:
            raise ValueError("delay_ms must be between 0 and 5000")
        if delay_ms:
            sleep(delay_ms / 1_000)
        try:
            return MACHINES[machine_id]
        except KeyError as exc:
            raise ValueError(f"Unknown machine: {machine_id}") from exc

    @server.tool(annotations=READ_ONLY)
    def list_open_work_orders(area: str) -> list[dict[str, str]]:
        """Return deterministic open work orders for a factory area."""
        try:
            return WORK_ORDERS[area]
        except KeyError as exc:
            raise ValueError(f"Unknown area: {area}") from exc

    @server.tool(annotations=READ_ONLY)
    def get_safety_procedure(
        procedure_code: str,
    ) -> dict[str, str | list[str]]:
        """Return an approved safety procedure by code."""
        try:
            return SAFETY_PROCEDURES[procedure_code]
        except KeyError as exc:
            raise ValueError(f"Unknown procedure: {procedure_code}") from exc

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
