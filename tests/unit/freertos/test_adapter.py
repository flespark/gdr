"""Unit tests for FreeRTOS adapter summaries."""

from __future__ import annotations

import freertos.adapter as adapter_module
from freertos.layout import FreeRtosConfig, FreeRtosLayout, build_layout


def test_value_to_task_preserves_the_complete_intermediate_model(monkeypatch):
    """TCB conversion retains RTOS-specific fields even if a table hides them."""
    config = FreeRtosConfig(
        smp=True,
        stack_end_field="pxEndOfStack",
        entry_field="pxTaskTag",
        tcb_fields=frozenset(
            {
                "uxBasePriority",
                "ulRunTimeCounter",
                "xTaskRunState",
                "uxCoreAffinityMask",
            }
        ),
    )
    layout = build_layout(config, (10, 5, 1))
    raw = object()
    values = {
        "name": "worker",
        "top_of_stack": 0x1180,
        "stack_base": 0x1000,
        "stack_end": 0x1200,
        "current_priority": 4,
        "base_priority": 3,
        "runtime_counter": 0,
        "entry": 0x5000,
        "core_affinity": 3,
    }
    monkeypatch.setattr(
        adapter_module,
        "read_field",
        lambda _value, _layout, field_name: values.get(field_name),
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(adapter_module, "read_cstring", lambda value: value)
    monkeypatch.setattr(adapter_module, "value_address", lambda _value: 0x2000)

    task = adapter_module.value_to_task(raw, "Running", 0, layout)

    assert task == adapter_module.FreeRtosTask(
        name="worker",
        address=0x2000,
        state="Running",
        current_priority=4,
        base_priority=3,
        top_of_stack=0x1180,
        stack_base=0x1000,
        stack_end=0x1200,
        stack_size=0x200,
        stack_used=0x80,
        high_water_mark=None,
        runtime_counter=0,
        entry=0x5000,
        core=0,
        core_affinity=3,
    )


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


def test_task_table_uses_freertos_capability_columns(monkeypatch):
    """FreeRTOS owns its task columns and preserves runtime/SMP information."""
    config = FreeRtosConfig(
        smp=True,
        number_of_cores=2,
        stack_end_field="pxEndOfStack",
        tcb_fields=frozenset(
            {
                "uxBasePriority",
                "ulRunTimeCounter",
                "xTaskRunState",
                "uxCoreAffinityMask",
            }
        ),
    )
    layout = build_layout(config, (10, 5, 1))
    task = adapter_module.FreeRtosTask(
        name="worker",
        address=0x2000,
        state="Running",
        current_priority=4,
        base_priority=3,
        top_of_stack=0x3000,
        stack_size=512,
        stack_used=64,
        runtime_counter=0,
        core=0,
        core_affinity=3,
    )
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter([task])
    )

    table = adapter_module.FreeRtosAdapter(layout).task_table()

    assert table.headers == [
        "Name",
        "State",
        "Prio",
        "BasePrio",
        "SP",
        "Stack",
        "Used",
        "Runtime",
        "CPU",
        "Affinity",
        "Addr",
    ]
    assert table.rows == [
        [
            "worker *",
            "Running",
            "4",
            "3",
            "0x3000",
            "512",
            "64",
            "0",
            "0",
            "3",
            "0x2000",
        ]
    ]
    assert "Entry" not in table.headers
    assert "HighWater" not in table.headers


def test_task_table_hides_unavailable_capability_columns(monkeypatch):
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter([])
    )

    table = adapter_module.FreeRtosAdapter(layout).task_table()

    assert table.headers == ["Name", "State", "Prio", "SP", "Addr"]
