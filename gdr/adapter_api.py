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
class SystemSummary:
    """RTOS-neutral system data for an adapter-owned system command."""

    kernel_version: str = "unknown"
    current_task: str | None = None
    task_count: int | None = None
    tick_count: int | None = None
    scheduler_state: str = "unavailable"
    state_counts: dict[str, int] = field(default_factory=dict)
    object_counts: dict[str, int] = field(default_factory=dict)
    heap_summary: str = "unavailable"


@dataclass
class ObjectTable:
    """Adapter-provided rows for a reliably enumerable object kind.

    ``elastic`` lists the headers that may shrink when the natural table
    width exceeds the terminal width (see ``gdr.gdb_bridge.print_table``),
    ordered from first to last shrink priority. Adapters own this metadata so
    renderers never guess it from header text.
    """

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    elastic: tuple[str, ...] = ()


@dataclass
class ObjectDetail:
    """Vertical key/value detail for one named object.

    ``found`` is ``False`` when the object does not exist or its type is not
    enabled in the current target configuration.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    found: bool = True


_active: RtosAdapter | None = None


def register(adapter: RtosAdapter) -> None:
    """Register one session adapter and reject replacement by another."""
    global _active
    if _active is None:
        _active = adapter
    elif _active is not adapter:
        raise RuntimeError("an RTOS adapter is already initialized")


def active() -> RtosAdapter | None:
    return _active


def is_initialized() -> bool:
    return _active is not None


class RtosAdapter(Protocol):
    """Operations required by the stable generic GDB API."""

    def find_task(self, name: str) -> gdb.Value | None: ...

    def find_object(self, kind: str, name: str) -> gdb.Value | None: ...

    def object_counts(self) -> dict[str, int]: ...

    def object_table(self, kind: str) -> ObjectTable | None: ...

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None: ...

    def iter_tasks(self) -> Iterator[gdb.Value]: ...

    def task_table(self) -> ObjectTable: ...

    def system_summary(self) -> SystemSummary: ...
