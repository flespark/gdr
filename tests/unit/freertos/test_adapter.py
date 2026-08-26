"""Unit tests for FreeRTOS adapter summaries."""

from __future__ import annotations

import pytest

import freertos.adapter as adapter_module
from freertos.layout import FreeRtosConfig, FreeRtosLayout, build_layout


def test_value_to_task_preserves_the_complete_intermediate_model(monkeypatch):
    """TCB conversion retains RTOS-specific fields even if a table hides them."""
    config = FreeRtosConfig(
        smp=True,
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
    raw = object()
    values = {
        "name": "worker",
        "top_of_stack": 0x1180,
        "stack_base": 0x1000,
        "stack_end": 0x1200,
        "current_priority": 4,
        "base_priority": 3,
        "runtime_counter": 0,
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
    # Stack is readable but never filled with the watermark byte.
    monkeypatch.setattr(
        adapter_module, "read_bytes", lambda _addr, _size: b"\x00" * 512
    )
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

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
        core=0,
        core_affinity=3,
    )


def test_value_to_task_computes_high_water_from_the_stack_bytes(monkeypatch):
    """A filled stack yields a word-counted high-water mark."""
    layout = build_layout(FreeRtosConfig(stack_end_field="pxEndOfStack"), (10, 3, 1))
    raw = object()
    values = {
        "top_of_stack": 0x1180,
        "stack_base": 0x1000,
        "stack_end": 0x1200,
        "current_priority": 2,
    }
    monkeypatch.setattr(
        adapter_module,
        "read_field",
        lambda _value, _layout, field_name: values.get(field_name),
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(adapter_module, "read_cstring", lambda _value: "w")
    monkeypatch.setattr(adapter_module, "value_address", lambda _value: 0x2000)
    monkeypatch.setattr(
        adapter_module, "read_bytes", lambda _addr, _size: b"\xa5" * 128 + b"\x00"
    )
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda: 4)
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

    task = adapter_module.value_to_task(raw, "Ready", None, layout)

    assert task.high_water_mark == 32


def test_high_water_uses_px_top_of_stack_window_when_stack_end_absent(monkeypatch):
    """HighWater must not depend on pxEndOfStack, absent on the default build.

    The fixture's default downward-growing build records no stack-end member
    (configRECORD_STACK_HIGH_ADDRESS is off), so the watermark scan must fall
    back to the [pxStack, pxTopOfStack] window: the deepest historical use is
    <= the current saved SP, so the untouched 0xa5 run always sits inside it.
    A fully-filled window reports its whole word count -- conservative in that
    it never overstates free stack beyond what is visible.
    """
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))  # no stack_end_field
    values = {
        "top_of_stack": 0x1180,
        "stack_base": 0x1000,
        "current_priority": 2,
    }
    monkeypatch.setattr(
        adapter_module,
        "read_field",
        lambda _value, _layout, field_name: values.get(field_name),
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(adapter_module, "read_cstring", lambda _value: "w")
    monkeypatch.setattr(adapter_module, "value_address", lambda _value: 0x2000)
    read_bytes_calls: list[tuple[int, int]] = []

    def spy_read_bytes(addr: int, size: int) -> bytes:
        read_bytes_calls.append((addr, size))
        return b"\xa5" * size  # whole window is untouched fill

    monkeypatch.setattr(adapter_module, "read_bytes", spy_read_bytes)
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda: 4)
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

    task = adapter_module.value_to_task(object(), "Ready", None, layout)

    # Read window is [stack_base, pxTopOfStack] since stack_end is absent.
    assert read_bytes_calls == [(0x1000, 0x180)]
    # Whole window 0xa5 => 0x180 bytes / 4 = 0x60 words, reported conservatively.
    assert task.high_water_mark == 0x60
    # Stack/Used stay gated off (no stack_end); only HighWater becomes available.
    assert task.stack_size is None
    assert task.stack_used is None


# --- high-water mark --------------------------------------------------------


def test_high_water_unavailable_when_stack_not_filled(monkeypatch):
    """A stack whose first byte is not 0xa5 was never watermark-filled."""
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda: 4)

    assert adapter_module._high_water_mark(b"\x00" * 128) is None
    assert adapter_module._high_water_mark(None) is None


def test_high_water_counts_words_when_partially_filled(monkeypatch):
    """Untouched fill bytes are counted and divided by StackType_t size."""
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda: 4)
    stack = b"\xa5" * 32 + b"\x00" * 96

    assert adapter_module._high_water_mark(stack) == 8


# --- task table column gating ------------------------------------------------


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
    assert summary.heap_allocator is None
    assert summary.heap_status is None


def test_object_counts_uses_the_converted_task_snapshot(monkeypatch):
    """object_counts counts through the same conversion path as the summary."""
    traversals = 0

    def converted(_layout):
        nonlocal traversals
        traversals += 1
        yield object()

    monkeypatch.setattr(adapter_module, "iter_converted_tasks", converted)
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    assert adapter.object_counts() == {"task": 1}
    assert traversals == 1


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


@pytest.mark.parametrize(
    ("config", "task_kwargs", "expected_headers"),
    (
        (FreeRtosConfig(), {}, ["Name", "State", "Prio", "SP", "Addr"]),
        (
            FreeRtosConfig(tcb_fields=frozenset({"uxBasePriority"})),
            {},
            ["Name", "State", "Prio", "BasePrio", "SP", "Addr"],
        ),
        (
            FreeRtosConfig(
                stack_end_field="pxEndOfStack",
                tcb_fields=frozenset({"ulRunTimeCounter"}),
            ),
            {},
            ["Name", "State", "Prio", "SP", "Stack", "Used", "Runtime", "Addr"],
        ),
        (
            FreeRtosConfig(
                smp=True,
                number_of_cores=2,
                tcb_fields=frozenset({"uxCoreAffinityMask", "xTaskRunState"}),
            ),
            {},
            ["Name", "State", "Prio", "SP", "CPU", "Affinity", "Addr"],
        ),
        (
            FreeRtosConfig(stack_end_field="pxEndOfStack"),
            {"high_water_mark": 64},
            ["Name", "State", "Prio", "SP", "Stack", "Used", "HighWater", "Addr"],
        ),
        (
            FreeRtosConfig(
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
            ),
            {"high_water_mark": 64},
            [
                "Name",
                "State",
                "Prio",
                "BasePrio",
                "SP",
                "Stack",
                "Used",
                "HighWater",
                "Runtime",
                "CPU",
                "Affinity",
                "Addr",
            ],
        ),
    ),
)
def test_task_table_gates_columns_by_config_combination(
    config, task_kwargs, expected_headers, monkeypatch
):
    """Column presence follows the probed config capability set."""
    layout = build_layout(config, (10, 3, 1))
    task = adapter_module.FreeRtosTask(
        name="t", address=0x10, current_priority=1, **task_kwargs
    )
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter([task])
    )

    table = adapter_module.FreeRtosAdapter(layout).task_table()

    assert table.headers == expected_headers
    assert "Entry" not in table.headers


def test_task_table_reports_unavailable_high_water_cell(monkeypatch):
    """A visible HighWater column renders every unfilled stack as-unavailable."""
    layout = build_layout(FreeRtosConfig(stack_end_field="pxEndOfStack"), (10, 3, 1))
    task = adapter_module.FreeRtosTask(
        name="t",
        address=0x10,
        state="Ready",
        current_priority=1,
        stack_size=256,
        stack_used=32,
        high_water_mark=None,
    )
    filled = adapter_module.FreeRtosTask(
        name="u",
        address=0x20,
        state="Ready",
        current_priority=1,
        stack_size=256,
        high_water_mark=8,
    )
    monkeypatch.setattr(
        adapter_module,
        "iter_converted_tasks",
        lambda _layout: iter([task, filled]),
    )

    table = adapter_module.FreeRtosAdapter(layout).task_table()

    assert "HighWater" in table.headers
    assert table.rows[0] == [
        "t",
        "Ready",
        "1",
        "N/A",
        "256",
        "32",
        "unavailable",
        "0x10",
    ]
    assert table.rows[1][6] == "8"


# --- object detail routing ---------------------------------------------------


def test_object_detail_reports_missing_task(monkeypatch):
    monkeypatch.setattr(adapter_module, "find_task", lambda _name, _layout: None)
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    detail = adapter.object_detail("task", "nope")

    assert detail is not None
    assert detail.found is False


def test_object_detail_task_routes_via_task_state_and_builder(monkeypatch):
    raw = object()
    task = adapter_module.FreeRtosTask(
        name="worker", address=0x2000, state="Running", current_priority=3
    )
    monkeypatch.setattr(adapter_module, "find_task", lambda _name, _layout: raw)
    monkeypatch.setattr(
        adapter_module,
        "task_state",
        lambda _value, _layout: ("Running", 0),
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_task",
        lambda _value, _state, _core, _layout: task,
    )
    monkeypatch.setattr(
        adapter_module,
        "task_detail",
        lambda converted, _layout: [("Name", converted.name)],
    )
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    detail = adapter.object_detail("task", "worker")

    assert detail is not None
    assert detail.pairs == [("Name", "worker")]
    # Non-task kinds are not reliably enumerable yet.
    assert adapter.object_detail("queue", "q") is None


def test_task_table_high_water_header_matches_the_cell_position(monkeypatch):
    """HighWater's header sits where its cell does, with no stack bounds.

    A config with runtime stats but no pxEndOfStack (configRECORD_STACK_HIGH_
    ADDRESS defaults to 0) has no Stack/Used columns, so the HighWater cell is
    emitted right after SP; inserting the header anywhere else would print the
    Runtime value under the HighWater heading.
    """
    config = FreeRtosConfig(tcb_fields=frozenset({"ulRunTimeCounter"}))
    layout = build_layout(config, (10, 3, 1))
    task = adapter_module.FreeRtosTask(
        name="t",
        address=0x10,
        state="Ready",
        current_priority=1,
        top_of_stack=0x1180,
        high_water_mark=105,
        runtime_counter=42,
    )
    monkeypatch.setattr(
        adapter_module, "iter_converted_tasks", lambda _layout: iter([task])
    )

    table = adapter_module.FreeRtosAdapter(layout).task_table()

    assert table.headers == [
        "Name",
        "State",
        "Prio",
        "SP",
        "HighWater",
        "Runtime",
        "Addr",
    ]
    assert dict(zip(table.headers, table.rows[0], strict=True)) == {
        "Name": "t",
        "State": "Ready",
        "Prio": "1",
        "SP": "0x1180",
        "HighWater": "105",
        "Runtime": "42",
        "Addr": "0x10",
    }


def test_value_to_task_skips_the_watermark_scan_on_an_inverted_window(monkeypatch):
    """A pxTopOfStack below pxStack must not reach read_bytes.

    gdb's read_memory converts the length to unsigned, so a negative size
    raises OverflowError -- which read_bytes does not degrade from -- and would
    abort the whole task table instead of leaving one cell unavailable.
    """
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    values = {"top_of_stack": 0x1000, "stack_base": 0x1180, "current_priority": 1}
    monkeypatch.setattr(
        adapter_module,
        "read_field",
        lambda _value, _layout, field_name: values.get(field_name),
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(adapter_module, "read_cstring", lambda _value: "t")
    monkeypatch.setattr(adapter_module, "value_address", lambda _value: 0x2000)
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

    def forbidden(addr, size):
        raise AssertionError(f"read_bytes must not be called with {addr:#x}/{size}")

    monkeypatch.setattr(adapter_module, "read_bytes", forbidden)

    task = adapter_module.value_to_task(object(), "Ready", None, layout)

    assert task.high_water_mark is None
