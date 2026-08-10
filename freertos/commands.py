"""Aggregate FreeRTOS GDB commands."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.adapter import iter_converted_tasks
from freertos.layout import FreeRtosLayout
from freertos.navigation import current_tasks, list_count, system_value
from gdr.gdb_bridge import gdb_command_guard, info, lookup_symbol_at, print_table, warn

_layout: FreeRtosLayout | None = None
_version: tuple[int, int, int] | None = None
_registered = False


def _entry(addr: int) -> str:
    symbol = lookup_symbol_at(addr) if addr else None
    return symbol or (hex(addr) if addr else "N/A")


@gdb_command_guard
def _cmd_tasks() -> None:
    if _layout is None:
        warn("run `gdr init freertos <version>` first")
        return
    rows = []
    for task in iter_converted_tasks(_layout):
        marker = " *" if task.core is not None else ""
        rows.append(
            [
                task.name + marker,
                task.state,
                str(task.current_priority),
                hex(task.top_of_stack) if task.top_of_stack else "N/A",
                str(task.stack_size) if task.stack_size is not None else "N/A",
                str(task.high_water_mark)
                if task.high_water_mark is not None
                else "N/A",
                _entry(task.entry),
            ]
        )
    print_table(rows, ["Name", "State", "Prio", "Top", "Stack", "HighWater", "Entry"])


def _count(label: str, key: str) -> str:
    value = list_count(key, _layout) if _layout is not None else None
    return f"{label}: {value if value is not None else 'unavailable'}"


@gdb_command_guard
def _cmd_system() -> None:
    if _layout is None:
        warn("run `gdr init freertos <version>` first")
        return
    version = ".".join(map(str, _version)) if _version else "unknown"
    info(f"Kernel version: {version}")
    current = current_tasks(_layout)
    tasks = list(iter_converted_tasks(_layout))
    current_task = (
        next((t for t in tasks if t.address == current[0][1]), None)
        if current
        else None
    )
    info(f"Current task: {current_task.name if current_task else 'unavailable'}")
    total = system_value("uxCurrentNumberOfTasks")
    info(f"Task count: {total if total is not None else len(tasks)}")
    tick = system_value("xTickCount")
    info(f"Tick count: {tick if tick is not None else 'unavailable'}")
    running = system_value("xSchedulerRunning")
    info(
        f"Scheduler state: {('running' if running else 'not-running') if running is not None else 'unavailable'}"
    )
    info(_count("Ready", "ready"))
    delayed = [list_count(key, _layout) for key in ("delayed_1", "delayed_2")]
    info(
        f"Delayed: {sum(x for x in delayed if x is not None) if any(x is not None for x in delayed) else 'unavailable'}"
    )
    info(_count("Pending", "pending"))
    info(_count("Suspended", "suspended"))
    info(_count("Termination", "termination"))
    info("Heap: unavailable")


if gdb is not None:

    class _FreeRtosCmd(gdb.Command):
        def __init__(self):
            super().__init__("freertos", gdb.COMMAND_USER, gdb.COMPLETE_COMMAND)

        def invoke(self, argument: str, from_tty: bool) -> None:
            args = argument.split()
            if not args or args[0].lower() == "help":
                print("freertos tasks | freertos system")
            elif args[0].lower() == "tasks":
                _cmd_tasks()
            elif args[0].lower() == "system":
                _cmd_system()
            else:
                warn(f"unknown FreeRTOS subcommand: {args[0]!r}")


def register_commands(layout: FreeRtosLayout, version: tuple[int, int, int]) -> None:
    global _layout, _version, _registered
    if _layout is not None:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    _layout, _version = layout, version
    if not _registered:
        _FreeRtosCmd()
        gdb.execute("alias frt = freertos")
        _registered = True
    info("freertos commands registered (alias: frt)")


def register_command_shell() -> None:
    """Compatibility hook; command registration now occurs after init."""
