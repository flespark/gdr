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

from gdr.gdb_bridge import (  # noqa: E402
    gdb_command_guard,
    info,
    print_table,
    warn,
)
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
from gdr.version import TargetVerdict  # noqa: E402


def _root_help_tree() -> HelpTree:
    """Build RTOS-neutral bootstrap and reference help."""
    topics: list[HelpTopic] = [
        CommandHelp(
            "init",
            "Initialize one RTOS adapter",
            "Selects an RTOS/version, probes configuration, builds layouts and registers commands, functions and pretty-printers.",
            usage=("gdr init <rtos> <version> [--force] [--allow-unverified]",),
            fields=(
                HelpField("rtos", "rtthread/rtt or freertos/frt."),
                HelpField(
                    "version",
                    "Exact supported kernel version, or `auto` to use `gdr probe`'s \
unambiguous result.",
                ),
            ),
            tips=(
                "Use `rtt help` or `frt help` after initialization.",
                "`gdr init <rtos> <version> --force` replaces the active adapter without restarting GDB.",
                "`--allow-unverified` registers even when no RTOS symbol matched the target (a modified or stripped kernel); the session then repeats the override in every API envelope.",
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
        CommandHelp(
            "probe",
            "Report which RTOS adapter this target matches",
            "Runs every adapter's presence probe and prints its evidence; the adapter is never selected for you.",
            usage=("gdr probe",),
            tips=(
                "`gdr init <rtos> auto` uses this evidence when exactly one version is unambiguous.",
                "A candidate without an exact version needs a manual choice; build with -g3 to export the version constants.",
            ),
            configuration=(
                "Probing reads target symbols and DWARF only; it registers nothing and drives nothing.",
            ),
            action="probe",
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


def _report_unknown_command(token: str) -> None:
    """Report an unrecognized ``gdr`` subcommand without blaming initialization.

    Reason: every unknown word used to print the ``gdr init`` usage, so a user who
    spelled a kernel object as a ``gdr`` subcommand (``gdr heap``) read the message
    as "this session is not initialized" and went looking in the wrong place, while
    the session was in fact initialized and the command was simply not GDR's.
    Kernel state is listed by the RTOS command tree, which is a different GDB
    command, and a documented ``gdr`` topic is not a command at all.
    """
    from gdr.adapter_api import session_meta

    documented = {topic.name for topic in _root_help_tree().topics}
    if token.lower() in documented:
        warn(f"'{token}' is a help topic, not a command; run 'gdr help {token}'")
        return
    commands = "init, probe, reset, api, help"
    rtos, _version = session_meta()
    if rtos is None:
        warn(
            f"unknown command '{token}'; gdr takes {commands} -- kernel state is "
            f"listed by the RTOS command tree, so initialize an adapter with "
            f"'gdr init <rtos> <version>' and then run '<rtos> {token}'"
        )
        return
    warn(
        f"unknown command '{token}'; gdr takes {commands} -- kernel state is "
        f"listed by the active RTOS command tree, so try '{rtos} {token}' "
        f"or '{rtos} help'"
    )


def _complete(text: str, word: str | None) -> list[str]:
    """Tab-complete bootstrap subcommands and structured help topics."""
    from gdr.commands import prefix_candidates

    parts = text.split()
    if parts and parts[0].lower() == "help" and " " in text:
        return prefix_candidates(
            word, [topic.name for topic in _root_help_tree().topics]
        )
    return prefix_candidates(word, ["init", "probe", "reset", "api", "help"])


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
    if argv[0] == "api":
        _api_command(argv[1:])
        return
    if argv[0] == "probe":
        _probe_command(argv[1:])
        return
    if argv[0] == "reset":
        _reset_command()
        return
    if argv[0] != "init":
        _report_unknown_command(argv[0])
        return
    _init_command(argv[1:])


def _init_command(rest: list[str]) -> None:
    """``gdr init <rtos> <version> [--force] [--allow-unverified] [--json]``.

    ``--force`` replaces an already initialized adapter instead of refusing.
    ``--allow-unverified`` registers even when no RTOS symbol matched the
    target, for a deliberately modified or stripped kernel; the override is
    recorded on the session and repeated in every later API envelope, so a
    client shows it instead of silently trusting empty reads.
    ``--json`` is the machine channel a plugin uses to read one structured
    result instead of parsing log lines, so it is not advertised in help.
    """
    from gdr.adapter_api import is_initialized
    from gdr.constants import GDR_API_SCHEMA_VERSION

    force = "--force" in rest
    allow_unverified = "--allow-unverified" in rest
    json_mode = "--json" in rest
    positional = [token for token in rest if not token.startswith("--")]
    if len(positional) != 2:
        warn("usage: gdr init <rtos> <version> [--force] [--allow-unverified]")
        return
    rtos, version = positional[0].lower(), positional[1]
    if version == "auto":
        resolved = _resolve_auto(rtos)
        if resolved is None:
            _init_refusal(
                json_mode,
                rtos,
                None,
                "auto_unresolved",
                "cannot resolve `auto`: run `gdr probe` for the evidence and "
                "pass the version explicitly",
            )
            return
        version = resolved
        if not json_mode:
            info(f"resolved {rtos} version {version} from the probe")
    if is_initialized() and not force:
        _init_refusal(
            json_mode,
            rtos,
            version,
            "already_initialized",
            "an RTOS adapter is already initialized; add --force to replace it",
        )
        return
    if force and is_initialized():
        _reset_command()
    verdict = _setup_rtos(rtos, version, allow_unverified=allow_unverified)
    if not json_mode:
        return
    result: dict[str, object] = {
        "schema_version": GDR_API_SCHEMA_VERSION,
        "ok": verdict.accepted,
        "rtos": rtos,
        "version": version,
        "verified": verdict.accepted and verdict.note is None,
        "markers": list(verdict.markers),
    }
    if verdict.note is not None:
        result["warning"] = verdict.note
    if not verdict.accepted:
        result["error"] = {"code": verdict.code, "message": verdict.message}
    _write_json(result)


def _init_refusal(
    json_mode: bool, rtos: str, version: str | None, code: str, message: str
) -> None:
    """Report one refusal: a coded line in ``--json`` mode, else a warning.

    The error object matches the API envelope's shape, so a client decodes one
    error form across ``gdr init --json`` and ``$gdr_json``.
    """
    if not json_mode:
        warn(message)
        return
    from gdr.constants import GDR_API_SCHEMA_VERSION

    _write_json(
        {
            "schema_version": GDR_API_SCHEMA_VERSION,
            "ok": False,
            "rtos": rtos,
            "version": version,
            "error": {"code": code, "message": message},
        }
    )


def _write_json(document: dict[str, object]) -> None:
    """Print one compact JSON document to GDB stdout."""
    import json

    if gdb is None:
        print(json.dumps(document, separators=(",", ":")))
        return
    gdb.write(json.dumps(document, separators=(",", ":")) + "\n")


def _api_command(argv: list[str]) -> None:
    """``gdr api <verb> [args...] [--json] [--out <path>]``.

    Default output is pretty-printed JSON for a human at the prompt;
    ``--json`` is compact and ``--out`` writes a file a plugin reads back.
    """
    import json

    from gdr.api import envelope_json

    out_path: str | None = None
    compact = False
    verb_args: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--json":
            compact = True
        elif token == "--out":
            if index + 1 >= len(argv):
                warn("gdr api --out requires a path")
                return
            out_path = argv[index + 1]
            index += 1
        else:
            verb_args.append(token)
        index += 1
    if not verb_args:
        warn("usage: gdr api <verb> [args...] [--json] [--out <path>]")
        return
    verb, *args = verb_args
    document = envelope_json(verb, *args)
    if out_path is not None:
        try:
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(document)
        except OSError as exc:
            warn(f"gdr api --out {out_path}: {exc}")
            return
        info(f"gdr api {verb}: wrote {len(document)} bytes to {out_path}")
        return
    if not compact:
        parsed = None
        try:
            parsed = json.loads(document)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            document = json.dumps(parsed, indent=2)
    if gdb is None:
        print(document)
        return
    gdb.write(document + "\n")


def _probe_command(argv: list[str]) -> None:
    """``gdr probe [--json]``: report which adapter this target matches.

    Reporting only -- the adapter is never selected here (see the architecture
    decision on auto-detection).  ``--json`` is the machine channel a client
    reads; the default is the same evidence as a human table.
    """
    from gdr.api import api_call

    unknown = [token for token in argv if token != "--json"]
    if unknown:
        warn("usage: gdr probe [--json]")
        return
    envelope = api_call("probe")
    if "--json" in argv:
        _write_json(envelope)
        return

    data = envelope.get("data")
    candidates = data.get("candidates", []) if isinstance(data, dict) else []
    warnings = envelope.get("warnings")
    if isinstance(warnings, list):
        for note in warnings:
            info(str(note))
    if not candidates:
        return
    rows = [
        [
            str(candidate.get("rtos", "")),
            str(candidate.get("version") or "N/A"),
            str(candidate.get("confidence", "")),
            "; ".join(str(item) for item in candidate.get("evidence", [])),
        ]
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    print_table(
        rows, ["RTOS", "Version", "Confidence", "Evidence"], elastic=("Evidence",)
    )
    resolved = data.get("resolved") if isinstance(data, dict) else None
    if isinstance(resolved, dict):
        info(
            f"resolved: {resolved.get('rtos')} {resolved.get('version')} -- "
            f"`gdr init {resolved.get('rtos')} {resolved.get('version')}`"
        )
    else:
        warn("no unambiguous version; choose the RTOS and version manually")
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("detail"):
            info(f"{candidate.get('rtos')}: {candidate['detail']}")


def _resolve_auto(rtos: str) -> str | None:
    """The probed version for ``gdr init <rtos> auto``, or ``None``.

    Only an unambiguous exact candidate resolves.  A range, an undetected
    version or two matching adapters must reach the user as a question, with
    the probe report carrying the evidence for their answer.
    """
    from gdr.probe import collect_candidates

    aliases = {"rt-thread": "rtthread", "rtt": "rtthread", "frt": "freertos"}
    wanted = aliases.get(rtos, rtos)
    matches = [
        candidate for candidate in collect_candidates() if candidate.rtos == wanted
    ]
    if len(matches) == 1 and matches[0].confidence == "exact" and matches[0].version:
        return matches[0].version
    return None


def _register_probes() -> None:
    """Register every adapter probe so ``gdr probe`` works before ``init``."""
    from freertos.probe import probe as freertos_probe
    from gdr.probe import register_probe
    from rtthread.probe import probe as rtthread_probe

    register_probe(rtthread_probe)
    register_probe(freertos_probe)


def _register_transport_functions() -> None:
    """Register the data-channel functions before any adapter exists.

    ``$gdr_json`` is how a client reads the probe that decides which adapter to
    initialize, so it must exist from ``source gdr.py`` -- registering it in the
    adapter setup would leave a client unable to ask its first question.  The
    human convenience functions (which return target values and need an active
    adapter) stay in the setup path.
    """
    from gdr.functions import register_api_functions

    register_api_functions()


def _reset_command() -> None:
    """Drop the active adapter and all session caches (re-init point).

    Printers are unregistered as well: ``register_printers`` keeps one
    layout-derived lookup per session, so a re-init into a different RTOS
    would otherwise fold objects through the previous adapter's layout.
    """
    from gdr.adapter_api import active, is_initialized, reset
    from gdr.api import next_generation
    from gdr.gdb_bridge import reset_loadable_ranges
    from gdr.printers import unregister_printers

    if not is_initialized():
        warn("no active RTOS adapter to reset")
        return
    adapter = active()
    reset_adapter = getattr(adapter, "reset", None)
    if reset_adapter is not None:
        reset_adapter()
    reset_loadable_ranges()
    unregister_printers()
    reset()
    next_generation()
    info("GDR session reset; run `gdr init <rtos> <version>` to re-initialize")


_EVENTS_REGISTERED = False


def _register_session_events() -> None:
    """Bump the API generation and clear caches on session-changing events.

    ``gdb.events`` handlers must never raise: they run inside GDB's event
    loop and a raise would surface as unexpected Python noise mid-session.
    """
    global _EVENTS_REGISTERED
    if _EVENTS_REGISTERED or gdb is None or not hasattr(gdb, "events"):
        return
    _EVENTS_REGISTERED = True

    def on_stop(_event: object = None) -> None:
        try:
            from gdr.api import next_generation

            next_generation()
        except Exception:  # pragma: no cover - defensive event-loop guard
            # Reason: an event hook that raises aborts GDB's event dispatch
            # and prints a raw traceback into the prompt; generation is a
            # cache hint, so losing one bump is preferable to that.
            return

    def on_new_objfile(_event: object = None) -> None:
        try:
            from gdr.adapter_api import active
            from gdr.api import next_generation
            from gdr.gdb_bridge import reset_loadable_ranges

            next_generation()
            reset_loadable_ranges()
            adapter = active()
            reset_adapter = getattr(adapter, "reset", None)
            if reset_adapter is not None:
                reset_adapter()
        except Exception:  # pragma: no cover - defensive event-loop guard
            # Reason: same as on_stop; a reflash must not crash the session
            # if one cache clear fails.
            return

    try:
        gdb.events.stop.connect(on_stop)
        gdb.events.new_objfile.connect(on_new_objfile)
    except Exception:  # pragma: no cover - older GDB event API
        # Reason: gdb.events exists from GDB 7.9; a build without it still
        # serves every command, it just cannot auto-invalidate caches.
        return


def _setup_rtthread(version: str, *, allow_unverified: bool = False) -> TargetVerdict:
    """Initialise RT-Thread support once for the current GDB session.

    Args:
        version: Full RT-Thread version string (e.g. ``"4.0.5"``).
        allow_unverified: Register even when the target matched no RT-Thread
            symbol (see ``rtthread.version.check_target``).

    Returns:
        The target verdict; ``accepted`` is false when nothing was registered.
    """
    from gdr.adapter_api import is_initialized, register
    from gdr.functions import register_api_functions, register_functions
    from rtthread.adapter import RtThreadAdapter
    from rtthread.commands import register_commands
    from rtthread.layout import build_layouts, detect_config
    from rtthread.version import check_target

    if is_initialized():
        message = (
            "RT-Thread support is already initialized; restart GDB before "
            "selecting a different target or version"
        )
        warn(message)
        return TargetVerdict(code="already_initialized", message=message)

    verdict = check_target(version, allow_unverified=allow_unverified)
    if not verdict.accepted or verdict.version is None:
        return verdict
    target_version = verdict.version
    info(f"setting up RT-Thread v{version}...")
    cfg = detect_config()
    info(
        f"  config: smp={cfg.smp} heap={cfg.heap_type} "
        f"sem={cfg.using_semaphore} mutex={cfg.using_mutex} "
        f"mb={cfg.using_mailbox} mq={cfg.using_messagequeue}"
    )
    kl = build_layouts(cfg, target_version)
    info(f"  layout: {len(kl.structs)} structs, {len(kl.list_hooks)} list hooks")
    adapter = RtThreadAdapter(kl, heap_type=cfg.heap_type, version=target_version)

    register_printers(kl)
    register_functions()
    register_api_functions()
    register_commands()
    register(
        adapter,
        rtos="rtthread",
        version=version,
        verification_note=verdict.note,
    )

    info("RT-Thread support ready. Type 'rtt help' for commands.")
    return verdict


def _setup_freertos(version: str, *, allow_unverified: bool = False) -> TargetVerdict:
    """Initialise FreeRTOS support for the current GDB target.

    Args:
        version: Full FreeRTOS version string.
        allow_unverified: Register even when the target matched no FreeRTOS
            symbol (see ``freertos.version.check_target``).

    Returns:
        The target verdict; ``accepted`` is false when nothing was registered.
    """
    from freertos.adapter import FreeRtosAdapter
    from freertos.commands import register_commands
    from freertos.layout import build_layout, detect_config
    from freertos.version import check_target
    from gdr.adapter_api import is_initialized, register
    from gdr.functions import register_api_functions, register_functions

    if is_initialized():
        message = (
            "an RTOS adapter is already initialized; restart GDB before "
            "selecting a different target or version"
        )
        warn(message)
        return TargetVerdict(code="already_initialized", message=message)

    verdict = check_target(version, allow_unverified=allow_unverified)
    if not verdict.accepted or verdict.version is None:
        return verdict
    target_version = verdict.version
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
    register_api_functions()
    register_commands()
    register(
        adapter,
        rtos="freertos",
        version=version,
        verification_note=verdict.note,
    )
    info("FreeRTOS support ready. Type 'freertos help' (alias: frt) for commands.")
    return verdict


def _setup_rtos(
    rtos: str, version: str, *, allow_unverified: bool = False
) -> TargetVerdict:
    """Dispatch to the appropriate RTOS setup function.

    Args:
        rtos: RTOS name (e.g. ``"rtthread"``).
        version: Full RTOS version string.
        allow_unverified: Passed through to the adapter's target check.

    Returns:
        The selected adapter's target verdict; an unsupported name yields a
        refusal verdict instead of raising, so callers can report one shape.
    """
    if rtos == "rtthread" or rtos == "rt-thread" or rtos == "rtt":
        return _setup_rtthread(version, allow_unverified=allow_unverified)
    if rtos == "freertos" or rtos == "frt":
        return _setup_freertos(version, allow_unverified=allow_unverified)
    warn(f"unsupported RTOS: {rtos!r}")
    warn("currently supported: rtthread, freertos")
    return TargetVerdict(
        code="unsupported_rtos",
        message=f"unsupported RTOS: {rtos!r}; currently supported: rtthread, freertos",
    )


def initialize() -> None:
    """Entry point: parse args and initialise the requested RTOS support."""
    if gdb is None:
        print("GDR must be sourced inside GDB.", file=sys.stderr)
        raise SystemExit(1)

    GdrCommand()
    _register_probes()
    _register_transport_functions()
    _register_session_events()
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
