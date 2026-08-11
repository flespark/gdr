"""Unit tests for RT-Thread adapter summaries and tables."""

from __future__ import annotations

import rtthread.adapter as adapter_module
from gdr.abstractions import Timer
from gdr.adapter_api import TaskSummary
from gdr.layout import KernelLayout


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
