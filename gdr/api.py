"""GDR semantic API: one neutral verb layer over the active adapter.

``gdr api <verb> [args]`` (and the ``$gdr_json`` / ``$gdr_json_chunk``
convenience functions) are the stable, machine-readable contract that
plugin clients depend on. The RTOS command trees stay the terminal-facing
text renderers; both consume the same collectors in :mod:`gdr.commands`,
so a verb result and the corresponding command output cannot drift apart.

Every response is an envelope (see :mod:`gdr.serialize`) carrying a schema
version, the session identity, a generation counter (bumped on target stop,
objfile load and session reset) and structured errors -- plugins never
scrape GDB stderr.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence

from gdr import __version__ as GDR_VERSION
from gdr.adapter_api import (
    RtosAdapter,
    active,
    default_capabilities,
    session_meta,
    verification_note,
)
from gdr.commands import (
    collect_detail_table,
    collect_heap_report,
    collect_object_detail,
    collect_object_listing,
    collect_object_overview,
    collect_task_table,
)
from gdr.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    format_exception,
    is_debug,
    propagate_exception,
    show_last_exception,
)
from gdr.probe import collect_candidates
from gdr.serialize import (
    capabilities_to_dict,
    checks_to_dict,
    chunk_payload,
    heap_map_to_dict,
    heap_report_to_dict,
    object_detail_to_dict,
    object_table_to_dict,
    pack_envelope,
    probe_to_dict,
    system_summary_to_dict,
    transport_plan,
)

_generation = 0

_VerbResult = tuple[object, list[str]]
_VerbHandler = Callable[[RtosAdapter, Sequence[str]], _VerbResult]
_PreInitVerbHandler = Callable[[Sequence[str]], _VerbResult]


class ApiError(Exception):
    """A structured API failure the envelope reports instead of raising."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def next_generation() -> int:
    """Bump and return the session generation counter.

    Bumped on target stop, objfile load and session reset (see the
    ``gdr.py`` event hooks); the envelope carries the value so a plugin can
    drop responses that raced a newer stop.
    """
    global _generation
    _generation += 1
    return _generation


def current_generation() -> int:
    """The generation counter of the current session."""
    return _generation


def _tasks_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if args:
        raise ApiError("bad_args", "tasks takes no arguments")
    table = collect_task_table(adapter)
    return object_table_to_dict(table, kind="task"), list(table.messages)


def _objects_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if len(args) > 1:
        raise ApiError("bad_args", "objects takes at most one kind argument")
    if not args:
        table = collect_object_overview(adapter)
        return object_table_to_dict(table), list(table.messages)
    requested = args[0].strip().lower()
    if requested == "task":
        return _tasks_verb(adapter, ())
    table = collect_object_listing(adapter, requested)
    if table is None:
        raise ApiError(
            "kind_not_enumerable",
            f"object kind {args[0]!r} is not reliably enumerable",
        )
    return object_table_to_dict(table, kind=requested), list(table.messages)


def _detail_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if len(args) != 2:
        raise ApiError("bad_args", "detail requires <kind> <name>")
    if not args[1].strip():
        raise ApiError("empty_name", "object name must not be empty")
    detail = collect_object_detail(adapter, args[0], args[1])
    if detail is None:
        raise ApiError(
            "kind_not_enumerable",
            f"object kind {args[0]!r} is not reliably enumerable",
        )
    return object_detail_to_dict(detail), []


def _table_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """Every object of one kind as a merged detail table (one row each)."""
    if len(args) != 1:
        raise ApiError("bad_args", "table requires <kind>")
    table = collect_detail_table(adapter, args[0])
    if table is None:
        raise ApiError(
            "kind_not_enumerable",
            f"object kind {args[0]!r} has no merged detail table",
        )
    return object_table_to_dict(table, kind=args[0].strip().lower()), list(
        table.messages
    )


def _heap_dump_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """The heap block layout and palette ``<rtos> heap dump`` draws."""
    if args:
        raise ApiError("bad_args", "heap-dump takes no arguments")
    heap = adapter.heap_map()
    if heap is None:
        raise ApiError(
            "no_heap",
            "no heap block chain to draw (no heap, or the chain is not resolvable)",
        )
    return heap_map_to_dict(heap), list(heap.messages)


def _system_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if args:
        raise ApiError("bad_args", "system takes no arguments")
    return system_summary_to_dict(adapter.system_summary()), []


def _heap_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if args:
        raise ApiError("bad_args", "heap takes no arguments")
    report = collect_heap_report(adapter)
    if report is None:
        return {"pairs": [], "messages": [], "table": None}, []
    return heap_report_to_dict(report), list(report.messages)


def _capabilities_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    if args:
        raise ApiError("bad_args", "capabilities takes no arguments")
    claims = adapter.capabilities()
    if claims is None:
        claims = default_capabilities(adapter)
    return capabilities_to_dict(claims), []


def _checks_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """The adapter's system-wide consistency conclusions, structured."""
    if args:
        raise ApiError("bad_args", "checks takes no arguments")
    findings = adapter.diagnostics_report()
    if findings is None:
        return checks_to_dict([]), [
            "this adapter reports no system-level checks; per-object "
            "verdicts stay in the detail verbs"
        ]
    return checks_to_dict(findings), []


_SNAPSHOT_FLAGS = ("--no-heap", "--no-details")


def _detail_tables(adapter: RtosAdapter) -> dict[str, object]:
    """One merged detail table per enumerable kind except tasks (their table is ``tasks``)."""
    tables: dict[str, object] = {}
    for kind in sorted(adapter.object_counts()):
        if kind == "task":
            continue
        table = adapter.detail_table(kind)
        if table is not None:
            tables[kind] = object_table_to_dict(table, kind=kind)
    return tables


def _snapshot_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """Every panel-facing verb in one envelope, one round trip.

    ``--no-heap`` drops the heap section: the heap walk is the one full-memory
    traversal among the panel verbs, so a stop-time refresh that only needs
    tasks/objects/system can skip it. ``--no-details`` drops the per-kind
    detail tables, the other per-object cost (mailbox, message queue and pool
    walk their entries). The panel's cost switches map onto these flags.
    Diagnostics ride inside the table they belong to, so the snapshot carries
    no separate checks section.
    """
    no_heap = False
    no_details = False
    for arg in args:
        if arg == "--no-heap":
            no_heap = True
        elif arg == "--no-details":
            no_details = True
        else:
            raise ApiError(
                "bad_args",
                f"snapshot takes only {', '.join(_SNAPSHOT_FLAGS)} (got {arg!r})",
            )
    tasks, task_warnings = _tasks_verb(adapter, ())
    objects, object_warnings = _objects_verb(adapter, ())
    system, _system_warnings = _system_verb(adapter, ())
    heap: object | None = None
    heap_warnings: list[str] = []
    if not no_heap:
        heap, heap_warnings = _heap_verb(adapter, ())
    capabilities, _capability_warnings = _capabilities_verb(adapter, ())
    tables: dict[str, object] | None = None
    if not no_details:
        tables = _detail_tables(adapter)
    warnings = task_warnings + object_warnings + heap_warnings
    build_hints: list[str] = []
    for payload in (tasks, capabilities):
        if isinstance(payload, dict):
            build_hints.extend(payload.get("build_hints", []))
    data = {
        "tasks": tasks,
        "objects": objects,
        "system": system,
        "heap": heap,
        "capabilities": capabilities,
        "tables": tables,
        # Reason: general messages and version notes are not build advice.
        "build_hints": list(dict.fromkeys(build_hints)),
    }
    return data, warnings


def _probe_payload(args: Sequence[str]) -> _VerbResult:
    """The probe report, with no session needed.

    Probing reads target symbols only, which is exactly why it is the one verb
    an uninitialized session must still answer: a client's first question is
    which adapter to initialize.
    """
    if args:
        raise ApiError("bad_args", "probe takes no arguments")
    candidates = collect_candidates()
    warnings: list[str] = []
    if not candidates:
        warnings.append("no supported RTOS was detected on this target")
    return probe_to_dict(candidates), warnings


def _probe_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """Serve the same probe report once a session exists."""
    del adapter
    return _probe_payload(args)


_VERBS: dict[str, _VerbHandler] = {
    "tasks": _tasks_verb,
    "objects": _objects_verb,
    "list": _objects_verb,
    "detail": _detail_verb,
    "system": _system_verb,
    "heap": _heap_verb,
    "capabilities": _capabilities_verb,
    "checks": _checks_verb,
    "table": _table_verb,
    "heap-dump": _heap_dump_verb,
    "probe": _probe_verb,
    "snapshot": _snapshot_verb,
}

# Verbs an uninitialized session can still answer (see _dispatch).
_PRE_INIT_VERBS: dict[str, _PreInitVerbHandler] = {"probe": _probe_payload}


def _error_envelope(code: str, message: str, elapsed_ms: float) -> dict[str, object]:
    return pack_envelope(
        None,
        ok=False,
        error={"code": code, "message": message},
        gdr_version=GDR_VERSION,
        generation=current_generation(),
        elapsed_ms=elapsed_ms,
    )


def _dispatch(
    verb: str, adapter: RtosAdapter | None, args: Sequence[str]
) -> _VerbResult:
    """Resolve and run one verb, or raise the coded error for its failure.

    An uninitialized session rejects every verb except the pre-init ones, so
    ``not_initialized`` keeps its precedence over ``unknown_verb``: the first
    actionable message a client needs is "initialize", not the vocabulary.
    """
    if adapter is None:
        pre_init = _PRE_INIT_VERBS.get(verb)
        if pre_init is None:
            raise ApiError("not_initialized", "run `gdr init <rtos> <version>` first")
        return pre_init(args)
    handler = _VERBS.get(verb)
    if handler is None:
        available = ", ".join(sorted(_VERBS))
        raise ApiError("unknown_verb", f"unknown verb {verb!r}; available: {available}")
    return handler(adapter, args)


def api_call(verb: str, *args: str) -> dict[str, object]:
    """Run one API verb and return its envelope (no GDB printing)."""
    start = time.monotonic()
    adapter = active()
    try:
        data, warnings = _dispatch(verb, adapter, args)
    except ApiError as exc:
        return _error_envelope(exc.code, exc.message, _elapsed_ms(start))
    except TARGET_ACCESS_ERRORS as exc:
        # Reason: a target read fails for reasons the plugin simply retries
        # after the next stop (halted core, unmapped address, dropped remote
        # link). That is recoverable degradation, not a GDR defect, so it gets
        # its own code and no diagnostic noise -- the envelope is the report.
        return _error_envelope(
            "target_unavailable", format_exception(exc), _elapsed_ms(start)
        )
    except Exception as exc:
        # Reason: an unexpected failure is a defect, so the API boundary
        # mirrors the command guard: a debug build prints the full diagnostic
        # and honours GDR_PROPAGATE_EXCEPTION. Unlike the guard it does not
        # also print an err() one-liner, because the envelope carries the same
        # message and every caller already renders it.
        if is_debug():
            show_last_exception()
            if propagate_exception():
                raise
        return _error_envelope(
            "internal", f"{type(exc).__name__}: {exc}", _elapsed_ms(start)
        )
    rtos, rtos_version = session_meta()
    # Reason: a plugin cannot read `gdr init`'s console output, so the one
    # caveat a session can carry -- the target could not confirm the adapter
    # it was registered for -- rides every envelope instead of being lost
    # with that command's output. A client renders it as a hint, so an
    # unverified session is never silently trusted.
    note = verification_note()
    session_warnings = [note, *warnings] if note is not None else list(warnings)
    return pack_envelope(
        data,
        rtos=rtos,
        rtos_version=rtos_version,
        gdr_version=GDR_VERSION,
        warnings=session_warnings,
        generation=current_generation(),
        elapsed_ms=_elapsed_ms(start),
    )


def envelope_json(verb: str, *args: str) -> str:
    """The envelope as a compact JSON document (what ``$gdr_json`` returns)."""
    return json.dumps(api_call(verb, *args), default=str, separators=(",", ":"))


def dump_response(path: str, verb: str, *args: str) -> int:
    """Write one envelope to *path* and return its byte count, or -1.

    The plugin's file transport calls this through ``$gdr_dump`` instead of
    typing a console command: a convenience function's scalar result survives
    every adapter's ``evaluate`` rendering, while a command typed at the GDB
    console does not reach GDB through all of them.
    """
    payload = envelope_json(verb, *args)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError:
        return -1
    return len(payload.encode("utf-8"))


def transport_response(verb: str, *args: str) -> str:
    """One ``$gdr_json`` result: inline payload or the chunked meta frame.

    A chunked answer also seeds the frame snapshot (see
    :func:`_chunk_snapshot`) so every later frame describes the same
    envelope the meta frame's checksum was computed over.
    """
    payload = envelope_json(verb, *args)
    kind, body = transport_plan(payload)
    if kind == "chunked":
        _remember_chunk_snapshot(verb, args, payload)
    return body


def chunk_response(index: int, verb: str, *args: str) -> str:
    """One ``$gdr_json_chunk`` result: frame 0 is meta, 1..total are slices.

    Frames must come from one frozen envelope: recomputing per frame would
    embed a new ``elapsed_ms`` each time, so the checksum could never match
    and, worse, a target that moved between two frame requests would
    reassemble into a mix of two different snapshots.
    """
    frames = chunk_payload(_chunk_snapshot(verb, args))
    if index < 0 or index >= len(frames):
        return json.dumps(
            {"error": "chunk_out_of_range", "index": index, "total": len(frames) - 1}
        )
    return frames[index]


# Chunk frames are pulled one per DAP evaluate round trip, so the envelope
# they describe has to survive between those calls.  A tiny bounded cache
# keyed by (verb, args) does that; the generation is stored beside the
# payload because a target stop or objfile load invalidates every snapshot
# it was built from.
_CHUNK_SNAPSHOTS: dict[tuple[str, tuple[str, ...]], tuple[int, str]] = {}
_CHUNK_SNAPSHOT_LIMIT = 8


def _remember_chunk_snapshot(verb: str, args: Sequence[str], payload: str) -> None:
    """Freeze *payload* as the frame source for a subsequent frame pull."""
    if len(_CHUNK_SNAPSHOTS) >= _CHUNK_SNAPSHOT_LIMIT:
        _CHUNK_SNAPSHOTS.clear()
    _CHUNK_SNAPSHOTS[(verb, tuple(args))] = (current_generation(), payload)


def _chunk_snapshot(verb: str, args: Sequence[str]) -> str:
    """The frozen frame source for (verb, args), or a fresh one for a new pull."""
    cached = _CHUNK_SNAPSHOTS.get((verb, tuple(args)))
    if cached is not None and cached[0] == current_generation():
        return cached[1]
    payload = envelope_json(verb, *args)
    _remember_chunk_snapshot(verb, args, payload)
    return payload


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 3)
