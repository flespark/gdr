"""RT-Thread's RTOS-specific command tree."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.commands import render_objects, render_system, render_tasks
from gdr.gdb_bridge import info, warn

_command_registered = False
_alias_registered = False

_COMMAND_ALIASES = {
    "tasks": "threads",
    "sems": "semaphores",
    "msgs": "messagequeues",
    "mtxs": "mutexes",
    "mboxs": "mailboxs",
    # Accept the conventional spelling in addition to RT-Thread's historical
    # command spelling used by the short alias above.
    "mailboxes": "mailboxs",
}


if gdb is not None:

    class RtThreadCommand(gdb.Command):
        """RT-Thread command tree.

        Usage:
            rtthread threads
            rtthread semaphores
            rtthread mutexes
            rtthread timers
            rtthread messagequeues
            rtthread mailboxs
            rtthread system

        Short aliases:
            tasks, sems, mtxs, msgs, mboxs
        """

        def __init__(self) -> None:
            super().__init__("rtthread", gdb.COMMAND_USER, gdb.COMPLETE_COMMAND)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            args = argument.split()
            if not args or args[0].lower() == "help":
                print(self.__doc__)
                return
            command = _COMMAND_ALIASES.get(args[0].lower(), args[0].lower())
            if command == "threads" and len(args) == 1:
                render_tasks()
            elif command == "system" and len(args) == 1:
                render_system()
            elif (
                command
                in {
                    "semaphores",
                    "mutexes",
                    "timers",
                    "messagequeues",
                    "mailboxs",
                }
                and len(args) == 1
            ):
                render_objects(command)
            else:
                warn(
                    "usage: rtthread <threads|semaphores|mutexes|timers|"
                    "messagequeues|mailboxs|system>"
                )


def register_commands() -> None:
    """Register the command tree and alias, resuming after partial failure."""
    global _alias_registered, _command_registered
    if _command_registered and _alias_registered:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    if not _command_registered:
        RtThreadCommand()
        _command_registered = True
    if not _alias_registered:
        gdb.execute("alias rtt = rtthread")
        _alias_registered = True
    info("rtthread commands registered (alias: rtt)")
