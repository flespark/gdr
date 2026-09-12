#!/usr/bin/env python3
"""GDR — GDB helper framework for debugging RTOS-based embedded firmware.

Usage in GDB::

    (gdb) source gdr.py
    (gdb) gdr init rtthread 4.0.5
    (gdb) rtt threads
    (gdb) p $gdr_task("main")

This entry point loads the requested RTOS adapter package, probes kernel
configuration by symbol presence, builds the layout, and registers
pretty-printers, convenience functions and the selected RTOS command tree.
No RTOS auto-detection is performed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

# Reason: GDB's Python interpreter does not add the script directory to
# sys.path, so `source gdr.py` from an arbitrary cwd would fail on the first
# `import gdr.*`. Prepend this file's directory so the project packages are
# importable without requiring PYTHONPATH or a cwd change.
_GDR_ROOT = os.path.dirname(os.path.abspath(__file__))
if _GDR_ROOT not in sys.path:
    sys.path.insert(0, _GDR_ROOT)

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import gdb_command_guard, info, warn  # noqa: E402
from gdr.help import (  # noqa: E402
    CommandHelp,
    HelpField,
    HelpTopic,
    HelpTree,
    find_topic,
    pretty_printer_topic,
    render_terminal,
)
from gdr.printers import register_printers  # noqa: E402


def _root_help_tree() -> HelpTree:
    """Build RTOS-neutral bootstrap and reference help."""
    topics: list[HelpTopic] = [
        CommandHelp(
            "init",
            "Initialize one RTOS adapter",
            "Selects an RTOS/version, probes configuration, builds layouts and registers commands, functions and pretty-printers.",
            usage=("gdr init <rtos> <version>",),
            fields=(
                HelpField("rtos", "rtthread/rtt or freertos/frt."),
                HelpField(
                    "version", "Exact supported kernel version declared by the user."
                ),
            ),
            tips=(
                "Use `rtt help` or `frt help` after initialization.",
                "Restart GDB before selecting a different adapter or version.",
            ),
            configuration=(
                "Kernel features are probed from symbols and DWARF; the RTOS itself is never auto-detected.",
            ),
            limitations=(
                "The target ELF needs debug symbols; GDR never calls inferior functions.",
            ),
            action="init",
            category="Available command",
        ),
        HelpTopic(
            "rtthread",
            "Continue with the registered `rtt` command tree",
            "After RT-Thread initialization, use `rtt help <topic>` for command fields, tips, configuration and limitations.",
            usage=("rtt help", "rtt help <topic>"),
            category="RTOS command tree",
        ),
        HelpTopic(
            "freertos",
            "Continue with the registered `frt` command tree",
            "After FreeRTOS initialization, use `frt help <topic>` for command fields, tips, configuration and limitations.",
            usage=("frt help", "frt help <topic>"),
            category="RTOS command tree",
        ),
        replace(
            pretty_printer_topic(None),
            summary="Explain automatic one-line formatting of kernel structs",
            description=(
                "GDR's core pretty-printer mechanism is RTOS-neutral and uses the "
                "active adapter's layout metadata. For concrete registered types and "
                "fields, run `rtt help pretty-printers` or "
                "`frt help pretty-printers`."
            ),
            tips=(
                "After initialization, use the selected RTOS help tree for concrete printer subtopics.",
            ),
            children=(),
            category="Help topic",
        ),
    ]
    return HelpTree(
        "gdr",
        "GDR help",
        "Bootstrap and documentation entry point for GDR's help trees.",
        tuple(topics),
        category_descriptions=(
            (
                "Available command",
                "Executable `gdr` subcommands. Run one directly at the GDB prompt.",
            ),
            (
                "RTOS command tree",
                "The matching command tree is registered by `gdr init`; run `rtt help` or `frt help` to browse its commands.",
            ),
            (
                "Help topic",
                "Documentation only, not an executable GDB subcommand. Open it with `gdr help <topic>`.",
            ),
        ),
    )


def _parse_args() -> dict[str, str]:
    """Parse automatic initialisation arguments from the environment.

    ``GDR_RTOS`` and ``GDR_VERSION`` allow non-interactive launchers to source
    GDR and initialise its RTOS adapter in one step.  Interactive sessions
    use ``gdr init`` instead.

    Returns:
        Dict with keys ``"rtos"`` and ``"version"``.
    """
    args: dict[str, str] = {}

    env_rtos = os.environ.get("GDR_RTOS", "")
    env_version = os.environ.get("GDR_VERSION", "")
    if env_rtos:
        args["rtos"] = env_rtos
    if env_version:
        args["version"] = env_version

    return args


def _print_usage(path: tuple[str, ...] = ()) -> None:
    """Print structured bootstrap help or one nested topic."""
    print(render_terminal(_root_help_tree(), path))


def _complete(text: str, word: str | None) -> list[str]:
    """Tab-complete bootstrap subcommands and structured help topics."""
    from gdr.commands import prefix_candidates

    parts = text.split()
    if parts and parts[0].lower() == "help" and " " in text:
        return prefix_candidates(
            word, [topic.name for topic in _root_help_tree().topics]
        )
    return prefix_candidates(word, ["init", "help"])


_GdbCommandBase = gdb.Command if gdb is not None else object


class GdrCommand(_GdbCommandBase):  # type: ignore[misc]
    """Interactive GDR bootstrap command."""

    def __init__(self) -> None:
        if gdb is None:
            return
        super().__init__("gdr", gdb.COMMAND_USER)

    def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
        _invoke_command(argument)

    def complete(self, text: str, word: str | None) -> list[str]:
        """Complete root commands and help topics without touching target state."""
        return _complete(text, word)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one ``gdr`` bootstrap command, guarded at the edge.

    A guarded entry means an unexpected failure in argument parsing or RTOS
    setup surfaces as a single ``[gdr] error`` diagnostic instead of raw GDB
    "Python Exception" noise.  Init-time policy failures (unknown RTOS,
    invalid/unsupported version, declared/target mismatch) warn and abort
    the init: ``SystemExit`` from a GDB command kills the whole session,
    which a typo must never do.
    """
    argv = gdb.string_to_argv(argument) if gdb is not None else argument.split()
    if not argv or argv[0] in ("--help", "-h"):
        _print_usage()
        return
    if argv[0].lower() == "help":
        path = tuple(argv[1:])
        tree = _root_help_tree()
        if path and find_topic(tree, path) is None:
            warn("usage: gdr help [topic]")
            return
        _print_usage(path)
        return

    if len(argv) != 3 or argv[0] != "init":
        warn("usage: gdr init <rtos> <version> (run 'gdr help' for help)")
        return
    _setup_rtos(argv[1].lower(), argv[2])


def _setup_rtthread(version: str) -> None:
    """Initialise RT-Thread support once for the current GDB session.

    Args:
        version: Full RT-Thread version string (e.g. ``"4.0.5"``).
    """
    from gdr.adapter_api import is_initialized, register
    from gdr.functions import register_functions
    from rtthread.adapter import RtThreadAdapter
    from rtthread.commands import register_commands
    from rtthread.layout import build_layouts, detect_config
    from rtthread.version import check_version

    if is_initialized():
        warn(
            "RT-Thread support is already initialized; restart GDB before "
            "selecting a different target or version"
        )
        return

    target_version = check_version(version)
    if target_version is None:
        info(f"invalid RT-Thread version: {version!r}")
        return
    info(f"setting up RT-Thread v{version}...")
    cfg = detect_config()
    info(
        f"  config: smp={cfg.smp} heap={cfg.heap_type} "
        f"sem={cfg.using_semaphore} mutex={cfg.using_mutex} "
        f"mb={cfg.using_mailbox} mq={cfg.using_messagequeue}"
    )
    kl = build_layouts(cfg, target_version)
    info(f"  layout: {len(kl.structs)} structs, {len(kl.list_hooks)} list hooks")
    adapter = RtThreadAdapter(kl, heap_type=cfg.heap_type)

    register_printers(kl)
    register_functions()
    register_commands()
    register(adapter)

    info("RT-Thread support ready. Type 'rtt help' for commands.")


def _setup_freertos(version: str) -> None:
    """Initialise FreeRTOS support for the current GDB target."""
    from freertos.adapter import FreeRtosAdapter
    from freertos.commands import register_commands
    from freertos.layout import build_layout, detect_config
    from freertos.version import check_version
    from gdr.adapter_api import is_initialized, register
    from gdr.functions import register_functions

    if is_initialized():
        warn(
            "an RTOS adapter is already initialized; restart GDB before "
            "selecting a different target or version"
        )
        return

    target_version = check_version(version)
    if target_version is None:
        info(f"invalid FreeRTOS version: {version!r}")
        return
    info(f"setting up FreeRTOS v{version}...")
    cfg = detect_config()
    info(
        f"  config: smp={cfg.smp} cores={cfg.number_of_cores} "
        f"tick={cfg.tick_bits} notifications={cfg.notification_count}"
    )
    layout = build_layout(cfg, target_version)
    info(f"  layout: {len(layout.structs)} structs")
    adapter = FreeRtosAdapter(layout)
    register_printers(layout)
    register_functions()
    register_commands()
    register(adapter)
    info("FreeRTOS support ready. Type 'freertos help' (alias: frt) for commands.")


def _setup_rtos(rtos: str, version: str) -> None:
    """Dispatch to the appropriate RTOS setup function.

    Args:
        rtos: RTOS name (e.g. ``"rtthread"``).
        version: Full RTOS version string.
    """
    if rtos == "rtthread" or rtos == "rt-thread" or rtos == "rtt":
        _setup_rtthread(version)
    elif rtos == "freertos" or rtos == "frt":
        _setup_freertos(version)
    else:
        warn(f"unsupported RTOS: {rtos!r}")
        warn("currently supported: rtthread, freertos")


def initialize() -> None:
    """Entry point: parse args and initialise the requested RTOS support."""
    if gdb is None:
        print("GDR must be sourced inside GDB.", file=sys.stderr)
        raise SystemExit(1)

    GdrCommand()
    args = _parse_args()
    rtos = args.get("rtos", "")
    version = args.get("version", "")

    if not rtos and not version:
        info("GDR loaded. Run `gdr init <rtos> <version>` to initialise support.")
        return
    if not rtos or not version:
        warn("GDR_RTOS and GDR_VERSION are both required for automatic initialisation")
        _print_usage()
        return

    from gdr.adapter_api import is_initialized

    _setup_rtos(rtos, version)
    if not is_initialized():
        info("GDR loaded without an active adapter; run `gdr init <rtos> <version>`.")


# GDB sources this file as a script, so __name__ is "__main__" when loaded
# via `source gdr.py`.  When imported as a module (for testing), we skip
# auto-initialisation.
if __name__ == "__main__":
    initialize()
