"""RT-Thread's RTOS-specific command tree."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.commands import objects as render_objects
from gdr.commands import system as render_system
from gdr.commands import tasks as render_tasks
from gdr.gdb_bridge import info, warn

_registered = False

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
    """Register the RT-Thread command tree and its short alias once."""
    global _registered
    if _registered:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    RtThreadCommand()
    gdb.execute("alias rtt = rtthread")
    _registered = True
    info("rtthread commands registered (alias: rtt)")
