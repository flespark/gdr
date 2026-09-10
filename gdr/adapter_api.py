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
    # Row label for task_count; adapters override it to name the evidence
    # source (a kernel counter vs a scheduler-list walk). Whether both
    # sources exist is adapter-specific, so the neutral default stays
    # unqualified.
    task_count_label: str = "Task count"
    tick_count: int | None = None
    scheduler_state: str = "N/A"
    state_counts: dict[str, int] = field(default_factory=dict)
    object_counts: dict[str, int] = field(default_factory=dict)
    heap_allocator: str | None = None
    heap_used: int | None = None
    heap_total: int | None = None
    heap_status: str | None = None
    extra_pairs: list[tuple[str, str]] = field(default_factory=list)
    # Adapter-owned vertical rows beyond the normalized fields (consistency
    # checks, provenance notes).  ``gdr.commands.render_system`` appends them
    # verbatim, so RTOS-specific verdict strings never leak into the core.


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
    enabled in the current target configuration.  ``message`` replaces the
    renderer's generic "not found" line with a precise reason -- e.g. the name
    resolved to a different object kind -- so the user gets an actionable
    redirect instead of a misleading detail block.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    found: bool = True
    message: str | None = None


@dataclass
class HeapReport:
    """One heap snapshot formatted for a neutral ``render_heap``.

    ``pairs`` are the vertical basics (Algorithm/sizes/counters), ``table``
    the optional block/occupancy table, and ``messages`` capability notes
    (heap_3 wraps malloc, no allocator, ...) printed above the table.  Both
    adapters return this shape so the core owns a single ``render_heap``
    for the RTOS heap command.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    table: ObjectTable | None = None
    messages: list[str] = field(default_factory=list)


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

    def object_summary_table(self) -> ObjectTable | None:
        """Per-kind provenance summary for the objects command, or ``None``.

        ``None`` (the default) makes the neutral renderer fall back to the
        plain Kind/Count table from :meth:`object_counts`; an adapter that
        can show where each object came from (provenance channels) returns
        its own table.
        """
        return None

    def heap_report(self) -> HeapReport | None:
        """One heap snapshot formatted for the neutral ``render_heap``.

        ``None`` means the RTOS has no heap to report; adapters without a
        heap command return it so the neutral renderer prints nothing.
        """
        return None
