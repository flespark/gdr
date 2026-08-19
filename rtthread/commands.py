"""RT-Thread's RTOS-specific command tree."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.adapter_api import active
from gdr.commands import (
    render_object_detail,
    render_objects,
    render_system,
    render_tasks,
)
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_detail,
    print_table,
    warn,
)
from rtthread.adapter import RtThreadAdapter
from rtthread.navigation import iter_object_names

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
# Singular forms render one object's vertical detail: ``rtt <object> <name>``.
_SINGULAR_COMMANDS = {
    "thread": "task",
    "timer": "timer",
    "semaphore": "semaphore",
    "mutex": "mutex",
    "event": "event",
    "mailbox": "mailbox",
    "messagequeue": "msgqueue",
    "mempool": "mempool",
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
    "heap": "Show system heap status and diagnostics",
}
_DETAIL_DESCRIPTIONS = {
    "thread": "Show one thread's detail (rtt thread <name>)",
    "timer": "Show one timer's detail (rtt timer <name>)",
    "semaphore": "Show one semaphore's detail (rtt semaphore <name>)",
    "mutex": "Show one mutex's detail (rtt mutex <name>)",
    "event": "Show one event's detail (rtt event <name>)",
    "mailbox": "Show one mailbox's detail (rtt mailbox <name>)",
    "messagequeue": "Show one message queue's detail (rtt messagequeue <name>)",
    "mempool": "Show one memory pool's detail (rtt mempool <name>)",
}
_USAGE = "usage: rtthread <command> (run 'rtt help' for available commands)"
_HELP = (
    "RT-Thread commands:\n"
    + "\n".join(
        f"  rtt {command:<14} {description}"
        for command, description in _COMMAND_DESCRIPTIONS.items()
    )
    + "\n\nSingle-object detail (rtt <object> <name>):\n"
    + "\n".join(
        f"  rtt {command:<14} {description}"
        for command, description in _DETAIL_DESCRIPTIONS.items()
    )
    + "\n\nAliases:\n"
    + "\n".join(
        f"  {alias:<10} -> {command}" for alias, command in _COMMAND_ALIASES.items()
    )
)


@gdb_command_guard
def render_heap() -> None:
    """Render the system-heap snapshot, block walk, and per-thread occupancy."""
    adapter = active()
    if not isinstance(adapter, RtThreadAdapter):
        warn("run `gdr init rtthread <version>` first")
        return
    pairs = adapter.heap_basic_pairs()
    diagnostics_data = adapter.heap_detail()
    if diagnostics_data is None:
        pairs += [("Blocks", "N/A"), ("Holes", "N/A"), ("Thread occupancy", "N/A")]
        print_detail(pairs)
        return
    pairs += diagnostics_data.pairs
    if diagnostics_data.occupancy is None:
        pairs.append(("Thread occupancy", "N/A"))
        print_detail(pairs)
        return
    pairs.append(("Thread occupancy", f"{len(diagnostics_data.occupancy)} threads"))
    print_detail(pairs)
    print_table(
        diagnostics_data.occupancy,
        ["Thread", "Blocks", "Bytes"],
        elastic=("Thread",),
    )


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one RT-Thread command without depending on GDB."""
    args = argument.split()
    if not args or (len(args) == 1 and args[0].lower() == "help"):
        print(_HELP)
        return
    command = _COMMAND_ALIASES.get(args[0].lower(), args[0].lower())
    if len(args) == 2 and command in _SINGULAR_COMMANDS:
        render_object_detail(_SINGULAR_COMMANDS[command], args[1])
    elif len(args) != 1:
        warn(_USAGE)
    elif command == "threads":
        render_tasks()
    elif command == "system":
        render_system()
    elif command == "heap":
        render_heap()
    elif command in _OBJECT_COMMANDS:
        render_objects(_OBJECT_COMMANDS[command])
    else:
        warn(_USAGE)


def _command_vocabulary() -> list[str]:
    """Return every word the first argument may complete against."""
    return list(_COMMAND_DESCRIPTIONS) + list(_SINGULAR_COMMANDS)


def _prefixes(word: str | None, candidates: list[str]) -> list[str]:
    """Return candidates that start with ``word``, preserving order.

    ``None`` or an empty word (GDB probes completion with ``word=None`` first)
    yields every candidate.
    """
    if not word:
        return list(candidates)
    return [candidate for candidate in candidates if candidate.startswith(word)]


def _object_names(kind: str) -> list[str]:
    """Return live object names of *kind* for tab completion.

    Traverses the active adapter's kernel registry so candidates reflect the
    objects on the connected target right now. Degrades to ``[]`` when no
    RT-Thread adapter is active or traversal fails, so tab completion never
    raises inside GDB.
    """
    adapter = active()
    if not isinstance(adapter, RtThreadAdapter):
        return []
    try:
        return list(iter_object_names(kind, adapter.layout))
    except Exception:
        # Reason: this is the GDB completion boundary (``complete()``), not a
        # command body. GDB completion must never raise or print -- the guard's
        # warn/err output would corrupt the readline prompt -- so we swallow
        # everything here and degrade to no candidates, as documented on
        # :func:`_object_names`.
        return []


def _complete(text: str, word: str | None) -> list[str]:
    """Return tab-completion candidates for a partial ``rtt`` command line.

    The first argument completes against the command vocabulary; the second
    argument of a singular detail command completes against live kernel
    object names. This is RT-Thread command-tree policy; the prefix filter is
    a private helper because nothing outside the adapter needs it.
    """
    parts = text.split()
    if not parts:
        return _prefixes(word, _command_vocabulary())
    command = _COMMAND_ALIASES.get(parts[0].lower(), parts[0].lower())
    if command in _SINGULAR_COMMANDS and " " in text:
        return _prefixes(word, _object_names(_SINGULAR_COMMANDS[command]))
    return _prefixes(word, _command_vocabulary())


if gdb is not None:

    class RtThreadCommand(gdb.Command):
        """RT-Thread command tree. Run `rtt help` for available commands."""

        def __init__(self) -> None:
            # Reason: do not pass a completer_class here. GDB only calls the
            # command's Python ``complete()`` method when no completer class is
            # given; passing gdb.COMPLETE_NONE explicitly means "no completion"
            # and would silently disable tab-completion of our subcommands and
            # live object names (GDB manual: "Command.complete").
            super().__init__("rtthread", gdb.COMMAND_USER)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            _invoke_command(argument)

        def complete(self, text: str, word: str | None) -> list[str]:
            """Tab-complete subcommands and live object names.

            Completing the second argument of a singular detail command walks
            the active adapter's kernel registry, so candidates reflect the
            objects that exist on the connected target right now.
            """
            return _complete(text, word)


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
