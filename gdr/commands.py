"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

from gdr.gdb_bridge import info, lookup_symbol_at, print_table, warn
from gdr.registry import active


def _display_address(address: int) -> str:
    return hex(address) if address else "N/A"


def _display_entry(address: int) -> str:
    symbol = lookup_symbol_at(address) if address else None
    return f"<{symbol}>" if symbol else _display_address(address)


def tasks() -> None:
    """Render all tasks from the active adapter in one normalized table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    rows: list[list[str]] = []
    for value in adapter.iter_tasks():
        task = adapter.summarize_task(value)
        name = task.name + (" *" if task.current_core is not None else "")
        rows.append(
            [
                name,
                task.state,
                str(task.priority) if task.priority is not None else "N/A",
                _display_address(task.stack_pointer),
                str(task.stack_size) if task.stack_size is not None else "N/A",
                str(task.stack_used) if task.stack_used is not None else "N/A",
                str(task.high_water_mark)
                if task.high_water_mark is not None
                else "N/A",
                _display_entry(task.entry),
            ]
        )
    print_table(
        rows,
        ["Name", "State", "Prio", "SP", "Stack", "Used", "HighWater", "Entry"],
    )


def system() -> None:
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
    for kind, count in sorted(adapter.object_counts().items()):
        info(f"  {kind}: {count}")
    info(f"Heap: {summary.heap_summary}")


def objects(kind: str = "") -> None:
    """Render reliably enumerable object counts from the active adapter.

    A count is an adapter capability claim: an absent kind means the adapter
    cannot enumerate it reliably, not that the target necessarily has none.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    counts = adapter.object_counts()
    requested = _canonical_kind(kind)
    if requested:
        if requested == "task":
            tasks()
            return
        table = adapter.object_table(requested)
        if table is not None:
            for message in table.messages:
                info(message)
            print_table(table.rows, table.headers)
            return
        if requested not in counts:
            warn(f"object kind {requested!r} is not reliably enumerable")
            return
        print_table([[requested, str(counts[requested])]], ["Kind", "Count"])
        return
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
