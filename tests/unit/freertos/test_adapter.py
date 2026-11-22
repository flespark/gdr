"""Unit tests for FreeRTOS adapter summaries."""

from __future__ import annotations

import pytest

import freertos.adapter as adapter_module
import freertos.navigation as navigation
from core.stack_frame_parser import StackContextSelection
from freertos.backtrace.unwind import ARMV8M_MAINLINE_MPU_INTEGER
from freertos.layout import (
    FreeRtosConfig,
    FreeRtosLayout,
    build_layout,
)


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
        stack_size=0x204,
        stack_used=0x84,
        high_water_mark=None,
        runtime_counter=0,
        core=0,
        core_affinity=3,
    )


@pytest.mark.parametrize("fill_bytes", [128, 472, None])
@pytest.mark.parametrize("word_bytes", [4, 8])
@pytest.mark.parametrize("read_delta", [0, -4])
def test_value_to_task_computes_high_water_from_the_stack_bytes(
    monkeypatch, fill_bytes, word_bytes, read_delta
):
    """Historical fill usage wins unless saved SP proves greater occupancy."""
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
        adapter_module,
        "read_bytes",
        lambda _addr, size: (
            b"\xa5" * (size if fill_bytes is None else fill_bytes)
            + b"\x00" * (0 if fill_bytes is None else size - fill_bytes)
        )[: size + read_delta],
    )
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda _layout: word_bytes)
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

    task = adapter_module.value_to_task(raw, "Ready", None, layout)

    assert task.stack_size == 512 + word_bytes
    assert task.stack_used == 128 + word_bytes
    if read_delta:
        assert task.high_water_mark is None
        return
    assert task.high_water_mark is not None
    assert task.stack_used is not None and task.stack_size is not None
    assert task.stack_used <= task.high_water_mark <= task.stack_size
    if fill_bytes == 128:
        assert task.high_water_mark == 512 + word_bytes - fill_bytes
    else:
        # Partly or entirely untouched reserved slots are still occupied.
        assert task.high_water_mark == task.stack_used


def test_high_water_unavailable_without_recorded_stack_end(monkeypatch):
    """Saved SP is not stack capacity: no maximum-used estimate without bounds."""
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
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda _layout: 4)
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _value, _layout: False)

    task = adapter_module.value_to_task(object(), "Ready", None, layout)

    assert read_bytes_calls == []
    assert task.high_water_mark is None
    # All used-byte measures need the recorded capacity.
    assert task.stack_size is None
    assert task.stack_used is None


# --- high-water mark --------------------------------------------------------


@pytest.mark.parametrize("failure", [None, "bounds", "overflow", "unreadable"])
def test_mpu_stack_usage_decodes_task_sp_separately_from_tcb_context(
    monkeypatch, failure
):
    from tests.unit.freertos.test_backtrace import _Memory

    layout = build_layout(
        FreeRtosConfig(mpu_context=True, stack_end_field="pxEndOfStack")
    )
    memory = _Memory()
    context = ARMV8M_MAINLINE_MPU_INTEGER
    monkeypatch.setattr(
        adapter_module,
        "_loaded_context",
        lambda _layout: StackContextSelection("armv8-m.mainline", "arm", context, "ok"),
    )
    top = 0x3000
    for register, offset in context.registers:
        memory.word(top + offset, 0x100 + register)
    memory.word(top - 24, 0x1235)
    memory.word(top - 20, 0x61000000)
    memory.word(top - 28, 0x222)
    memory.word(top - 16, 0x1200 if failure == "overflow" else 0x1160)
    values = {"top_of_stack": top, "stack_base": 0x1000, "stack_end": 0x11FC}
    monkeypatch.setattr(
        adapter_module, "read_field", lambda _v, _l, key: values.get(key)
    )
    monkeypatch.setattr(adapter_module, "read_int", lambda value: value)
    monkeypatch.setattr(adapter_module, "read_cstring", lambda _v: "mpu")
    monkeypatch.setattr(adapter_module, "value_address", lambda _v: 0x3100)
    monkeypatch.setattr(adapter_module, "arch_or_default", lambda: (4, "little"))
    monkeypatch.setattr(
        adapter_module,
        "saved_context_bounds",
        lambda _v, _l: (None, None) if failure == "bounds" else (top - 80, top + 4),
    )
    monkeypatch.setattr(adapter_module, "is_idle_task", lambda _v, _l: False)
    reads = []

    def read(address, size):
        reads.append((address, size))
        if address == 0x1000:
            return b"\xa5" * 0x100 + b"\x00" * (size - 0x100)
        return None if failure == "unreadable" else memory(address, size)

    monkeypatch.setattr(adapter_module, "read_bytes", read)
    task = adapter_module.value_to_task(object(), "Blocked", None, layout)
    assert task.top_of_stack == top and task.stack_size == 512
    assert task.stack_used == (128 if failure is None else None)
    assert task.high_water_mark == 256
    assert all(
        address == 0x1000 or top - 80 <= address < top + 4 for address, _size in reads
    )
    if failure == "bounds":
        assert reads == [(0x1000, 512)]


def test_high_water_unavailable_when_stack_not_filled(monkeypatch):
    """A stack whose first byte is not 0xa5 was never watermark-filled."""
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda _layout: 4)

    assert adapter_module._high_water_mark(b"\x00" * 128, 4) is None
    assert adapter_module._high_water_mark(None, 4) is None


def test_high_water_counts_words_when_partially_filled(monkeypatch):
    """Untouched fill bytes are counted and divided by StackType_t size."""
    monkeypatch.setattr(adapter_module, "_stack_type_size", lambda _layout: 4)
    stack = b"\xa5" * 32 + b"\x00" * 96

    assert adapter_module._high_water_mark(stack, 4) == 8


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
        "task_count": 2,
        "tick": 123,
        "scheduler_running": 1,
    }
    monkeypatch.setattr(adapter_module, "iter_tasks", iter_scheduler_tasks)
    monkeypatch.setattr(adapter_module, "value_to_task", convert)
    monkeypatch.setattr(adapter_module, "list_count", lambda _key, _layout: 0)
    monkeypatch.setattr(
        adapter_module, "system_value", lambda key, _layout: values.get(key)
    )
    # Heap snapshot and system checks read scheduler globals / dwarf types
    # that cannot run outside GDB; they are exercised by test_diagnostics
    # and test_heap, so this scheduler-focus test stubs them.
    monkeypatch.setattr(
        adapter_module.heap_module,
        "heap_snapshot",
        lambda _layout: (_ for _ in ()).throw(ValueError("no gdb")),
    )
    monkeypatch.setattr(adapter_module, "system_checks", lambda _layout: [])
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    summary = adapter.system_summary()

    assert traversals == 1
    assert summary.current_task == "task-running"
    assert summary.task_count == 2
    assert summary.task_count_label == "Task number (kernel)"
    assert summary.object_counts == {"task (walked)": 2}
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
    # Reason: outside GDB the kind probes cannot look up DWARF types; the
    # task row is the contract under test here, other kinds are covered by
    # the discovery unit tests.
    monkeypatch.setattr(adapter_module, "_kind_enabled", lambda _kind, _layout: False)
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
        "PC",
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
            "N/A",
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

    assert table.headers == ["Name", "State", "Prio", "PC", "SP", "Addr"]


@pytest.mark.parametrize(
    ("config", "task_kwargs", "expected_headers"),
    (
        (FreeRtosConfig(), {}, ["Name", "State", "Prio", "PC", "SP", "Addr"]),
        (
            FreeRtosConfig(tcb_fields=frozenset({"uxBasePriority"})),
            {},
            ["Name", "State", "Prio", "BasePrio", "PC", "SP", "Addr"],
        ),
        (
            FreeRtosConfig(
                stack_end_field="pxEndOfStack",
                tcb_fields=frozenset({"ulRunTimeCounter"}),
            ),
            {},
            ["Name", "State", "Prio", "PC", "SP", "Stack", "Used", "Runtime", "Addr"],
        ),
        (
            FreeRtosConfig(
                smp=True,
                number_of_cores=2,
                tcb_fields=frozenset({"uxCoreAffinityMask", "xTaskRunState"}),
            ),
            {},
            ["Name", "State", "Prio", "PC", "SP", "CPU", "Affinity", "Addr"],
        ),
        (
            FreeRtosConfig(stack_end_field="pxEndOfStack"),
            {"high_water_mark": 64},
            [
                "Name",
                "State",
                "Prio",
                "PC",
                "SP",
                "Stack",
                "Used",
                "HighWater",
                "Addr",
            ],
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
                "PC",
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
        "N/A",
        "256",
        "32",
        "N/A",
        "0x10",
    ]
    assert table.rows[1][7] == "8"


# --- object detail routing ---------------------------------------------------


def test_object_detail_reports_missing_task(monkeypatch):
    monkeypatch.setattr(adapter_module, "find_task", lambda _name, _layout: None)
    monkeypatch.setattr(adapter_module, "resolve_object", lambda *_args: None)
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
        lambda converted, _layout, **_kwargs: [("Name", converted.name)],
    )
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    detail = adapter.object_detail("task", "worker")

    assert detail is not None
    assert detail.pairs == [("Name", "worker")]
    # Event groups and stream buffers route through the discovery channels
    # like the queue family: an unknown name reports not-found instead of
    # bubbling the old "not enumerable at this depth" None.
    monkeypatch.setattr(
        adapter_module.FreeRtosAdapter,
        "_find_named_object",
        lambda _self, _kind, _name: (None, None),
    )
    eg = adapter.object_detail("eventgroup", "q")
    assert eg is not None and eg.found is False
    sb = adapter.object_detail("streambuffer", "q")
    assert sb is not None and sb.found is False


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
        "PC",
        "SP",
        "HighWater",
        "Runtime",
        "Addr",
    ]
    assert dict(zip(table.headers, table.rows[0], strict=True)) == {
        "Name": "t",
        "State": "Ready",
        "Prio": "1",
        "PC": "N/A",
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


# Global scheduler-list addresses used by the fake symbol table.
PENDING = 0x0100
DELAYED = 0x0200
OVERFLOW = 0x0300
SUSPENDED = 0x0400
TERMINATION = 0x0500
CURRENT_TCB = 0x0600


class _Addr:
    def __init__(self, value: int):
        self.value = value

    def __int__(self) -> int:
        return self.value


class _Target:
    """A list/TCB value: ``.address`` feeds value_address, ``int()`` the pointer."""

    def __init__(self, value: int):
        self._value = value
        self.address = _Addr(value)

    def __int__(self) -> int:
        return self._value


class _Ptr:
    """A pointer to a _Target; dereference() yields the pointee."""

    def __init__(self, target: _Target):
        self.target = target

    def __int__(self) -> int:
        return int(self.target)

    def dereference(self) -> _Target:
        return self.target


class _Array:
    """Indexable fake for pxCurrentTCBs (list of _Ptr)."""

    def __init__(self, pointers: list[_Ptr]):
        self.pointers = pointers

    def __getitem__(self, index: int) -> _Ptr:
        return self.pointers[index]


class _Slots:
    """Indexable fake for the ucNotifyState array member."""

    def __init__(self, values: list[int]):
        self.values = values

    def __getitem__(self, index: int) -> int:
        return self.values[index]


def _task_state(
    monkeypatch,
    *,
    smp: bool = False,
    cores: int = 2,
    tcb_addr: int = CURRENT_TCB,
    current: int | None = None,
    state_container: int | None = None,
    event_container: int | None = None,
    run_state: int | None = None,
    notify_state: _Slots | int | None = None,
    notification_array: bool = False,
    current_tcbs: _Array | None = None,
):
    """Drive task_state() against a controlled TCB/list world."""
    config = FreeRtosConfig(
        smp=smp,
        number_of_cores=cores,
        notifications=True,
        # Reason: the notification member is a scalar before V10.4.0 and an
        # array from V10.4.0 on; the fake must carry the shape the config
        # declares or the scalar branch never gets exercised.
        notification_array=notification_array,
        notification_count=(
            len(notify_state.values)
            if notification_array and isinstance(notify_state, _Slots)
            else 1
        ),
        tcb_fields=frozenset({"xTaskRunState"} if smp else ()),
    )
    layout = build_layout(config, (10, 5, 1))
    tcb = _Target(tcb_addr)
    state_item = object()
    event_item = object()

    def read_field(value, _struct_layout, field_name):
        if value is tcb:
            return {
                "state_list_item": state_item,
                "event_list_item": event_item,
                "run_state": run_state,
                "notify_state": notify_state,
            }.get(field_name)
        if value is state_item and field_name == "container":
            return state_container
        if value is event_item and field_name == "container":
            return event_container
        return None

    def lookup_symbol(name):
        plain = {
            "xPendingReadyList": PENDING,
            "xSuspendedTaskList": SUSPENDED,
            "xTasksWaitingTermination": TERMINATION,
        }
        if name in plain:
            return _Target(plain[name])
        if name == "pxDelayedTaskList":
            return _Ptr(_Target(DELAYED))
        if name == "pxOverflowDelayedTaskList":
            return _Ptr(_Target(OVERFLOW))
        if name == "pxCurrentTCB":
            return _Ptr(_Target(current)) if current is not None else None
        if name == "pxCurrentTCBs":
            return current_tcbs
        return None

    monkeypatch.setattr(navigation, "read_field", read_field)
    monkeypatch.setattr(navigation, "lookup_symbol", lookup_symbol)
    return navigation.task_state(tcb, layout)


# --- the six main branches ---------------------------------------------------


def test_single_core_current_tcb_is_running(monkeypatch):
    """The task behind pxCurrentTCB reports Running on core 0."""
    assert _task_state(monkeypatch, current=CURRENT_TCB) == ("Running", 0)


def test_event_list_on_pending_ready_is_ready(monkeypatch):
    """xEventListItem on xPendingReadyList wins over any state-list position."""
    assert _task_state(monkeypatch, event_container=PENDING) == ("Ready", None)


def test_state_list_on_delayed_is_blocked(monkeypatch):
    """The active delayed list classifies the task as Blocked."""
    assert _task_state(monkeypatch, state_container=DELAYED) == ("Blocked", None)


def test_state_list_on_overflow_delayed_is_blocked(monkeypatch):
    """The overflow delayed list also classifies the task as Blocked."""
    assert _task_state(monkeypatch, state_container=OVERFLOW) == ("Blocked", None)


def test_termination_list_is_deleted(monkeypatch):
    """xTasksWaitingTermination yields Deleted."""
    assert _task_state(monkeypatch, state_container=TERMINATION) == (
        "Deleted",
        None,
    )


def test_state_list_null_is_deleted(monkeypatch):
    """A NULL state-list container means the task object is gone (eDeleted)."""
    # Reason: a real NULL container is a pointer value of 0, distinct from an
    # unreadable container (None). The fake maps container=0 to safe_int=0.
    assert _task_state(monkeypatch, state_container=0) == ("Deleted", None)


def test_unreadable_container_is_not_reported_deleted(monkeypatch):
    """An unreadable container is unknown, never a definite Deleted.

    If the DWARF spelling of the container member is wrong (e.g. the layout
    probes pxContainer while the build emits pvContainer), reads return None.
    Rendering that as Deleted would label every non-running task as deleted;
    it must instead fall through to the default state instead of inventing a
    confident wrong answer.
    """
    # container=None models the field being absent/unreadable from the value.
    assert _task_state(monkeypatch, state_container=None) == ("Ready", None)


# --- suspended-list three-way split ------------------------------------------


def test_suspended_with_event_container_is_blocked(monkeypatch):
    """On xSuspendedTaskList with an event item => Blocked (IPC wait)."""
    assert _task_state(
        monkeypatch, state_container=SUSPENDED, event_container=0x10
    ) == ("Blocked", None)


def test_suspended_no_event_notify_waiting_is_blocked(monkeypatch):
    """Suspended with a pending notification wait is Blocked, not Suspended."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=_Slots([0, 1]),
        notification_array=True,
    ) == ("Blocked", None)


def test_suspended_scalar_notify_waiting_is_blocked(monkeypatch):
    """Pre-V10.4.0 kernels keep ucNotifyState as a scalar member.

    The 10.3.1 `base` fixture is exactly this shape, so subscripting
    the member would raise and silently downgrade Blocked to Suspended.
    """
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=1,
    ) == ("Blocked", None)


def test_suspended_scalar_notify_idle_is_suspended(monkeypatch):
    """A scalar ucNotifyState of NOT_WAITING leaves the task Suspended."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=0,
    ) == ("Suspended", None)


def test_suspended_no_event_no_notify_is_suspended(monkeypatch):
    """Fully suspended: no event item and nothing waiting on a notification."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=_Slots([0, 0]),
        notification_array=True,
    ) == ("Suspended", None)


# --- SMP run state -----------------------------------------------------------


def test_smp_run_state_0_is_running(monkeypatch):
    """A valid core index in xTaskRunState means Running on that core."""
    assert _task_state(monkeypatch, smp=True, run_state=0) == ("Running", 0)


def test_smp_run_state_minus2_is_running_yielding(monkeypatch):
    """xTaskRunState == -2 (scheduled to yield) still occupies core 1."""
    tcbs = _Array([_Ptr(_Target(0x100)), _Ptr(_Target(CURRENT_TCB))])
    assert _task_state(monkeypatch, smp=True, run_state=-2, current_tcbs=tcbs) == (
        "Running(yielding)",
        1,
    )


def test_smp_run_state_minus1_on_ready_list_is_ready(monkeypatch):
    """xTaskRunState == -1 with no other match falls through to Ready."""
    assert _task_state(monkeypatch, smp=True, run_state=-1, state_container=0x777) == (
        "Ready",
        None,
    )


def test_smp_run_state_minus2_without_core_falls_through_to_blocked(monkeypatch):
    """-2 with no pxCurrentTCBs match must not invent Ready.

    The task still occupies a delayed list, so Step 3 reports Blocked.
    """
    assert _task_state(
        monkeypatch,
        smp=True,
        run_state=-2,
        current_tcbs=_Array([]),
        state_container=DELAYED,
    ) == ("Blocked", None)


def test_smp_run_state_minus2_without_core_defaults_to_ready(monkeypatch):
    """-2 with no core and no other list match falls through to Ready."""
    assert _task_state(
        monkeypatch,
        smp=True,
        run_state=-2,
        current_tcbs=_Array([]),
        state_container=0x777,
    ) == ("Ready", None)


# --- core_of back-reference --------------------------------------------------


def test_core_of_yielding_task_scans_px_current_tcbs(monkeypatch):
    """core_of() resolves a -2 run state against the per-core pointers."""
    tcbs = _Array([_Ptr(_Target(0x100)), _Ptr(_Target(CURRENT_TCB))])
    config = FreeRtosConfig(
        smp=True,
        number_of_cores=2,
        tcb_fields=frozenset({"xTaskRunState"}),
    )
    layout = build_layout(config, (10, 5, 1))
    tcb = _Target(CURRENT_TCB)

    def lookup_symbol(name):
        if name == "pxCurrentTCBs":
            return tcbs
        return None

    monkeypatch.setattr(navigation, "lookup_symbol", lookup_symbol)
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda value, _sl, field: (
            (-2 if field == "run_state" else None) if value is tcb else None
        ),
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.core_of(tcb, layout) == 1


def test_core_of_returns_none_outside_smp():
    """Single-core targets never report a core index."""
    config = FreeRtosConfig(number_of_cores=1)
    layout = build_layout(config, (10, 5, 1))
    assert navigation.core_of(_Target(CURRENT_TCB), layout) is None
