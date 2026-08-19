"""FreeRTOS's RTOS-specific aggregate GDB commands."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.commands import render_system, render_tasks
from gdr.gdb_bridge import gdb_command_guard, info, warn

_command_registered = False
_alias_registered = False

_HELP = (
    "FreeRTOS commands:\n"
    "  freertos tasks    List tasks\n"
    "  freertos system   Show the system summary\n"
    "\nAliases:\n"
    "  frt        -> freertos\n"
)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one FreeRTOS command without depending on GDB."""
    args = argument.split()
    if not args or args[0].lower() == "help":
        print(_HELP)
    elif args[0].lower() == "tasks" and len(args) == 1:
        render_tasks()
    elif args[0].lower() == "system" and len(args) == 1:
        render_system()
    else:
        warn("usage: freertos <tasks|system>")


if gdb is not None:

    class FreeRtosCommand(gdb.Command):
        """FreeRTOS command tree. Run `frt help` for available commands."""

        def __init__(self) -> None:
            super().__init__("freertos", gdb.COMMAND_USER, gdb.COMPLETE_COMMAND)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            _invoke_command(argument)


def register_commands() -> None:
    """Register the command tree and alias, resuming after partial failure."""
    global _alias_registered, _command_registered
    if _command_registered and _alias_registered:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    if not _command_registered:
        FreeRtosCommand()
        _command_registered = True
    if not _alias_registered:
        gdb.execute("alias frt = freertos")
        _alias_registered = True
    info("freertos commands registered (alias: frt)")
