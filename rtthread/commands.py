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

_OBJECT_COMMANDS = {
    "semaphores": "semaphore",
    "mutexes": "mutex",
    "events": "event",
    "mailboxs": "mailbox",
    "messagequeues": "msgqueue",
    "mempools": "mempool",
    "timers": "timer",
}
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
_COMMAND_DESCRIPTIONS = {
    "help": "Show this help",
    "threads": "List threads",
    "semaphores": "List semaphores",
    "mutexes": "List mutexes",
    "events": "List events",
    "mailboxs": "List mailboxes",
    "messagequeues": "List message queues",
    "mempools": "List memory pools",
    "timers": "List timers",
    "system": "Show the system summary",
}
_USAGE = "usage: rtthread <command> (run 'rtt help' for available commands)"
_HELP = (
    "RT-Thread commands:\n"
    + "\n".join(
        f"  rtt {command:<14} {description}"
        for command, description in _COMMAND_DESCRIPTIONS.items()
    )
    + "\n\nAliases:\n"
    + "\n".join(
        f"  {alias:<10} -> {command}" for alias, command in _COMMAND_ALIASES.items()
    )
)


def _invoke_command(argument: str) -> None:
    """Parse and dispatch one RT-Thread command without depending on GDB."""
    args = argument.split()
    if not args or (len(args) == 1 and args[0].lower() == "help"):
        print(_HELP)
        return
    command = _COMMAND_ALIASES.get(args[0].lower(), args[0].lower())
    if len(args) != 1:
        warn(_USAGE)
    elif command == "threads":
        render_tasks()
    elif command == "system":
        render_system()
    elif command in _OBJECT_COMMANDS:
        render_objects(_OBJECT_COMMANDS[command])
    else:
        warn(_USAGE)


if gdb is not None:

    class RtThreadCommand(gdb.Command):
        """RT-Thread command tree. Run `rtt help` for available commands."""

        def __init__(self) -> None:
            super().__init__("rtthread", gdb.COMMAND_USER, gdb.COMPLETE_COMMAND)

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
        RtThreadCommand()
        _command_registered = True
    if not _alias_registered:
        gdb.execute("alias rtt = rtthread")
        _alias_registered = True
    info("rtthread commands registered (alias: rtt)")
