"""RT-Thread's RTOS-specific command tree."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

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
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    warn,
)
from gdr.help import command_aliases, command_topics, find_topic, render_terminal
from rtthread.adapter import RtThreadAdapter
from rtthread.help_docs import build_help_tree
from rtthread.navigation import iter_object_names

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
_USAGE = "usage: rtthread <command> (run 'rtt help <topic>' for detailed help)"
_HELP = render_terminal(_HELP_TREE)


def _help_tree():
    """Return docs enriched with the active adapter's concrete printer layout."""
    adapter = active()
    layout = adapter.layout if isinstance(adapter, RtThreadAdapter) else None
    return build_help_tree(layout)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one RT-Thread command without depending on GDB."""
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
    if len(args) == 2 and command in _SINGULAR_COMMANDS:
        render_object_detail(_SINGULAR_COMMANDS[command], args[1])
    elif len(args) != 1:
        warn(_USAGE)
    elif command in _COMMANDS and _COMMANDS[command].action == "tasks":
        render_tasks()
    elif command in _COMMANDS and _COMMANDS[command].action == "objects":
        render_objects(_COMMANDS[command].kind)
    elif command in _COMMANDS and _COMMANDS[command].action == "system":
        render_system()
    elif command in _COMMANDS and _COMMANDS[command].action == "heap":
        render_heap()
    else:
        warn(_USAGE)


def _command_vocabulary() -> list[str]:
    """Return every word the first argument may complete against."""
    return ["help", *list(_COMMANDS), "functions", "pretty-printers"]


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
    object names.  Mechanics are shared with the FreeRTOS tree through
    :func:`gdr.commands.complete_command_tree`.
    """
    return complete_command_tree(
        CommandTreeSpec(
            _command_vocabulary(),
            _COMMAND_ALIASES,
            _SINGULAR_COMMANDS,
            _object_names,
            help_topics=[topic.name for topic in _HELP_TREE.topics],
        ),
        text,
        word,
    )


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
