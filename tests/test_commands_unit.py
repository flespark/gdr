"""Fast unit tests for shared command rendering and error boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field

import gdr.commands as commands
import gdr.gdb_bridge as bridge
import rtthread.adapter as rt_adapter
from gdr.abstractions import Timer
from gdr.adapter_api import ObjectTable, SystemSummary, TaskSummary
from gdr.layout import KernelLayout


@dataclass
class _Adapter:
    tasks: list[TaskSummary] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, ObjectTable] = field(default_factory=dict)
    summary: SystemSummary = field(default_factory=SystemSummary)

    def iter_tasks(self):
        return iter(range(len(self.tasks)))

    def summarize_task(self, value: int) -> TaskSummary:
        return self.tasks[value]

    def object_counts(self) -> dict[str, int]:
        return self.counts

    def object_table(self, kind: str) -> ObjectTable | None:
        return self.tables.get(kind)

    def system_summary(self) -> SystemSummary:
        return self.summary


def test_tasks_renders_symbols_address_fallbacks_and_optional_values(monkeypatch):
    """Task rows preserve symbols, raw addresses, nulls, and numeric zeroes."""
    adapter = _Adapter(
        tasks=[
            TaskSummary(
                name="symbolized",
                state="Ready",
                priority=20,
                stack_pointer=0x3000,
                stack_size=1024,
                stack_used=128,
                high_water_mark=256,
                entry=0x1000,
                current_core=0,
            ),
            TaskSummary(name="unknown", state="Suspend", entry=0x2000),
            TaskSummary(
                name="zero",
                state="Unknown",
                priority=0,
                stack_size=0,
                stack_used=0,
                high_water_mark=0,
            ),
        ]
    )
    tables: list[tuple[list[list[str]], list[str]]] = []
    looked_up: list[int] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "lookup_symbol_at",
        lambda address: (
            looked_up.append(address)
            or ("worker_entry+0" if address == 0x1000 else None)
        ),
    )
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers: tables.append((rows, headers)),
    )

    commands.tasks()

    assert looked_up == [0x1000, 0x2000]
    assert tables == [
        (
            [
                [
                    "symbolized *",
                    "Ready",
                    "20",
                    "0x3000",
                    "1024",
                    "128",
                    "256",
                    "<worker_entry+0>",
                ],
                [
                    "unknown",
                    "Suspend",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "0x2000",
                ],
                ["zero", "Unknown", "0", "N/A", "0", "0", "0", "N/A"],
            ],
            ["Name", "State", "Prio", "SP", "Stack", "Used", "HighWater", "Entry"],
        )
    ]


def test_tasks_renders_an_empty_adapter_without_target_access(monkeypatch):
    """An initialized adapter with no tasks still emits an empty table."""
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", _Adapter)
    monkeypatch.setattr(
        commands,
        "lookup_symbol_at",
        lambda _address: (_ for _ in ()).throw(AssertionError("unexpected lookup")),
    )
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers: tables.append((rows, headers)),
    )

    commands.tasks()

    assert tables[0][0] == []


def test_tasks_warns_before_initialization(monkeypatch):
    """The renderer rejects use before an adapter has been selected."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: None)
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected table")),
    )

    commands.tasks()

    assert warnings == ["run `gdr init <rtos> <version>` first"]


def test_system_renders_summary_and_sorted_object_counts(monkeypatch):
    """The system renderer preserves normalized fields and stable count order."""
    adapter = _Adapter(
        counts={"timer": 2, "task": 3},
        summary=SystemSummary(
            kernel_version="10.3.1",
            current_task="worker",
            task_count=3,
            tick_count=123,
            scheduler_state="running",
            state_counts={"Ready": 2, "Suspended": 1},
            heap_summary="unavailable",
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)

    commands.system()

    assert messages == [
        "Kernel version: 10.3.1",
        "Current task: worker",
        "Task count: 3",
        "Tick count: 123",
        "Scheduler state: running",
        "Ready: 2",
        "Suspended: 1",
        "  task: 3",
        "  timer: 2",
        "Heap: unavailable",
    ]


def test_objects_normalizes_kind_and_renders_adapter_table(monkeypatch):
    """Plural command names select the matching semantic object table."""
    adapter = _Adapter(
        counts={"timer": 1},
        tables={
            "timer": ObjectTable(
                headers=["Name", "Callback"],
                rows=[["heartbeat", "<tick>"]],
                messages=["Kernel tick: 10"],
            )
        },
    )
    messages: list[str] = []
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers: tables.append((rows, headers)),
    )

    commands.objects("timers")

    assert messages == ["Kernel tick: 10"]
    assert tables == [([["heartbeat", "<tick>"]], ["Name", "Callback"])]


def test_shared_renderer_guard_contains_unexpected_adapter_errors(monkeypatch):
    """A broken adapter cannot leak a Python exception through a GDB command."""
    errors: list[str] = []

    class _BrokenAdapter(_Adapter):
        def iter_tasks(self):
            raise ValueError("corrupt task list")

    monkeypatch.setattr(commands, "active", _BrokenAdapter)
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    assert commands.tasks() is None
    assert errors == ["tasks: ValueError: corrupt task list"]


def test_rtthread_timer_table_symbolizes_and_falls_back_to_addresses(monkeypatch):
    """Timer callbacks retain the old symbol, address, and null boundary behavior."""
    timers = [
        Timer(name="symbolized", callback=0x1000),
        Timer(name="unknown", callback=0x2000),
        Timer(name="null", callback=0),
    ]
    looked_up: list[int] = []
    adapter = rt_adapter.RtThreadAdapter(KernelLayout())
    monkeypatch.setattr(rt_adapter, "iter_timers", lambda _layout: iter(timers))
    monkeypatch.setattr(rt_adapter, "value_to_timer", lambda value, _layout: value)
    monkeypatch.setattr(rt_adapter, "get_tick", lambda: 123)
    monkeypatch.setattr(
        rt_adapter,
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
