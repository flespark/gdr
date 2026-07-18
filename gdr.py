#!/usr/bin/env python3
"""GDR — GDB helper framework for debugging RTOS-based embedded firmware.

Usage in GDB::

    (gdb) source gdr.py
    (gdb) gdr init rtthread 4.0.5
    (gdb) rtthread threads
    (gdb) p *$gdr_thread("main")

This entry point loads the requested RTOS adapter package, probes kernel
configuration by symbol presence, builds the layout, and registers
pretty-printers, convenience functions and aggregate commands.  No RTOS
auto-detection is performed.
"""

from __future__ import annotations

import os
import sys

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import info, warn
from gdr.printers import register_printers


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


def _print_usage() -> None:
    """Print usage information."""
    print(
        """
GDR — GDB helper for RTOS debugging

Usage:
    source gdr.py
    gdr init <rtos> <version>

Automation:
    GDR_RTOS=<name> GDR_VERSION=<ver> gdb ... -ex 'source gdr.py'

Examples:
    source gdr.py
    gdr init rtthread 4.0.5
"""
    )


_GdbCommandBase = gdb.Command if gdb is not None else object


class GdrCommand(_GdbCommandBase):  # type: ignore[misc]
    """Interactive GDR bootstrap command."""

    def __init__(self) -> None:
        if gdb is None:
            return
        super().__init__("gdr", gdb.COMMAND_USER)

    def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
        argv = gdb.string_to_argv(argument)
        if not argv or argv[0] in ("help", "--help", "-h"):
            _print_usage()
            return

        if len(argv) != 3 or argv[0] != "init":
            warn("usage: gdr init <rtos> <version>")
            _print_usage()
            return
        _setup_rtos(argv[1], argv[2])


def _setup_rtthread(version: str) -> None:
    """Initialise RT-Thread support once for the current GDB session.

    Args:
        version: Full RT-Thread version string (e.g. ``"4.0.5"``).
    """
    from rtthread.adapter import register_adapter
    from rtthread.commands import is_initialized, register_commands
    from rtthread.layout import build_layouts, detect_config
    from rtthread.version import check_version

    if is_initialized():
        warn(
            "RT-Thread support is already initialized; restart GDB before "
            "selecting a different target or version"
        )
        return

    check_version(version)
    info(f"setting up RT-Thread v{version}...")
    cfg = detect_config()
    info(
        f"  config: smp={cfg.smp} heap={cfg.heap_type} "
        f"sem={cfg.using_semaphore} mutex={cfg.using_mutex} "
        f"mb={cfg.using_mailbox} mq={cfg.using_messagequeue}"
    )
    kl = build_layouts(cfg)
    info(f"  layout: {len(kl.structs)} structs, {len(kl.list_hooks)} list hooks")

    register_printers(kl)
    register_adapter(kl)
    register_commands(kl)

    info("RT-Thread support ready. Type 'rtthread help' for commands.")


def _setup_rtos(rtos: str, version: str) -> None:
    """Dispatch to the appropriate RTOS setup function.

    Args:
        rtos: RTOS name (e.g. ``"rtthread"``).
        version: Full RTOS version string.
    """
    if rtos == "rtthread":
        _setup_rtthread(version)
    else:
        warn(f"unsupported RTOS: {rtos!r}")
        warn("currently supported: rtthread")
        raise SystemExit(1)


def initialize() -> None:
    """Entry point: parse args and initialise the requested RTOS support."""
    if gdb is None:
        print("GDR must be sourced inside GDB.", file=sys.stderr)
        raise SystemExit(1)

    GdrCommand()
    from rtthread.commands import register_command_shell

    register_command_shell()
    args = _parse_args()
    rtos = args.get("rtos", "")
    version = args.get("version", "")

    if not rtos and not version:
        info(
            "GDR loaded. Run `gdr init rtthread 4.0.5` to initialise RT-Thread support."
        )
        return
    if not rtos or not version:
        warn("GDR_RTOS and GDR_VERSION are both required for automatic initialisation")
        _print_usage()
        return

    _setup_rtos(rtos, version)


# GDB sources this file as a script, so __name__ is "__main__" when loaded
# via `source gdr.py`.  When imported as a module (for testing), we skip
# auto-initialisation.
if __name__ == "__main__":
    initialize()
