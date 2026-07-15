#!/usr/bin/env python3
"""GDR — GDB helper framework for debugging RTOS-based embedded firmware.

Usage in GDB::

    (gdb) source gdr.py
    (gdb) gdr rtthread 4.0.5
    (gdb) rtthread threads
    (gdb) p *$gdr_thread("main")

This entry point parses the ``--rtos`` / ``--version`` arguments, loads the
corresponding RTOS adapter package, probes kernel configuration by symbol
presence, builds the layout, and registers pretty-printers, convenience
functions and aggregate commands.  No RTOS auto-detection is performed.
"""

from __future__ import annotations

import os
import shlex
import sys

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import info, warn
from gdr.printers import register_printers


def _parse_args() -> dict[str, str]:
    """Parse ``--rtos`` and ``--version`` from supported startup inputs.

    GDB's ``source`` command does not pass command-line arguments to the
    sourced script.  Therefore we support three mechanisms:

    1. **Environment variables** ``GDR_RTOS`` and ``GDR_VERSION`` — fallback
       for non-interactive launchers.
    2. **GDB inferior args** via ``set args --rtos ... --version ...`` — the
       main interactive GDB path.
    3. **sys.argv** — kept for CLI usage outside GDB.

    Returns:
        Dict with keys ``"rtos"`` and ``"version"``.
    """
    args: dict[str, str] = {}

    # Environment variables (fallback mechanism)
    env_rtos = os.environ.get("GDR_RTOS", "")
    env_version = os.environ.get("GDR_VERSION", "")
    if env_rtos:
        args["rtos"] = env_rtos
    if env_version:
        args["version"] = env_version

    # GDB inferior args (common interactive path):
    #   (gdb) set args --rtos rtthread --version 4.0.5
    #   (gdb) source gdr.py
    if gdb is not None:
        gdb_args = _gdb_inferior_args()
        args.update(_parse_argv(gdb_args))

    # sys.argv overrides (secondary mechanism)
    argv = sys.argv[1:] if len(sys.argv) > 1 else []
    args.update(_parse_argv(argv))
    return args


def _gdb_inferior_args() -> list[str]:
    """Return args configured by GDB's ``set args`` command."""
    if gdb is None:
        return []
    try:
        output = gdb.execute("show args", to_string=True)
    except gdb.error:
        return []

    marker = ' is "'
    start = output.find(marker)
    if start < 0:
        return []
    value = output[start + len(marker) :].rstrip()
    if value.endswith('".'):
        value = value[:-2]
    try:
        return shlex.split(value)
    except ValueError:
        return []


def _parse_argv(argv: list[str]) -> dict[str, str]:
    """Parse GDR options from an argv-style list."""
    args: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--rtos", "-r"):
            i += 1
            if i < len(argv):
                args["rtos"] = argv[i]
        elif arg in ("--version", "-v"):
            i += 1
            if i < len(argv):
                args["version"] = argv[i]
        elif arg in ("--help", "-h"):
            _print_usage()
            raise SystemExit(0)
        i += 1
    return args


def _print_usage() -> None:
    """Print usage information."""
    print(
        """
GDR — GDB helper for RTOS debugging

Usage:
    source gdr.py
    gdr <rtos> <version>

Automation alternatives:
    set args --rtos <name> --version <ver>; source gdr.py
    GDR_RTOS=<name> GDR_VERSION=<ver> gdb ... -ex 'source gdr.py'

Options:
    --rtos <name>      RTOS name (currently: rtthread)
    --version <ver>    RTOS version (e.g. 4.0.5)
    --help             Show this help

Examples:
    source gdr.py
    gdr rtthread 4.0.5
"""
    )


def _parse_init_args(argv: list[str]) -> dict[str, str]:
    """Parse arguments accepted by the interactive ``gdr`` command."""
    if argv and argv[0] == "init":
        argv = argv[1:]
    if len(argv) == 2 and not argv[0].startswith("-"):
        return {"rtos": argv[0], "version": argv[1]}
    return _parse_argv(argv)


_GdbCommandBase = gdb.Command if gdb is not None else object


class GdrCommand(_GdbCommandBase):  # type: ignore[misc]
    """Interactive GDR bootstrap command."""

    def __init__(self) -> None:
        if gdb is None:
            return
        super().__init__("gdr", gdb.COMMAND_USER)

    def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
        argv = shlex.split(argument)
        if not argv or argv[0] in ("help", "--help", "-h"):
            _print_usage()
            return

        args = _parse_init_args(argv)
        rtos = args.get("rtos", "")
        version = args.get("version", "")
        if not rtos or not version:
            warn("usage: gdr <rtos> <version>")
            _print_usage()
            return
        _setup_rtos(rtos, version)


def _setup_rtthread(version: str) -> None:
    """Initialise RT-Thread support: probe config, build layout, register all.

    Args:
        version: Full RT-Thread version string (e.g. ``"4.0.5"``).
    """
    from rtthread.adapter import register_adapter
    from rtthread.commands import register_commands
    from rtthread.layout import build_layouts, detect_config
    from rtthread.version import check_version

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
        info("GDR loaded. Run `gdr rtthread 4.0.5` to initialise RT-Thread support.")
        return
    if not rtos or not version:
        warn("both --rtos and --version are required for automatic initialisation")
        _print_usage()
        return

    _setup_rtos(rtos, version)


# GDB sources this file as a script, so __name__ is "__main__" when loaded
# via `source gdr.py`.  When imported as a module (for testing), we skip
# auto-initialisation.
if __name__ == "__main__":
    initialize()
