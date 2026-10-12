"""Panel-facing typed tables on real halted RT-Thread/FreeRTOS fixtures."""

from __future__ import annotations

import json

from tests.support.loader import load_integration_spec


def test_snapshot_tables_preserve_navigation_contract(gdb):
    output = gdb.run("gdr api snapshot --json", timeout=40)
    envelope = next(
        json.loads(line) for line in output.splitlines() if line.startswith("{")
    )
    assert envelope["ok"], envelope
    tasks = envelope["data"]["tasks"]
    if "Used" in tasks["headers"] and "HighWater" in tasks["headers"]:
        used_index = tasks["headers"].index("Used")
        high_index = tasks["headers"].index("HighWater")
        size_index = tasks["headers"].index("Stack")
        for row in tasks["value_rows"]:
            used, high, size = row[used_index], row[high_index], row[size_index]
            if used is not None and high is not None and size is not None:
                assert used <= high <= size, row
    tables = envelope["data"]["tables"]
    rtos = load_integration_spec().rtos
    assert tables
    for table in tables.values():
        assert "KernelTick" not in table["headers"]
        assert "Type" not in table["headers"]
        assert len(table["rows"]) == len(table.get("ids", []))
        for column_index, spec in enumerate(table.get("columns", [])):
            if spec["kind"] == "waiters":
                for row in table["value_rows"]:
                    assert row[column_index] is None or isinstance(
                        row[column_index], list
                    )
    timer = tables.get("timer")
    if timer and timer["rows"]:
        callback = timer["headers"].index("Callback")
        assert timer["columns"][callback]["kind"] == "symbol"
        assert any(
            isinstance(row[callback], int) and row[callback] > 0
            for row in timer["value_rows"]
        )
        assert any(row[callback] is not None for row in timer.get("source_rows", []))
    if rtos == "freertos":
        linked_waiters = []
        for table in tables.values():
            for index, spec in enumerate(table["columns"]):
                if spec["kind"] != "waiters":
                    continue
                for row in table["value_rows"]:
                    for waiter in row[index] or []:
                        assert isinstance(waiter, dict), waiter
                        assert waiter["name"], waiter
                        assert (
                            isinstance(waiter["address"], int) and waiter["address"] > 0
                        ), waiter
                        linked_waiters.append(waiter)
        assert linked_waiters, tables
        assert any(waiter["source"] for waiter in linked_waiters), linked_waiters
        assert "queue" in tables
        assert tables["queue"]["rows"]
        queue = tables["queue"]
        assert any(spec["kind"] == "waiters" for spec in queue["columns"])
        for key in ("Head", "Tail", "WriteTo", "ReadFrom"):
            index = queue["headers"].index(key)
            assert queue["columns"][index]["kind"] == "symbol"
            assert any(
                isinstance(row[index], int) and row[index] > 0
                for row in queue["value_rows"]
            )
        assert "Checks" not in queue["headers"]
        index = queue["headers"].index("Item")
        assert any(row[index] for row in queue["value_rows"])
        for row in queue["value_rows"]:
            for item in row[index] if isinstance(row[index], list) else []:
                assert isinstance(item["address"], int)
                assert item["text"].endswith(hex(item["address"]))
        assert "Checks" not in [
            key for key, _value in envelope["data"]["system"]["pairs"]
        ]
