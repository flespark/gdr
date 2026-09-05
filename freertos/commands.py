"""FreeRTOS's RTOS-specific aggregate GDB commands."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.adapter import FreeRtosAdapter, iter_task_names
from freertos.navigation import discover
from gdr.adapter_api import active
from gdr.commands import (
    CommandTreeSpec,
    complete_command_tree,
    render_heap,
    render_object_detail,
    render_objects,
    render_system,
    render_tasks,
)
from gdr.gdb_bridge import gdb_command_guard, info, warn

_command_registered = False
_alias_registered = False

# Plural list commands and their semantic object kind. Only ``task`` is
# enumerable today; the other kinds are routed anyway so the command surface
# stays stable while their discovery channels land.
_OBJECT_COMMANDS = {
    "tasks": "task",
    "queues": "queue",
    "semaphores": "semaphore",
    "mutexes": "mutex",
    "timers": "timer",
    "eventgroups": "eventgroup",
    "streambuffers": "streambuffer",
}
# Singular forms render one object's vertical detail: ``frt <object> <name>``.
_SINGULAR_COMMANDS = {
    "task": "task",
    "queue": "queue",
    "semaphore": "semaphore",
    "mutex": "mutex",
    "timer": "timer",
    "eventgroup": "eventgroup",
    "streambuffer": "streambuffer",
}
_COMMAND_ALIASES = {
    "threads": "tasks",
    "sems": "semaphores",
    "mtxs": "mutexes",
    "qs": "queues",
    "egs": "eventgroups",
    "sbs": "streambuffers",
}
_COMMAND_DESCRIPTIONS = {
    "help": "Show this help",
    "tasks": "List tasks",
    "queues": "List queues",
    "semaphores": "List semaphores",
    "mutexes": "List mutexes",
    "timers": "List timers",
    "eventgroups": "List event groups",
    "streambuffers": "List stream buffers",
    "system": "Show the system summary",
    "objects": "Show object counts",
    "heap": "Show system heap status",
}
_DETAIL_DESCRIPTIONS = {
    "task": "Show one task's detail (frt task <name>)",
    "queue": "Show one queue's detail (frt queue <name>)",
    "semaphore": "Show one semaphore's detail (frt semaphore <name>)",
    "mutex": "Show one mutex's detail (frt mutex <name>)",
    "timer": "Show one timer's detail (frt timer <name>)",
    "eventgroup": "Show one event group's detail (frt eventgroup <name>)",
    "streambuffer": "Show one stream buffer's detail (frt streambuffer <name>)",
}
_USAGE = "usage: freertos <command> (run 'frt help' for available commands)"
# Reason: FreeRTOS TCBs do not store the task entry function pointer -- the
# initial stack frame carries it, so there is no stable DWARF field to read an
# ``Entry`` column from. The table deliberately omits it and help says why.
_NO_ENTRY_COLUMN = (
    "No 'Entry' column: FreeRTOS TCBs do not store a task entry function "
    "pointer, so there is no reliable field to display."
)
# Reason: a heap block header is exactly ``{pxNextFreeBlock, xBlockSize}``
# (heap_4.c BlockLink_t) and carries no owner field, so per-task heap usage
# cannot be attributed; help says why instead of faking a column.
_HEAP_NO_OWNER = (
    "No thread-ownership attribution for the heap: FreeRTOS block headers "
    "carry only pxNextFreeBlock + xBlockSize (no owner field), so per-task "
    "heap usage is not attributable."
)
_HELP = (
    "FreeRTOS commands:\n"
    + "\n".join(
        f"  frt {command:<14} {description}"
        for command, description in _COMMAND_DESCRIPTIONS.items()
    )
    + "\n\nSingle-object detail (frt <object> <name>):\n"
    + "\n".join(
        f"  frt {command:<14} {description}"
        for command, description in _DETAIL_DESCRIPTIONS.items()
    )
    + f"\n\n{_NO_ENTRY_COLUMN}\n{_HEAP_NO_OWNER}\n\nAliases:\n"
    + "\n".join(
        f"  {alias:<10} -> {command}" for alias, command in _COMMAND_ALIASES.items()
    )
)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one FreeRTOS command without depending on GDB."""
    args = argument.split()
    if not args or (len(args) == 1 and args[0].lower() == "help"):
        print(_HELP)
        return
    command = _COMMAND_ALIASES.get(args[0].lower(), args[0].lower())
    if len(args) >= 2 and command in _SINGULAR_COMMANDS:
        # Reason: FreeRTOS object names may contain spaces -- the timer daemon
        # task is literally "Tmr Svc" (timers.c configTIMER_SERVICE_TASK_NAME)
        # and pcTaskName is free text -- so the name is everything after the
        # command word, kept verbatim (split(None, 1) preserves inner spacing)
        # instead of a single token, which made those objects unreachable.
        render_object_detail(
            _SINGULAR_COMMANDS[command], argument.strip().split(None, 1)[1]
        )
    elif len(args) != 1:
        warn(_USAGE)
    elif command == "tasks":
        render_tasks()
    elif command == "system":
        render_system()
    elif command == "objects":
        render_objects("")
    elif command == "heap":
        render_heap()
    elif command in _OBJECT_COMMANDS:
        render_objects(_OBJECT_COMMANDS[command])
    else:
        warn(_USAGE)


def _command_vocabulary() -> list[str]:
    """Return every word the first argument may complete against."""
    return list(_COMMAND_DESCRIPTIONS) + list(_SINGULAR_COMMANDS)


def _object_names(kind: str) -> list[str]:
    """Return live object names of *kind* for tab completion.

    Tasks complete from the scheduler snapshot; other kinds complete from
    the discovery channels' names (registry names, static symbol names).
    Degrades to ``[]`` on any traversal failure so tab completion never
    raises inside GDB.
    """
    adapter = active()
    if not isinstance(adapter, FreeRtosAdapter):
        return []
    try:
        if kind == "task":
            return list(iter_task_names(adapter.layout))
        return sorted(obj.name for obj in discover(kind, adapter.layout) if obj.name)
    except Exception:
        # Reason: this is the GDB completion boundary (``complete()``), not a
        # command body. GDB completion must never raise or print -- the
        # guard's warn/err output would corrupt the readline prompt -- so we
        # swallow everything here and degrade to no candidates.
        return []


def _complete(text: str, word: str | None) -> list[str]:
    """Return tab-completion candidates for a partial ``frt`` command line.

    The first argument completes against the command vocabulary; the second
    argument of a singular detail command completes against live object
    names.  Mechanics are shared with the RT-Thread tree through
    :func:`gdr.commands.complete_command_tree`; FreeRTOS object names may
    contain spaces (the timer daemon task is literally ``"Tmr Svc"``), so
    the name is preserved verbatim.
    """
    return complete_command_tree(
        CommandTreeSpec(
            _command_vocabulary(),
            _COMMAND_ALIASES,
            _SINGULAR_COMMANDS,
            _object_names,
            preserve_spaces=True,
        ),
        text,
        word,
    )


if gdb is not None:

    class FreeRtosCommand(gdb.Command):
        """FreeRTOS command tree. Run `frt help` for available commands."""

        def __init__(self) -> None:
            # Reason: do not pass a completer_class here. GDB only calls the
            # command's Python ``complete()`` method when no completer class is
            # given; passing gdb.COMPLETE_COMMAND makes GDB complete with its
            # own command names and silently disables tab-completion of our
            # subcommands and live task names (GDB manual: "Command.complete").
            super().__init__("freertos", gdb.COMMAND_USER)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            _invoke_command(argument)

        def complete(self, text: str, word: str | None) -> list[str]:
            """Tab-complete subcommands and live task names."""
            return _complete(text, word)


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
