#!/usr/bin/env python3
"""GDR — GDB helper framework for debugging RTOS-based embedded firmware.

Usage in GDB::

    (gdb) source gdr.py --rtos rtthread --version 4.0
    (gdb) rtthread threads
    (gdb) p *$gdr_thread("main")

This entry point parses the ``--rtos`` / ``--version`` arguments, loads the
corresponding RTOS adapter package, probes kernel configuration by symbol
presence, builds the layout, and registers pretty-printers, convenience
functions and aggregate commands.  No RTOS auto-detection is performed.
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
    """Parse ``--rtos`` and ``--version`` from argv or environment variables.

    GDB's ``source`` command does not pass command-line arguments to the
    sourced script.  Therefore we support two mechanisms:

    1. **Environment variables** ``GDR_RTOS`` and ``GDR_VERSION`` — the
       primary mechanism for both pytest-driven tests and GDB ``source``.
    2. **sys.argv** — kept for CLI usage outside GDB.

    Returns:
        Dict with keys ``"rtos"`` and ``"version"``.
    """
    args: dict[str, str] = {}

    # Environment variables (primary mechanism)
    env_rtos = os.environ.get("GDR_RTOS", "")
    env_version = os.environ.get("GDR_VERSION", "")
    if env_rtos:
        args["rtos"] = env_rtos
    if env_version:
        args["version"] = env_version

    # sys.argv overrides (secondary mechanism)
    argv = sys.argv[1:] if len(sys.argv) > 1 else []
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
    source gdr.py --rtos <name> --version <ver>

Options:
    --rtos <name>      RTOS name (currently: rtthread)
    --version <ver>    Major version (e.g. 4.0)
    --help             Show this help

Examples:
    source gdr.py --rtos rtthread --version 4.0
"""
    )


def _setup_rtthread(version: str) -> None:
    """Initialise RT-Thread support: probe config, build layout, register all.

    Args:
        version: Major version string (e.g. ``"4.0"``).
    """
    from rtthread.adapter import register_adapter
    from rtthread.commands import register_commands
    from rtthread.layout import build_layouts, detect_config

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
        version: Major version string.
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

    args = _parse_args()
    rtos = args.get("rtos", "")
    version = args.get("version", "")

    if not rtos:
        warn("--rtos not specified")
        _print_usage()
        raise SystemExit(1)
    if not version:
        warn("--version not specified")
        _print_usage()
        raise SystemExit(1)

    _setup_rtos(rtos, version)


# GDB sources this file as a script, so __name__ is "__main__" when loaded
# via `source gdr.py`.  When imported as a module (for testing), we skip
# auto-initialisation.
if __name__ == "__main__":
    initialize()
