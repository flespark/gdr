"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

from gdr.adapter_api import active
from gdr.formatting import format_optional_int
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_detail,
    print_table,
    warn,
)


@gdb_command_guard
def render_tasks() -> None:
    """Render the active adapter's task table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    table = adapter.task_table()
    for message in table.messages:
        info(message)
    print_table(table.rows, table.headers, elastic=table.elastic)


@gdb_command_guard
def render_object_detail(kind: str, name: str) -> None:
    """Render one object as a vertical key/value detail block.

    The adapter supplies RTOS-neutral ``(key, value)`` pairs so no adapter
    field names leak into the generic renderer.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    requested = _canonical_kind(kind)
    if not name.strip():
        warn("object name must not be empty")
        return
    detail = adapter.object_detail(requested, name)
    if detail is None:
        warn(f"object kind {requested!r} is not reliably enumerable")
        return
    if not detail.found:
        warn(f"{requested} {name!r}: not found or type not enabled")
        return
    print_detail(detail.pairs)


@gdb_command_guard
def render_system() -> None:
    """Render the normalized system summary as a vertical key/value block."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    summary = adapter.system_summary()
    pairs: list[tuple[str, str]] = [
        ("Kernel version", summary.kernel_version),
        ("Current task", summary.current_task or "unavailable"),
        (
            "Task count",
            str(summary.task_count)
            if summary.task_count is not None
            else "unavailable",
        ),
        (
            "Tick count",
            str(summary.tick_count)
            if summary.tick_count is not None
            else "unavailable",
        ),
        ("Scheduler state", summary.scheduler_state),
    ]
    pairs.extend((state, str(count)) for state, count in summary.state_counts.items())
    pairs.extend(
        (kind, str(count)) for kind, count in sorted(summary.object_counts.items())
    )
    pairs.append(("Heap allocator", summary.heap_allocator or "unavailable"))
    if summary.heap_used is not None or summary.heap_total is not None:
        pairs.append(("Heap used", format_optional_int(summary.heap_used)))
        pairs.append(("Heap total", format_optional_int(summary.heap_total)))
    if summary.heap_status is not None:
        pairs.append(("Heap status", summary.heap_status))
    print_detail(pairs)


@gdb_command_guard
def render_objects(kind: str = "") -> None:
    """Render reliably enumerable object counts from the active adapter.

    A count is an adapter capability claim: an absent kind means the adapter
    cannot enumerate it reliably, not that the target necessarily has none.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    requested = _canonical_kind(kind)
    if requested:
        if requested == "task":
            render_tasks()
            return
        table = adapter.object_table(requested)
        if table is not None:
            for message in table.messages:
                info(message)
            print_table(table.rows, table.headers, elastic=table.elastic)
            return
        counts = adapter.object_counts()
        if requested not in counts:
            warn(f"object kind {requested!r} is not reliably enumerable")
            return
        print_table([[requested, str(counts[requested])]], ["Kind", "Count"])
        return
    counts = adapter.object_counts()
    print_table(
        [[name, str(count)] for name, count in sorted(counts.items())],
        ["Kind", "Count"],
    )


def _canonical_kind(kind: str) -> str:
    """Normalize only the adapter-neutral semantic kind spelling."""
    return kind.strip().lower()
