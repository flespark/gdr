"""Unit tests for FreeRTOS adapter summaries."""

from __future__ import annotations

import freertos.adapter as adapter_module
from freertos.layout import FreeRtosLayout


def test_system_summary_uses_one_scheduler_snapshot(monkeypatch):
    """System rendering converts every task from one scheduler-list traversal."""
    traversals = 0
    source = [
        (object(), "Running", 0),
        (object(), "Blocked", None),
    ]

    def iter_scheduler_tasks(_layout):
        nonlocal traversals
        traversals += 1
        yield from source

    def convert(_value, state, core, _layout):
        return adapter_module.FreeRtosTask(
            name=f"task-{state.lower()}",
            state=state,
            current_priority=2,
            core=core,
        )

    values = {
        "uxCurrentNumberOfTasks": 2,
        "xTickCount": 123,
        "xSchedulerRunning": 1,
    }
    monkeypatch.setattr(adapter_module, "iter_tasks", iter_scheduler_tasks)
    monkeypatch.setattr(adapter_module, "value_to_task", convert)
    monkeypatch.setattr(adapter_module, "list_count", lambda _key, _layout: 0)
    monkeypatch.setattr(adapter_module, "system_value", values.get)
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    summary = adapter.system_summary()

    assert traversals == 1
    assert summary.current_task == "task-running"
    assert summary.task_count == 2
    assert summary.object_counts == {"task": 2}
