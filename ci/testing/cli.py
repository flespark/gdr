"""Public CLI: cache/setup, discovery, build, run, matrix and containers."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .cache import CACHE, initialize
from .matrix import GROUPS, SUPPORTED, matrix_build_only, select


def parser() -> argparse.ArgumentParser:
    """Describe public commands without creating caches or probing tools."""
    root = argparse.ArgumentParser(
        description="GDR integration tests: one cache, explicit firmware coordinates.",
        epilog="Examples: list; run freertos mps2-an385 11.1.0 base -- -k backtrace; "
        "container run -- matrix smoke. Use COMMAND --help for details.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    cache = commands.add_parser("cache", help="initialize or inspect ci/.cache")
    cache.add_argument("action", choices=("init", "path"))
    cache.add_argument(
        "--directory", type=Path, help="link an absent ci/.cache to this directory"
    )
    commands.add_parser(
        "setup", help="register installed tools in the platform-specific cache"
    )
    listing = commands.add_parser(
        "list", help="list supported firmware coordinates without running tools"
    )
    listing.add_argument("group", choices=GROUPS, nargs="?", default="all")
    for name in ("doctor", "build", "run"):
        cmd = commands.add_parser(
            name,
            help={
                "doctor": "verify tools for one lane",
                "build": "prepare one firmware cache cell",
                "run": "build if necessary, then run pytest",
            }[name],
        )
        cmd.add_argument("rtos", choices=("rtthread", "freertos", "bare"))
        cmd.add_argument("target")
        cmd.add_argument("version")
        cmd.add_argument("variant", nargs="?")
        cmd.add_argument(
            "--rebuild", action="store_true", help="ignore the firmware manifest"
        )
        cmd.add_argument(
            "--gdb12",
            action="store_true",
            help="require GDB 12 and embedded Python 3.10",
        )
        cmd.add_argument(
            "--gdb",
            default=None,
            help="override the GDB executable used by doctor and pytest",
        )
    matrix = commands.add_parser(
        "matrix", help="run a named matrix using the same lane runner"
    )
    matrix.add_argument("group", choices=GROUPS)
    matrix.add_argument("--rebuild", action="store_true")
    matrix.add_argument("--gdb12", action="store_true")
    matrix.add_argument(
        "--gdb",
        default=None,
        help="override the GDB executable used by doctor and pytest",
    )
    container = commands.add_parser(
        "container", help="manage cache-owned Podman environments"
    )
    container.add_argument("action", choices=("init", "build", "run", "stop"))
    container.add_argument(
        "--profile", choices=("qemu", "gdb12", "gdb-latest"), default="qemu"
    )
    container.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    container.add_argument(
        "--no-build", action="store_true", help="require an existing image"
    )
    container.add_argument(
        "--no-start",
        action="store_true",
        help="do not start or heal the CI Podman machine for build/run",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    """Dispatch CLI commands, forwarding only arguments after an explicit --."""
    args = list(sys.argv[1:] if argv is None else argv)
    forwarded: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, forwarded = args[:split], args[split + 1 :]
    cli = parser()
    options = cli.parse_args(args)
    if forwarded and options.command not in ("run", "matrix", "container"):
        cli.error("only run, matrix and container run accept arguments after --")
    try:
        if options.command == "cache":
            if options.action == "init":
                initialize(options.directory, prompt=options.directory is None)
            elif options.directory:
                cli.error("cache path does not accept --directory")
            print(f"{CACHE} -> {CACHE.resolve()}")
            return 0
        if options.command == "list":
            for lane in SUPPORTED if options.group == "all" else GROUPS[options.group]:
                print(lane.key)
            return 0
        if options.command == "container":
            from .containers import container_command

            if forwarded and options.action != "run":
                cli.error("only container run accepts a forwarded command")
            container_command(
                options.action,
                options.profile,
                options.platform,
                options.no_build,
                forwarded,
                no_start=options.no_start,
            )
            return 0
        from .runner import build, doctor, preflight, register_tools, run

        if options.command == "setup":
            register_tools()
            return 0
        lanes = (
            GROUPS[options.group]
            if options.command == "matrix"
            else (
                select(options.rtos, options.target, options.version, options.variant),
            )
        )
        initialize(prompt=True)
        register_tools()
        compatibility = options.gdb12 or getattr(options, "group", None) == "gdb12"
        gdb_kw = {"gdb": options.gdb} if getattr(options, "gdb", None) else {}
        if options.command == "matrix":
            preflight(
                [lane for lane in lanes if forwarded or not matrix_build_only(lane)],
                compatibility=compatibility,
                **gdb_kw,
            )
        for lane in lanes:
            if options.command == "doctor":
                doctor(lane, compatibility=options.gdb12, **gdb_kw)
            elif options.command == "build" or (
                options.command == "matrix"
                and not forwarded
                and matrix_build_only(lane)
            ):
                build(lane, rebuild=options.rebuild)
            else:
                run(
                    lane,
                    forwarded,
                    rebuild=options.rebuild,
                    compatibility=compatibility,
                    check_tools=options.command != "matrix",
                    **gdb_kw,
                )
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[gdr-ci] FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
