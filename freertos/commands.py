"""FreeRTOS's RTOS-specific aggregate GDB commands."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.adapter import FreeRtosAdapter, iter_task_names
from freertos.help_docs import build_help_tree
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
from gdr.help import (
    command_aliases,
    command_topics,
    find_topic,
    render_terminal,
)

_command_registered = False
_alias_registered = False

_HELP_TREE = build_help_tree()
_COMMANDS = {topic.name: topic for topic in command_topics(_HELP_TREE)}
_COMMAND_ALIASES = command_aliases(_HELP_TREE)
_SINGULAR_COMMANDS = {
    topic.name: topic.kind for topic in _COMMANDS.values() if topic.action == "detail"
}
# Compatibility views for callers that inspect the public command vocabulary.
_COMMAND_DESCRIPTIONS = {
    "help": "Show this help",
    **{
        name: topic.summary
        for name, topic in _COMMANDS.items()
        if topic.action != "detail"
    },
}
_DETAIL_DESCRIPTIONS = {
    name: topic.summary for name, topic in _COMMANDS.items() if topic.action == "detail"
}
_USAGE = "usage: freertos <command> (run 'frt help <topic>' for detailed help)"
_HELP = render_terminal(_HELP_TREE)


def _help_tree():
    """Return docs enriched with the active adapter's concrete printer layout."""
    adapter = active()
    layout = adapter.layout if isinstance(adapter, FreeRtosAdapter) else None
    return build_help_tree(layout)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one FreeRTOS command without depending on GDB."""
    args = argument.split()
    if not args:
        print(render_terminal(_help_tree()))
        return
    if args[0].lower() == "help":
        path = tuple(args[1:])
        tree = _help_tree()
        if path and find_topic(tree, path) is None:
            warn(_USAGE)
            return
        print(render_terminal(tree, path))
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
    elif command in _COMMANDS and _COMMANDS[command].action == "tasks":
        render_tasks()
    elif command in _COMMANDS and _COMMANDS[command].action == "system":
        render_system()
    elif command in _COMMANDS and _COMMANDS[command].action == "objects":
        render_objects(_COMMANDS[command].kind)
    elif command in _COMMANDS and _COMMANDS[command].action == "heap":
        render_heap()
    else:
        warn(_USAGE)


def _command_vocabulary() -> list[str]:
    """Return every word the first argument may complete against."""
    return ["help", *list(_COMMANDS), "functions", "pretty-printers"]


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
            help_topics=[topic.name for topic in _HELP_TREE.topics],
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
