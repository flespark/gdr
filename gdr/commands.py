"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    lookup_symbol_at,
    print_detail,
    print_table,
    warn,
)
from gdr.registry import active


def _display_address(address: int) -> str:
    return hex(address) if address else "N/A"


def _display_entry(address: int) -> str:
    symbol = lookup_symbol_at(address) if address else None
    return f"<{symbol}>" if symbol else _display_address(address)


@gdb_command_guard
def render_tasks() -> None:
    """Render all tasks from the active adapter in one normalized table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    summaries = list(adapter.iter_task_summaries())
    # SMP capability is data-driven so UP targets and UP adapters keep the
    # same column set.
    smp = any(task.bind_cpu is not None or task.oncpu is not None for task in summaries)
    rows: list[list[str]] = []
    for task in summaries:
        name = task.name + (" *" if task.current_core is not None else "")
        row = [
            name,
            task.state,
            str(task.priority) if task.priority is not None else "N/A",
            str(task.base_priority) if task.base_priority is not None else "N/A",
            _display_address(task.stack_pointer),
            str(task.stack_size) if task.stack_size is not None else "N/A",
            str(task.stack_used) if task.stack_used is not None else "N/A",
            str(task.high_water_mark) if task.high_water_mark is not None else "N/A",
            _display_entry(task.entry),
        ]
        if smp:
            row.append(_display_int(task.oncpu))
            row.append(_display_int(task.bind_cpu))
        row.append(_display_address(task.address))
        rows.append(row)

    headers = [
        "Name",
        "State",
        "Prio",
        "BasePrio",
        "SP",
        "Stack",
        "Used",
        "HighWater",
        "Entry",
    ]
    if smp:
        headers += ["CPU", "Bind"]
    headers.append("Addr")
    print_table(rows, headers, elastic=("Name", "Entry"))


def _display_int(value: int | None) -> str:
    """Render an optional int as its value or ``N/A``."""
    return str(value) if value is not None else "N/A"


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
        warn(f"usage: rtt {requested} <name>")
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
    """Render the normalized system summary from the active adapter."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    summary = adapter.system_summary()
    info(f"Kernel version: {summary.kernel_version}")
    info(f"Current task: {summary.current_task or 'unavailable'}")
    info(
        f"Task count: {summary.task_count if summary.task_count is not None else 'unavailable'}"
    )
    info(
        f"Tick count: {summary.tick_count if summary.tick_count is not None else 'unavailable'}"
    )
    info(f"Scheduler state: {summary.scheduler_state}")
    for state, count in summary.state_counts.items():
        info(f"{state}: {count}")
    for kind, count in sorted(summary.object_counts.items()):
        info(f"  {kind}: {count}")
    info(f"Heap: {summary.heap_summary}")


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
    """Normalize public singular/plural object vocabulary."""
    raw = kind.strip().lower()
    aliases = {
        "thread": "task",
        "threads": "task",
        "tasks": "task",
        "semaphores": "semaphore",
        "mutexes": "mutex",
        "timers": "timer",
        "mailboxs": "mailbox",
        "mailboxes": "mailbox",
        "messagequeue": "msgqueue",
        "messagequeues": "msgqueue",
        "mempools": "mempool",
    }
    return aliases.get(raw, raw)
