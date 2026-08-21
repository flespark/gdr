"""Fast unit tests for shared command rendering and error boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field

import gdr.commands as commands
import gdr.gdb_bridge as bridge
from gdr.adapter_api import ObjectDetail, ObjectTable, SystemSummary


@dataclass
class _Adapter:
    task_output: ObjectTable = field(default_factory=ObjectTable)
    counts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, ObjectTable] = field(default_factory=dict)
    details: dict[str, ObjectDetail] = field(default_factory=dict)
    summary: SystemSummary = field(default_factory=SystemSummary)
    count_calls: int = 0

    def iter_tasks(self):
        raise AssertionError("raw task iteration is not used for rendering")

    def task_table(self):
        return self.task_output

    def object_counts(self) -> dict[str, int]:
        self.count_calls += 1
        return self.counts

    def object_table(self, kind: str) -> ObjectTable | None:
        return self.tables.get(kind)

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:  # noqa: ARG002
        return self.details.get(kind)

    def system_summary(self) -> SystemSummary:
        return self.summary


def test_tasks_renders_adapter_owned_table(monkeypatch):
    """The shared renderer forwards an adapter-owned task table unchanged."""
    adapter = _Adapter(
        task_output=ObjectTable(
            headers=["Task", "Runtime"],
            rows=[["worker *", "0"]],
            messages=["snapshot complete"],
            elastic=("Task",),
        )
    )
    tables = []
    messages = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **kwargs: tables.append((rows, headers, kwargs)),
    )

    commands.render_tasks()

    assert messages == ["snapshot complete"]
    assert tables == [
        ([["worker *", "0"]], ["Task", "Runtime"], {"elastic": ("Task",)})
    ]


def test_tasks_renders_an_empty_adapter_without_target_access(monkeypatch):
    """An initialized adapter with no tasks still emits an empty table."""
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", _Adapter)
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
        "Heap allocator: unavailable",
    ]
    assert adapter.count_calls == 0


def test_system_renders_heap_allocator_from_snapshot_fields(monkeypatch):
    """The renderer, not the adapter, formats algorithm/used/total."""
    adapter = _Adapter(
        summary=SystemSummary(
            heap_allocator="small_mem", heap_used=4096, heap_total=65536
        )
    )
    messages: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)

    commands.render_system()

    assert messages[-1] == "Heap allocator: small_mem, used: 4096, total: 65536"


def test_system_renders_partial_heap_counters_as_na(monkeypatch):
    """A one-sided snapshot still names the allocator; the missing side is N/A."""
    adapter = _Adapter(
        summary=SystemSummary(heap_allocator="small_mem", heap_total=65536)
    )
    messages: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)

    commands.render_system()

    assert messages[-1] == "Heap allocator: small_mem, used: N/A, total: 65536"

    adapter.summary = SystemSummary(heap_allocator="small_mem", heap_used=0)
    messages.clear()
    commands.render_system()
    assert messages[-1] == "Heap allocator: small_mem, used: 0, total: N/A"


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

    commands.render_objects("timer")

    assert messages == ["Kernel tick: 10"]
    assert tables == [
        ([["heartbeat", "<tick>"]], ["Name", "Callback"], ("Name", "Callback"))
    ]
    assert adapter.count_calls == 0


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

    commands.render_objects("msgqueue")

    assert adapter.count_calls == 1
    assert tables == [([["msgqueue", "2"]], ["Kind", "Count"])]


def test_shared_renderer_guard_contains_unexpected_adapter_errors(monkeypatch):
    """A broken adapter cannot leak a Python exception through a GDB command."""
    errors: list[str] = []

    class _BrokenAdapter(_Adapter):
        def task_table(self):
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
    """A missing object name receives a generic renderer diagnostic."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("semaphore", "  ")

    assert warnings == ["object name must not be empty"]
