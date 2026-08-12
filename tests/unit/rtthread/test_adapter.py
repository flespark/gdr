"""Unit tests for RT-Thread adapter summaries and tables."""

from __future__ import annotations

import pytest

import rtthread.adapter as adapter_module
from gdr.abstractions import Event, Mailbox, MemoryPool, MessageQueue, Timer
from gdr.adapter_api import TaskSummary
from gdr.layout import KernelLayout, StructLayout


def test_task_summaries_read_current_task_once(monkeypatch):
    """RT-Thread does not re-read the current task for every rendered row."""
    current_reads = 0
    converted: list[tuple[object, int]] = []
    values = [object(), object(), object()]
    adapter = adapter_module.RtThreadAdapter(KernelLayout())

    def current_task():
        nonlocal current_reads
        current_reads += 1
        return object()

    def summarize(value, current_address):
        converted.append((value, current_address))
        return TaskSummary(name=f"task-{len(converted)}")

    monkeypatch.setattr(adapter_module, "get_current_thread", current_task)
    monkeypatch.setattr(adapter_module, "_get_addr", lambda _value: 0x1234)
    monkeypatch.setattr(adapter_module, "iter_threads", lambda _layout: iter(values))
    monkeypatch.setattr(adapter, "_summarize_task", summarize)

    summaries = list(adapter.iter_task_summaries())

    assert current_reads == 1
    assert [summary.name for summary in summaries] == ["task-1", "task-2", "task-3"]
    assert converted == [(value, 0x1234) for value in values]


def test_timer_table_symbolizes_and_falls_back_to_addresses(monkeypatch):
    """Timer callbacks preserve symbol, address, and null boundary behavior."""
    timers = [
        Timer(name="symbolized", callback=0x1000),
        Timer(name="unknown", callback=0x2000),
        Timer(name="null", callback=0),
    ]
    looked_up: list[int] = []
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "iter_timers", lambda _layout: iter(timers))
    monkeypatch.setattr(adapter_module, "value_to_timer", lambda value, _layout: value)
    monkeypatch.setattr(adapter_module, "get_tick", lambda: 123)
    monkeypatch.setattr(
        adapter_module,
        "lookup_symbol_at",
        lambda address: (
            looked_up.append(address)
            or ("test_timer_timeout+0" if address == 0x1000 else None)
        ),
    )

    table = adapter.object_table("timer")

    assert table is not None
    assert [row[-1] for row in table.rows] == [
        "<test_timer_timeout+0>",
        "0x2000",
        "0x0",
    ]
    assert table.messages == ["Kernel tick: 123"]
    assert looked_up == [0x1000, 0x2000]


@pytest.mark.parametrize(
    ("kind", "struct_name", "converted", "headers", "expected_row"),
    (
        (
            "event",
            "struct rt_event",
            Event(name="ready", set=0x3, address=0x1000),
            ["Name", "Set", "Addr"],
            ["ready", "0x3", "0x1000"],
        ),
        (
            "mailbox",
            "struct rt_mailbox",
            Mailbox(
                name="input",
                entry=2,
                size=8,
                in_offset=3,
                out_offset=1,
                address=0x2000,
            ),
            ["Name", "Entry", "Size", "In", "Out", "Addr"],
            ["input", "2", "8", "3", "1", "0x2000"],
        ),
        (
            "msgqueue",
            "struct rt_messagequeue",
            MessageQueue(name="work", entry=3, msg_size=16, max_msgs=8, address=0x3000),
            ["Name", "Entry", "MsgSize", "MaxMsgs", "Addr"],
            ["work", "3", "16", "8", "0x3000"],
        ),
        (
            "mempool",
            "struct rt_mempool",
            MemoryPool(
                name="blocks",
                block_size=32,
                block_total_count=10,
                block_free_count=4,
                address=0x4000,
            ),
            ["Name", "BlockSize", "Total", "Free", "Addr"],
            ["blocks", "32", "10", "4", "0x4000"],
        ),
    ),
)
def test_ipc_object_tables_use_their_own_registry_route(
    kind, struct_name, converted, headers, expected_row, monkeypatch
):
    """Each IPC command converts only its own registered object type."""
    value = object()
    type_code = 7
    layout = KernelLayout(
        structs={struct_name: StructLayout(struct_name)},
        object_codes={kind: type_code},
    )
    calls: list[tuple[int, KernelLayout]] = []
    monkeypatch.setattr(
        adapter_module,
        "iter_objects",
        lambda code, selected: calls.append((code, selected)) or iter((value,)),
    )
    converter_name = {
        "event": "value_to_event",
        "mailbox": "value_to_mailbox",
        "msgqueue": "value_to_messagequeue",
        "mempool": "value_to_mempool",
    }[kind]
    monkeypatch.setattr(
        adapter_module,
        converter_name,
        lambda raw, selected: (
            converted if raw is value and selected is layout else None
        ),
    )

    table = adapter_module.RtThreadAdapter(layout).object_table(kind)

    assert table is not None
    assert table.headers == headers
    assert table.rows == [expected_row]
    assert table.elastic == ("Name",)
    assert calls == [(type_code, layout)]


def test_object_detail_returns_none_for_unknown_kind():
    """A kind outside the adapter's tables is reported as not enumerable."""
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    assert adapter.object_detail("device", "whatever") is None


def test_object_detail_reports_not_found(monkeypatch):
    """A missing object yields an explicit ``found=False`` result."""
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "find_thread", lambda _name, _layout: None)

    detail = adapter.object_detail("task", "missing")

    assert detail is not None
    assert detail.found is False


def test_object_detail_task_uses_thread_builder(monkeypatch):
    """Thread detail includes the public thread fields."""
    thread = object()
    detail_pairs = [("Name", "worker1"), ("Address", "0x1000")]
    adapter = adapter_module.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(adapter_module, "find_thread", lambda _name, _layout: thread)
    monkeypatch.setattr(
        adapter_module, "value_to_thread", lambda _value, _layout: thread
    )
    monkeypatch.setattr(
        adapter_module.rt_detail, "thread_detail", lambda _thread: detail_pairs
    )

    detail = adapter.object_detail("task", "worker1")

    assert detail is not None
    assert detail.found is True
    assert detail.pairs == detail_pairs


@pytest.mark.parametrize(
    ("kind", "expected_pairs"),
    (
        ("semaphore", [("Value", "3")]),
        ("mutex", [("Owner", "main")]),
        ("event", [("Set", "0x3")]),
        ("mailbox", [("Entry", "2")]),
        ("msgqueue", [("MaxMsgs", "8")]),
        ("mempool", [("Free", "4")]),
    ),
)
def test_object_detail_ipc_kinds_use_their_converter_and_builder(
    kind, expected_pairs, monkeypatch
):
    """Every IPC kind converts its value then builds its own detail pairs."""
    layout = KernelLayout()
    adapter = adapter_module.RtThreadAdapter(layout)
    value = object()
    converted = object()
    type_code = 7
    monkeypatch.setattr(
        adapter_module,
        "resolve_object_type_code",
        lambda _kind, _layout: type_code,
    )
    monkeypatch.setattr(
        adapter_module,
        "find_rt_object",
        lambda code, _name, _layout: value if code == type_code else None,
    )
    monkeypatch.setattr(
        adapter_module,
        "_DETAIL_BUILDERS",
        {
            kind: (
                lambda _value, _layout: converted,
                lambda _converted: expected_pairs,
            )
        },
    )

    detail = adapter.object_detail(kind, "obj")

    assert detail is not None
    assert detail.found is True
    assert detail.pairs == expected_pairs
