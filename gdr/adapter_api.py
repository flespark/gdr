"""RTOS-neutral semantic adapter contract used by GDR's public interface."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


@dataclass
class TaskSummary:
    """A normalized task view used only for generic command output.

    ``value`` remains target-native and is returned by convenience functions;
    no adapter ABI is hidden behind this presentation model.
    """

    name: str = ""
    address: int = 0
    state: str = "Unknown"
    priority: int | None = None
    base_priority: int | None = None
    stack_pointer: int = 0
    stack_size: int | None = None
    stack_used: int | None = None
    high_water_mark: int | None = None
    entry: int = 0
    current_core: int | None = None


@dataclass
class SystemSummary:
    """RTOS-neutral system data for an adapter-owned system command."""

    kernel_version: str = "unknown"
    current_task: str | None = None
    task_count: int | None = None
    tick_count: int | None = None
    scheduler_state: str = "unavailable"
    state_counts: dict[str, int] = field(default_factory=dict)
    heap_summary: str = "unavailable"


@dataclass
class ObjectTable:
    """Adapter-provided rows for a reliably enumerable object kind."""

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class RtosAdapter(Protocol):
    """Operations required by the stable generic GDB API."""

    def find_task(self, name: str) -> gdb.Value | None: ...

    def find_object(self, kind: str, name: str) -> gdb.Value | None: ...

    def object_counts(self) -> dict[str, int]: ...

    def object_table(self, kind: str) -> ObjectTable | None: ...

    def iter_tasks(self) -> Iterator[gdb.Value]: ...

    def summarize_task(self, value: gdb.Value) -> TaskSummary: ...

    def system_summary(self) -> SystemSummary: ...
