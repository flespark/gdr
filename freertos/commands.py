"""FreeRTOS's RTOS-specific aggregate GDB commands."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.commands import render_system, render_tasks
from gdr.gdb_bridge import info, warn

_registered = False


if gdb is not None:

    class FreeRtosCommand(gdb.Command):
        """FreeRTOS command tree.

        Usage:
            freertos tasks
            freertos system
        """

        def __init__(self) -> None:
            super().__init__("freertos", gdb.COMMAND_USER, gdb.COMPLETE_COMMAND)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            args = argument.split()
            if not args or args[0].lower() == "help":
                print(self.__doc__)
            elif args[0].lower() == "tasks" and len(args) == 1:
                render_tasks()
            elif args[0].lower() == "system" and len(args) == 1:
                render_system()
            else:
                warn("usage: freertos <tasks|system>")


def register_commands() -> None:
    """Register the FreeRTOS command tree once for the active GDB session."""
    global _registered
    if _registered:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    FreeRtosCommand()
    _registered = True
    info("freertos commands registered")
