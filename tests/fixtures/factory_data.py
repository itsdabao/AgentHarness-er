"""Isolated demo data. No production configuration or credentials."""

from typing import Any


def dataset(variant: str) -> dict[str, Any]:
    area = f"machining-{variant}"
    machines: dict[str, Any] = {}
    orders: dict[str, Any] = {area: [], "forming": []}
    procedures: dict[str, Any] = {}
    for number, (name, temperature, status, priority, location) in enumerate(
        [
            ("CNC-04", 78.4, "warning", "high", area),
            (f"M-{variant}-2", 92.0, "warning", "critical", area),
            (f"M-{variant}-3", 99.0, "running", "normal", area),
            ("PRESS-02", 61.2, "running", "high", "forming"),
        ],
        start=1,
    ):
        code = f"PROC-{variant}-{number}"
        machines[name] = {
            "machine_id": name,
            "temperature_c": temperature,
            "status": status,
            "area": location,
        }
        orders[location].append(
            {
                "work_order_id": f"WO-{variant}-{number}",
                "machine_id": name,
                "priority": priority,
                "summary": f"Inspect {name}",
                "procedure_code": code,
            }
        )
        procedures[code] = {
            "procedure_code": code,
            "title": f"Fixture procedure for {name}",
            "steps": ["Demonstration data only; not operational safety guidance."],
        }
    orders[area].insert(
        0,
        {
            "work_order_id": "distractor",
            "machine_id": "CNC-04",
            "priority": "low",
            "summary": "Not the selected job",
            "procedure_code": "DO-NOT-SELECT",
        },
    )
    return {"area": area, "machines": machines, "orders": orders, "procedures": procedures}
