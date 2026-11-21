"""GDR API envelope serialization (pure module, no ``gdb`` import).

The envelope is the stable machine-readable contract between GDR and
plugin-style clients. It carries a schema version independent of the GDR
release version, so a client can reject an incompatible GDR without
parsing release strings.

The chunk protocol lets the ``$gdr_json_chunk`` transport push arbitrary
payload sizes through DAP ``evaluate`` results, which are otherwise
bounded by GDB's print-element limit and MI varobj truncation rules.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Sequence

from core import __version__ as GDR_VERSION
from core.adapter import (
    BacktraceReport,
    Capabilities,
    CheckFinding,
    DetailSection,
    HeapMap,
    HeapReport,
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
    active,
    default_capabilities,
    session_meta,
    verification_note,
)
from core.commands import (
    collect_detail_table,
    collect_heap_report,
    collect_object_detail,
    collect_object_listing,
    collect_object_overview,
    collect_task_table,
)
from core.constants import (
    GDR_API_CHUNK_SIZE,
    GDR_API_INLINE_LIMIT,
    GDR_API_SCHEMA_VERSION,
)
from core.formatting import SourceLoc, format_optional_bytes
from core.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    format_exception,
    is_debug,
    loaded_object_path,
    propagate_exception,
    show_last_exception,
)
from core.heapmap import build_palette, grid_layout
from core.version import ProbeCandidate, collect_candidates, resolve_candidate

# Transport thresholds live in core.constants; this module only consumes them.


def pack_envelope(
    data: object,
    *,
    ok: bool = True,
    error: dict[str, object] | None = None,
    rtos: str | None = None,
    rtos_version: str | None = None,
    gdr_version: str | None = None,
    generation: int = 0,
    warnings: Sequence[str] = (),
    elapsed_ms: float | None = None,
) -> dict[str, object]:
    """Build one API response envelope.

    Args:
        data: Verb-specific payload (usually a ``*_to_dict`` render).
        error: Structured failure ``{"code", "message"}``; ``ok`` is then
            ``False``. Plugins never scrape GDB stderr, so every failure
            mode the text renderers warn about has a code here.
        rtos: Session RTOS identity registered at init time.
        rtos_version: Session version identity registered at init time.
        gdr_version: GDR release version that produced the envelope.
        generation: Session-generation counter; a client drops responses
            that raced a newer stop or objfile event (see ``core.api``).
        warnings: Capability notes the text renderers used to print as
            ``info`` lines; plugins surface them the same way.
        elapsed_ms: Wall time of the verb, so a client can self-throttle.
    """
    envelope: dict[str, object] = {
        "schema_version": GDR_API_SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "rtos": rtos,
        "rtos_version": rtos_version,
        "gdr_version": gdr_version,
        "generation": generation,
        "elapsed_ms": elapsed_ms,
    }
    if warnings:
        envelope["warnings"] = list(warnings)
    if error is not None:
        envelope["error"] = error
    return envelope


def object_table_to_dict(
    table: ObjectTable, *, kind: str | None = None
) -> dict[str, object]:
    """Serialize a list table; typed metadata only when the adapter set it.

    ``ids`` are the rows' stable identities (the ``id_key`` column's machine
    value) and ``kind`` names the object family they belong to, so a client
    can keep a selection or expansion state across refreshes.  Both are
    omitted when the adapter declares no identity column.
    """
    result: dict[str, object] = {
        "headers": list(table.headers),
        "rows": [list(row) for row in table.rows],
        "messages": list(table.messages),
        "elastic": list(table.elastic),
    }
    if table.build_hints:
        result["build_hints"] = list(table.build_hints)
    if kind is not None:
        result["kind"] = kind
    if table.column_specs:
        result["columns"] = [
            {
                "key": spec.key,
                "kind": spec.kind,
                "align": spec.align,
                "elastic": spec.elastic,
                "doc": spec.doc,
                "group": spec.group,
            }
            for spec in table.column_specs
        ]
    if table.value_rows is not None:
        result["value_rows"] = table.value_rows
        if table.id_key in table.headers:
            index = table.headers.index(table.id_key)
            result["ids"] = [row[index] for row in table.value_rows]
    if table.source_rows is not None:
        result["source_rows"] = [
            [_source_to_dict(source) for source in row] for row in table.source_rows
        ]
    return result


def _source_to_dict(source: SourceLoc | None) -> dict[str, object] | None:
    """One cell's source location, or null.

    ``file`` is GDB's resolved path, ``name`` the file as recorded in the debug
    information; the client resolves a missing ``file`` through ``name``.
    """
    if source is None:
        return None
    return {"file": source.fullname, "name": source.name, "line": source.line}


def object_detail_to_dict(detail: ObjectDetail) -> dict[str, object]:
    """Serialize one vertical detail block.

    ``pairs`` stays the complete flat view the text renderer prints;
    ``sections`` adds the grouped verdict rows with per-row severity.
    """
    result: dict[str, object] = {
        "pairs": [[key, value] for key, value in detail.pairs],
        "found": detail.found,
        "message": detail.message,
    }
    if detail.sections:
        result["sections"] = _sections_to_list(detail.sections)
    return result


def _sections_to_list(sections: Sequence[DetailSection]) -> list[dict[str, object]]:
    """Serialize verdict sections: title plus per-row key, value and severity."""
    return [
        {
            "title": section.title,
            "rows": [
                {"key": row.key, "value": row.value, "severity": row.severity}
                for row in section.rows
            ],
        }
        for section in sections
    ]


def backtrace_to_dict(report: BacktraceReport) -> dict[str, object]:
    """Serialize one bounded task backtrace.

    The frame list is the stable contract a client walks; ``complete`` is true
    only when the unwinder reached the task entry or a zero return address.
    ``stop_reason`` names the exact bound that ended the walk.
    """
    return {
        "task": report.task,
        "address": report.address,
        "architecture": report.architecture,
        "abi": report.abi,
        "method": report.method,
        "source": report.source,
        "complete": report.complete,
        "stop_reason": report.stop_reason,
        "truncated": report.truncated,
        "max_depth": report.max_depth,
        "bounds": report.bounds,
        "frames": [
            {
                "index": frame.index,
                "pc": frame.pc,
                "symbol": frame.symbol,
                "source": _source_to_dict(frame.source),
                "method": frame.method,
            }
            for frame in report.frames
        ],
        "messages": list(report.messages),
    }


def system_summary_pairs(summary: SystemSummary) -> list[tuple[str, str]]:
    """The normalized vertical key/value pairs of a system summary.

    Lifted out of ``core.commands.render_system`` so the text renderer and
    the JSON serializer share one ordering/formatting source of truth.
    """
    pairs: list[tuple[str, str]] = [
        ("Current task", summary.current_task or "N/A"),
        (
            summary.task_count_label,
            str(summary.task_count) if summary.task_count is not None else "N/A",
        ),
        (
            "Tick count",
            str(summary.tick_count) if summary.tick_count is not None else "N/A",
        ),
    ]
    if summary.scheduler_state is not None:
        pairs.append(("Scheduler state", summary.scheduler_state))
    pairs.extend((state, str(count)) for state, count in summary.state_counts.items())
    pairs.extend(
        (kind, str(count)) for kind, count in sorted(summary.object_counts.items())
    )
    pairs.append(("Heap allocator", summary.heap_allocator or "N/A"))
    if summary.heap_used is not None or summary.heap_total is not None:
        pairs.append(("Heap used", format_optional_bytes(summary.heap_used)))
        pairs.append(("Heap total", format_optional_bytes(summary.heap_total)))
    if summary.heap_status is not None:
        pairs.append(("Heap status", summary.heap_status))
    pairs.extend(summary.extra_pairs)
    return pairs


def system_summary_to_dict(summary: SystemSummary) -> dict[str, object]:
    """Serialize a system summary as the same pairs the text renderer shows."""
    return {"pairs": [[key, value] for key, value in system_summary_pairs(summary)]}


def heap_report_to_dict(report: HeapReport) -> dict[str, object]:
    """Serialize one heap snapshot, with its verdict rows as ``sections``."""
    table = None
    if report.table is not None:
        table = object_table_to_dict(report.table)
    result: dict[str, object] = {
        "pairs": [[key, value] for key, value in report.pairs],
        "messages": list(report.messages),
        "table": table,
        "table_title": report.table_title,
    }
    if report.sections:
        result["sections"] = _sections_to_list(report.sections)
    return result


def heap_map_to_dict(heap: HeapMap) -> dict[str, object]:
    """Serialize the heap block layout the bitmap and the panel draw from.

    Blocks are ``[address, size, used, owner]`` rows in address order; ``owner``
    is ``null`` when the allocator keeps no owner names (see ``owners``).
    """
    return {
        "algorithm": heap.algorithm,
        "base": heap.base,
        "end": heap.end,
        "owners": heap.owners,
        "truncated": heap.truncated,
        "messages": list(heap.messages),
        "blocks": [
            [block.address, block.size, block.used, block.owner]
            for block in heap.blocks
        ],
        # The colours the bitmap uses, so the panel's drawing matches the PNG.
        "palette": build_palette(heap).as_json(),
        "grid": grid_layout(heap),
    }


def capabilities_to_dict(capabilities: Capabilities) -> dict[str, object]:
    """Serialize adapter capability claims (structured missing-column reasons)."""
    return {
        "kinds": {
            kind: {
                "enumerable": claim.enumerable,
                "detail": claim.detail,
                "columns": list(claim.columns),
                "note": claim.note,
            }
            for kind, claim in sorted(capabilities.kinds.items())
        },
        "extra": capabilities.extra,
        "build_hints": list(capabilities.build_hints),
    }


def probe_to_dict(candidates: Sequence[ProbeCandidate]) -> dict[str, object]:
    """Serialize probe candidates plus the one safe to initialize from.

    ``resolved`` is the single ``exact`` candidate, and ``None`` whenever the
    evidence is ambiguous, missing or only a range: a client must treat that as
    "ask the user", never as "take the first row".
    """
    resolved = resolve_candidate(candidates)
    return {
        "candidates": [
            {
                "rtos": candidate.rtos,
                "version": candidate.version,
                "confidence": candidate.confidence,
                "evidence": list(candidate.evidence),
                "detail": candidate.detail,
            }
            for candidate in candidates
        ],
        "resolved": (
            {"rtos": resolved.rtos, "version": resolved.version}
            if resolved is not None
            else None
        ),
    }


def checks_to_dict(findings: Sequence[CheckFinding]) -> dict[str, object]:
    """Serialize system check findings plus their per-severity counts."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1
    return {
        "findings": [
            {
                "name": finding.name,
                "verdict": finding.verdict,
                "detail": finding.detail,
                "scope": finding.scope,
            }
            for finding in findings
        ],
        "counts": dict(sorted(counts.items())),
    }


# ---------------------------------------------------------------------------
# Chunked transport: DAP evaluate results are strings bounded by GDB's
# print/varobj limits, so oversized envelopes travel as base64 frames.
# Base64 keeps every frame printable ASCII with no quotes or control bytes,
# which no evaluate-result escaping can mangle.
# ---------------------------------------------------------------------------


def chunk_payload(payload: str, chunk_size: int = GDR_API_CHUNK_SIZE) -> list[str]:
    """Split *payload* into chunked-transport frames.

    Frame 0 is a meta JSON frame (schema, total, chunk size, sha256);
    frames 1..total are base64 slices of the UTF-8 payload.
    """
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    slices = [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]
    meta = json.dumps(
        {
            "kind": "chunks",
            "schema_version": GDR_API_SCHEMA_VERSION,
            "total": len(slices),
            "chunk_size": chunk_size,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        }
    )
    return [meta, *slices]


def reassemble_chunks(meta_frame: str, slices: Sequence[str]) -> str:
    """Inverse of :func:`chunk_payload`; verifies the sha256 frame.

    Raises:
        ValueError: The meta frame is not JSON, or the checksum disagrees.
    """
    try:
        meta = json.loads(meta_frame)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chunk meta frame is not JSON: {exc}") from exc
    payload = base64.b64decode("".join(slices)).decode("utf-8")
    expected = meta["sha256"]
    actual = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if actual != expected:
        raise ValueError(f"chunk reassembly checksum mismatch: {actual} != {expected}")
    return payload


def transport_plan(
    payload: str, inline_limit: int = GDR_API_INLINE_LIMIT
) -> tuple[str, str]:
    """Decide how a payload should cross the DAP evaluate channel.

    Returns ``("inline", payload)`` when it fits in one evaluate result,
    else ``("chunked", meta_frame)``; the caller then drives
    ``$gdr_json_chunk`` for frames 1..total.
    """
    if len(payload) <= inline_limit:
        return "inline", payload
    return "chunked", chunk_payload(payload)[0]


# ---------------------------------------------------------------------------
# Semantic API verb layer (``gdr api <verb>`` and the ``$gdr_json`` /
# ``$gdr_json_chunk`` transports).
# ---------------------------------------------------------------------------


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
        raise ApiError("bad_args", "detail requires <kind> <name-or-address>")
    if not args[1].strip():
        raise ApiError("empty_name", "object name or address must not be empty")
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


def _parse_backtrace_args(args: Sequence[str]) -> tuple[str | None, int]:
    """Parse ``[<task>] [--max-depth N]`` arguments for backtrace verbs."""
    target: str | None = None
    max_depth = 32
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--max-depth":
            if index + 1 >= len(args):
                raise ApiError("bad_args", "--max-depth requires a value")
            try:
                max_depth = int(args[index + 1])
            except ValueError as exc:
                raise ApiError(
                    "bad_args", f"invalid --max-depth: {args[index + 1]}"
                ) from exc
            index += 2
            continue
        if arg.startswith("--max-depth="):
            try:
                max_depth = int(arg.split("=", 1)[1])
            except ValueError as exc:
                raise ApiError("bad_args", f"invalid --max-depth: {arg}") from exc
            index += 1
            continue
        if arg.startswith("--"):
            raise ApiError("bad_args", f"unknown backtrace option: {arg}")
        if target is not None:
            raise ApiError("bad_args", "backtrace takes at most one task name")
        target = arg
        index += 1
    if target is not None and not target.strip():
        raise ApiError("empty_name", "task name must not be empty")
    if max_depth < 1 or max_depth > 64:
        raise ApiError("bad_args", "--max-depth must be between 1 and 64")
    return (target.strip() if target is not None else None), max_depth


def _pre_init_backtrace_verb(args: Sequence[str]) -> _VerbResult:
    """Adaptive non-RTOS current-PC backtrace before any RTOS adapter is active."""
    from .backtrace.backtrace_gdb import live_backtrace

    target, max_depth = _parse_backtrace_args(args)
    if target is not None:
        raise ApiError(
            "not_initialized",
            "task backtrace requires an active RTOS adapter; "
            "run `gdr init <rtos> <version>` first",
        )
    report = live_backtrace(max_depth)
    return backtrace_to_dict(report), list(report.messages)


def live_backtrace_envelope(max_depth: int = 32) -> dict[str, object]:
    """Run RTOS-agnostic live backtrace and wrap the result in a GDR envelope."""
    from .backtrace.backtrace_gdb import live_backtrace

    start = time.monotonic()
    rtos, rtos_version = session_meta()
    note = verification_note()
    try:
        report = live_backtrace(max_depth)
    except TARGET_ACCESS_ERRORS as exc:
        return _error_envelope(
            "target_unavailable", format_exception(exc), _elapsed_ms(start)
        )
    except Exception as exc:
        if is_debug():
            show_last_exception()
            if propagate_exception():
                raise
        return _error_envelope(
            "internal", f"{type(exc).__name__}: {exc}", _elapsed_ms(start)
        )
    warnings = list(report.messages)
    if note is not None:
        warnings.insert(0, note)
    return pack_envelope(
        backtrace_to_dict(report),
        rtos=rtos,
        rtos_version=rtos_version,
        gdr_version=GDR_VERSION,
        warnings=warnings,
        generation=current_generation(),
        elapsed_ms=_elapsed_ms(start),
    )


def _backtrace_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """One task's or current PC's bounded backtrace; ``backtrace [<task>] [--max-depth N]``."""
    target, max_depth = _parse_backtrace_args(args)
    report = adapter.backtrace(target, max_depth)
    if report is None:
        raise ApiError(
            "backtrace_unavailable", "this adapter cannot unwind task stacks"
        )
    return backtrace_to_dict(report), list(report.messages)


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


def _check_verb(adapter: RtosAdapter, args: Sequence[str]) -> _VerbResult:
    """The adapter's system-wide consistency conclusions, structured."""
    if args:
        raise ApiError("bad_args", "check takes no arguments")
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
        # NOTE: general messages and version notes are not build advice.
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
        if loaded_object_path() is None:
            warnings.append(
                "GDB has no symbol file loaded; run `file <elf>` first "
                "(`load` only writes the target memory)"
            )
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
    "check": _check_verb,
    # NOTE: ``checks`` stays an alias for earlier clients; the shape is
    # unchanged, so no schema bump is owed.
    "checks": _check_verb,
    "backtrace": _backtrace_verb,
    "bt": _backtrace_verb,
    "table": _table_verb,
    "heap-dump": _heap_dump_verb,
    "probe": _probe_verb,
    "snapshot": _snapshot_verb,
}

# Verbs an uninitialized session can still answer (see _dispatch).
_PRE_INIT_VERBS: dict[str, _PreInitVerbHandler] = {
    "probe": _probe_payload,
    "backtrace": _pre_init_backtrace_verb,
    "bt": _pre_init_backtrace_verb,
}


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
        # NOTE: a target read fails for reasons the plugin simply retries
        # after the next stop (halted core, unmapped address, dropped remote
        # link). That is recoverable degradation, not a GDR defect, so it gets
        # its own code and no diagnostic noise -- the envelope is the report.
        return _error_envelope(
            "target_unavailable", format_exception(exc), _elapsed_ms(start)
        )
    except Exception as exc:
        # NOTE: an unexpected failure is a defect, so the API boundary
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
    # NOTE: a plugin cannot read `gdr init`'s console output, so the one
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
