"""Fast unit tests for shared command rendering and error boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field

import gdr.commands as commands
import gdr.gdb_bridge as bridge
from gdr.adapter_api import ObjectDetail, ObjectTable, SystemSummary, TaskSummary


@dataclass
class _Adapter:
    tasks: list[TaskSummary] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, ObjectTable] = field(default_factory=dict)
    details: dict[str, ObjectDetail] = field(default_factory=dict)
    summary: SystemSummary = field(default_factory=SystemSummary)
    count_calls: int = 0

    def iter_tasks(self):
        raise AssertionError("raw task iteration is not used for rendering")

    def iter_task_summaries(self):
        return iter(self.tasks)

    def object_counts(self) -> dict[str, int]:
        self.count_calls += 1
        return self.counts

    def object_table(self, kind: str) -> ObjectTable | None:
        return self.tables.get(kind)

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:  # noqa: ARG002
        return self.details.get(kind)

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
                base_priority=20,
                stack_pointer=0x3000,
                stack_size=1024,
                stack_used=128,
                high_water_mark=256,
                entry=0x1000,
                address=0x4000,
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
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_tasks()

    assert looked_up == [0x1000, 0x2000]
    assert tables == [
        (
            [
                [
                    "symbolized *",
                    "Ready",
                    "20",
                    "20",
                    "0x3000",
                    "1024",
                    "128",
                    "256",
                    "<worker_entry+0>",
                    "0x4000",
                ],
                [
                    "unknown",
                    "Suspend",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "0x2000",
                    "N/A",
                ],
                [
                    "zero",
                    "Unknown",
                    "0",
                    "N/A",
                    "N/A",
                    "0",
                    "0",
                    "0",
                    "N/A",
                    "N/A",
                ],
            ],
            [
                "Name",
                "State",
                "Prio",
                "BasePrio",
                "SP",
                "Stack",
                "Used",
                "HighWater",
                "Entry",
                "Addr",
            ],
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
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_tasks()

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

    commands.render_tasks()

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
            object_counts={"timer": 2, "task": 3},
            heap_summary="unavailable",
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)

    commands.render_system()

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
    assert adapter.count_calls == 0


def test_objects_normalizes_kind_and_renders_adapter_table(monkeypatch):
    """Plural command names select the matching semantic object table."""
    adapter = _Adapter(
        counts={"timer": 1},
        tables={
            "timer": ObjectTable(
                headers=["Name", "Callback"],
                rows=[["heartbeat", "<tick>"]],
                messages=["Kernel tick: 10"],
                elastic=("Name", "Callback"),
            )
        },
    )
    messages: list[str] = []
    tables: list[tuple[list[list[str]], list[str], tuple[str, ...]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append(
            (rows, headers, _kwargs.get("elastic", ()))
        ),
    )

    commands.render_objects("timers")

    assert messages == ["Kernel tick: 10"]
    assert tables == [
        ([["heartbeat", "<tick>"]], ["Name", "Callback"], ("Name", "Callback"))
    ]
    assert adapter.count_calls == 0


def test_tasks_renderer_passes_elastic_metadata(monkeypatch):
    """The shared task renderer marks Name/Entry as shrinkable."""
    adapter = _Adapter(
        tasks=[
            TaskSummary(name="worker1", state="Ready", priority=20),
        ]
    )
    tables: list[tuple[list[list[str]], list[str], tuple[str, ...]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "lookup_symbol_at", lambda _address: None)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append(
            (rows, headers, _kwargs.get("elastic", ()))
        ),
    )

    commands.render_tasks()

    assert tables[0][2] == ("Name", "Entry")


def test_tasks_renderer_adds_cpu_and_bind_for_smp(monkeypatch):
    """SMP adapters surface CPU/Bind columns with the real CPU placement."""
    adapter = _Adapter(
        tasks=[
            TaskSummary(
                name="worker1",
                state="Running",
                priority=20,
                current_core=1,
                oncpu=1,
                bind_cpu=0,
                address=0x1000,
            ),
            TaskSummary(name="worker2", state="Ready", priority=21, address=0x2000),
        ]
    )
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "lookup_symbol_at", lambda _address: None)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_tasks()

    headers = tables[0][1]
    assert headers == [
        "Name",
        "State",
        "Prio",
        "BasePrio",
        "SP",
        "Stack",
        "Used",
        "HighWater",
        "Entry",
        "CPU",
        "Bind",
        "Addr",
    ]
    assert tables[0][0] == [
        [
            "worker1 *",
            "Running",
            "20",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "1",
            "0",
            "0x1000",
        ],
        [
            "worker2",
            "Ready",
            "21",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "N/A",
            "0x2000",
        ],
    ]


def test_objects_only_counts_when_no_detailed_table_is_available(monkeypatch):
    """Count traversal remains a fallback for kinds without detailed tables."""
    adapter = _Adapter(counts={"msgqueue": 2})
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_objects("messagequeues")

    assert adapter.count_calls == 1
    assert tables == [([["msgqueue", "2"]], ["Kind", "Count"])]


def test_shared_renderer_guard_contains_unexpected_adapter_errors(monkeypatch):
    """A broken adapter cannot leak a Python exception through a GDB command."""
    errors: list[str] = []

    class _BrokenAdapter(_Adapter):
        def iter_task_summaries(self):
            raise ValueError("corrupt task list")

    monkeypatch.setattr(commands, "active", _BrokenAdapter)
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    assert commands.render_tasks() is None
    assert errors == ["render_tasks: ValueError: corrupt task list"]


def test_object_detail_renders_adapter_pairs(monkeypatch):
    """A found object renders its adapter-supplied key/value pairs."""
    adapter = _Adapter(
        details={
            "semaphore": ObjectDetail(pairs=[("Name", "test_sem"), ("Value", "3")])
        }
    )
    written: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_object_detail("semaphore", "test_sem")

    assert written == [[("Name", "test_sem"), ("Value", "3")]]


def test_object_detail_warns_when_kind_is_not_enumerable(monkeypatch):
    """Adapters that cannot enumerate a kind produce an explicit warning."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("mempool", "x")

    assert warnings == ["object kind 'mempool' is not reliably enumerable"]


def test_object_detail_warns_when_object_is_missing(monkeypatch):
    """A missing or disabled object reports a clear not-found diagnostic."""
    adapter = _Adapter(details={"semaphore": ObjectDetail(found=False)})
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("semaphore", "missing")

    assert warnings == ["semaphore 'missing': not found or type not enabled"]


def test_object_detail_warns_before_initialization(monkeypatch):
    """The detail renderer rejects use before an adapter has been selected."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: None)
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands.render_object_detail("semaphore", "test_sem")

    assert warnings == ["run `gdr init <rtos> <version>` first"]


def test_object_detail_warns_for_an_empty_name(monkeypatch):
    """A missing object name routes the user back to the command usage."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("semaphore", "  ")

    assert warnings == ["usage: rtt semaphore <name>"]
