"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from core import heapmap
from core.adapter import (
    BacktraceReport,
    ColumnSpec,
    HeapReport,
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    active,
)
from core.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    display_path,
    format_source_loc,
    gdb_command_guard,
    gdb_function_guard,
    info,
    is_debug,
    make_pointer_array,
    print_detail,
    print_table,
    read_cstring,
    warn,
    write_stdout,
)
from core.object_ref import ObjectResolution

BACKTRACE_COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec("#", align="right"),
    ColumnSpec("PC", kind="addr"),
    ColumnSpec("Function", kind="symbol"),
    ColumnSpec("Source", kind="file", elastic="right"),
    ColumnSpec("Method"),
)


def _render_object_table(table: ObjectTable) -> None:
    """Render one :class:`ObjectTable`, shortening file-path columns."""
    for message in table.messages:
        info(message)
    rows = table.rows
    file_indexes = [
        index for index, spec in enumerate(table.column_specs) if spec.kind == "file"
    ]
    if file_indexes:
        rows = [
            [
                display_path(cell) if index in file_indexes and cell else cell
                for index, cell in enumerate(row)
            ]
            for row in rows
        ]
    modes = table.elastic_modes()
    if any(mode != "left" for mode in modes.values()):
        print_table(rows, table.headers, elastic=table.elastic, elastic_modes=modes)
    else:
        print_table(rows, table.headers, elastic=table.elastic)


@gdb_command_guard
def render_tasks() -> None:
    """Render the active adapter's task table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    _render_object_table(collect_task_table(adapter))


def render_backtrace_report(report: BacktraceReport) -> None:
    """Format and print one bounded :class:`BacktraceReport` as a frame table."""
    if report.stop_reason == "ambiguous-target" and report.messages:
        warn(report.messages[0])
        return
    if is_debug():
        for message in report.messages:
            info(message)
    headers = [spec.key for spec in BACKTRACE_COLUMN_SPECS]
    rows = [
        [
            str(frame.index),
            hex(frame.pc),
            frame.symbol or hex(frame.pc),
            format_source_loc(frame.source),
            frame.method,
        ]
        for frame in report.frames
    ]
    print_table(rows, headers, elastic={"Source": "right"})
    state = "complete" if report.complete else "partial"
    print(
        f"{report.task}: {report.architecture}, "
        f"{report.source}, {report.method}, {state} ({report.stop_reason})"
    )


@gdb_command_guard
def render_backtrace(target: str | None = None, max_depth: int = 32) -> None:
    """Render one bounded task or current-PC backtrace as a compact frame table.

    ``target=None`` selects the halted core's current running task or pre-
    scheduler live context.  Each row names the frame index, PC, resolved
    symbol and source line.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    if target is not None and not target.strip():
        warn("task name or address must not be empty")
        return
    normalized = target.strip() if target is not None else None
    report = adapter.backtrace(normalized, max_depth)
    if report is None:
        warn("this adapter cannot unwind task stacks")
        return
    render_backtrace_report(report)


def parse_backtrace_max_depth(
    argument: str, usage: str, *, allow_target: bool = True
) -> tuple[str | None, int] | None:
    """Parse ``[<target>] [--max-depth N]``, or warn and return ``None``."""
    rest = argument.strip()
    max_depth = 32
    marker = rest.rfind("--max-depth")
    if marker >= 0 and (marker == 0 or rest[marker - 1].isspace()):
        option = rest[marker:].split()
        if option[0] == "--max-depth":
            if len(option) != 2:
                warn(usage)
                return None
            depth_text = option[1]
        elif option[0].startswith("--max-depth=") and len(option) == 1:
            depth_text = option[0].split("=", 1)[1]
        else:
            warn(usage)
            return None
        try:
            max_depth = int(depth_text)
        except ValueError:
            warn(f"invalid --max-depth: {depth_text}")
            return None
        rest = rest[:marker].strip()
    if rest.startswith("--") or (not allow_target and rest):
        warn(usage)
        return None
    if max_depth < 1 or max_depth > 64:
        warn("--max-depth must be between 1 and 64")
        return None
    return (rest or None), max_depth


def invoke_backtrace_command(argument: str, usage: str) -> None:
    """Parse ``backtrace [<target>] [--max-depth N]`` and render the frame table."""
    words = argument.split(None, 1)
    parsed = parse_backtrace_max_depth(
        words[1] if len(words) == 2 else "", usage, allow_target=True
    )
    if parsed is None:
        return
    target, max_depth = parsed
    render_backtrace(target, max_depth)


@gdb_command_guard
def render_object_detail(kind: str, target: str) -> None:
    """Render one object's complete detail as a Key: Value block.

    *target* is a registry name, an explicit ``0x``-hex or decimal address,
    or a plain C identifier naming the object's static symbol or handle.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    requested = _canonical_kind(kind)
    if not target.strip():
        warn("object name or address must not be empty")
        return
    detail = collect_object_detail(adapter, requested, target)
    if detail is None:
        warn(f"object kind {requested!r} is not reliably enumerable")
        return
    if not detail.found:
        warn(detail.message or f"{requested} {target!r}: not found or type not enabled")
        return
    print_detail(detail.pairs)


@gdb_command_guard
def render_system() -> None:
    """Render the normalized system summary as a vertical key/value block."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    from core.api import system_summary_pairs

    print_detail(system_summary_pairs(adapter.system_summary()))


@gdb_command_guard
def render_objects(kind: str = "") -> None:
    """Render reliably enumerable object counts from the active adapter.

    A count is an adapter capability claim: an absent kind means the adapter
    cannot enumerate it reliably, not that the target necessarily has none.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    requested = _canonical_kind(kind)
    if requested == "task":
        render_tasks()
        return
    if requested:
        table = collect_object_listing(adapter, requested)
        if table is None:
            warn(f"object kind {requested!r} is not reliably enumerable")
            return
    else:
        # The provenance summary wins when the adapter provides one (its
        # per-object source channels); otherwise fall back to the plain
        # Kind/Count table so every RTOS shares one renderer.
        table = collect_object_overview(adapter)
    _render_object_table(table)


@dataclass(frozen=True)
class HeapDumpOptions:
    """Parsed ``heap dump`` arguments: image path plus bitmap sizing."""

    path: str = ""
    cell_px: int | None = None
    max_px: int | None = None
    row_bytes: int | None = None


def render_heap_dump_options(
    words: list[str], usage: str
) -> tuple[HeapDumpOptions | None, str | None]:
    """Split ``heap dump`` tail words into sizing flags.

    The first bare word is the image path; each flag takes ``value`` or
    ``=value``. Returns ``(options, None)`` on success, ``(None, reason)``
    otherwise (shape errors carry *usage*), so all adapters share one
    parser and one validation without depending on GDB.
    """
    path = ""
    cell_px: int | None = None
    max_px: int | None = None
    row_bytes: int | None = None
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("--"):
            name, equals, value = word[2:].partition("=")
            if not equals:
                index += 1
                if index >= len(words) or words[index].startswith("--"):
                    return None, usage
                value = words[index]
            if name == "cell-pixel":
                cell_px = _positive_int(value)
                if cell_px is None:
                    return (
                        None,
                        f"--cell-pixel {value!r} must be an integer 1-64: {usage}",
                    )
                if cell_px > 64:
                    return None, f"--cell-pixel {cell_px} exceeds the 64-pixel maximum"
            elif name == "width":
                max_px = _positive_int(value)
                if max_px is None:
                    return (
                        None,
                        f"--width {value!r} must be a positive integer: {usage}",
                    )
            elif name == "row-bytes":
                row_bytes = _positive_int(value)
                if row_bytes is None:
                    return (
                        None,
                        f"--row-bytes {value!r} must be a positive integer: {usage}",
                    )
            else:
                return None, usage
        elif path:
            return None, usage
        else:
            path = word
        index += 1
    # NOTE: flags may follow the path, so compatibility is checked after
    # the whole tail parses rather than per token.
    if max_px is not None and cell_px is not None and max_px < cell_px:
        return (
            None,
            f"heap bitmap width {max_px} is narrower than one {cell_px}-pixel cell",
        )
    return HeapDumpOptions(
        path=path, cell_px=cell_px, max_px=max_px, row_bytes=row_bytes
    ), None


def _positive_int(value: str) -> int | None:
    """A strictly positive integer flag value, or ``None``."""
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 1 else None


@gdb_command_guard
def render_heap_dump(
    rtos: str,
    path: str = "",
    cell_px: int | None = None,
    max_px: int | None = None,
    row_bytes: int | None = None,
) -> None:
    """Draw the heap-occupancy bitmap, inline when the terminal can show it.

    The bitmap goes to ``path`` (default ``gdr-heap-<rtos>-gen<N>.png`` in the
    working directory) whenever the terminal cannot show images or ``path`` is
    given. Otherwise the terminal's inline-image sequence is written to stdout.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    heap = adapter.heap_map()
    if heap is None:
        warn("no heap block chain to draw (no heap, or the chain is not resolvable)")
        return
    for message in heap.messages:
        info(message)
    palette = heapmap.build_palette(heap)
    try:
        png = heapmap.render_png(
            heap,
            palette,
            cell_px if cell_px is not None else heapmap.SCALE,
            max_px if max_px is not None else heapmap.MAX_PIXEL_WIDTH,
            row_bytes=row_bytes,
        )
    except ValueError as exc:
        warn(str(exc))
        return
    for line in heapmap.summary_lines(heap, palette):
        info(line)
    mode = heapmap.output_mode(os.environ)
    if path or mode == "file":
        target = path or _default_heap_image(rtos)
        try:
            with open(target, "wb") as handle:
                handle.write(png)
        except OSError as exc:
            warn(f"could not write the heap map to {target}: {exc.strerror or exc}")
            return
        info(f"heap map written to {target} ({len(png)} bytes)")
        return
    write_stdout("\n" + heapmap.inline_sequence(png, mode) + "\n")


def _default_heap_image(rtos: str) -> str:
    """The default bitmap path: the generation keeps successive dumps apart."""
    from core.api import current_generation

    return os.path.join(os.getcwd(), f"gdr-heap-{rtos}-gen{current_generation()}.png")


@gdb_command_guard
def render_heap() -> None:
    """Render the active adapter's heap snapshot via :class:`HeapReport`.

    The adapter returns the vertical basics, an optional block/occupancy
    table and capability messages; printing order is detail, then messages,
    then the table -- one neutral path for the RTOS heap command.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    report = collect_heap_report(adapter)
    if report is None:
        return
    print_detail(report.pairs)
    for message in report.messages:
        info(message)
    if report.table is not None:
        _render_object_table(report.table)


def collect_task_table(adapter) -> ObjectTable:
    """The adapter-owned task table, without printing."""
    return adapter.task_table()


def collect_detail_table(adapter, kind: str) -> ObjectTable | None:
    """One kind's merged detail table, or ``None`` when the adapter has none."""
    return adapter.detail_table(_canonical_kind(kind))


def collect_object_listing(adapter, kind: str) -> ObjectTable | None:
    """One kind's object table, or the Kind/Count fallback.

    ``None`` means the kind is not reliably enumerable (the renderer warns).
    """
    requested = _canonical_kind(kind)
    table = adapter.object_table(requested)
    if table is not None:
        return table
    counts = adapter.object_counts()
    if requested not in counts:
        return None
    return ObjectTable(
        headers=["Kind", "Count"], rows=[[requested, str(counts[requested])]]
    )


def collect_object_overview(adapter) -> ObjectTable:
    """The per-kind overview, provenance summary when the adapter has one."""
    summary = adapter.object_summary_table()
    if summary is not None:
        return summary
    counts = adapter.object_counts()
    return ObjectTable(
        headers=["Kind", "Count"],
        rows=[[name, str(count)] for name, count in sorted(counts.items())],
    )


def collect_object_detail(adapter, kind: str, target: str) -> ObjectDetail | None:
    """One object's vertical detail, or ``None`` when the kind is unknown."""
    return adapter.object_detail(_canonical_kind(kind), target)


def collect_heap_report(adapter) -> HeapReport | None:
    """The adapter's heap snapshot, or ``None`` when it has no heap."""
    return adapter.heap_report()


def _canonical_kind(kind: str) -> str:
    """Normalize only the adapter-neutral semantic kind spelling."""
    return kind.strip().lower()


def prefix_candidates(word: str | None, candidates: list[str]) -> list[str]:
    """Return candidates starting with *word*, preserving order.

    ``None`` or an empty word (GDB probes completion with ``word=None``
    first) yields every candidate.  Shared by the RTOS command trees.
    """
    if not word:
        return list(candidates)
    return [candidate for candidate in candidates if candidate.startswith(word)]


@dataclass(frozen=True)
class CommandTreeSpec:
    """Static shape of one RTOS command tree, for tab completion.

    The four members are constants per adapter (the command vocabulary, the
    alias table, which plural kind each singular detail command maps to, and
    the live object-name probe), so ``complete_command_tree`` takes the spec
    as one argument instead of threading four unrelated parameters.
    ``preserve_spaces`` keeps a multi-word object name (e.g. a kernel service
    task whose name contains a space) intact during second-argument
    completion.
    """

    vocabulary: list[str]
    aliases: dict[str, str]
    detail_kinds: dict[str, str]
    object_names_fn: Callable[[str], list[str]]
    preserve_spaces: bool = False
    help_topics: list[str] | None = None


def complete_command_tree(
    spec: CommandTreeSpec,
    text: str,
    word: str | None,
) -> list[str]:
    """Tab-completion for an RTOS command tree's first and second argument.

    The first argument completes against ``spec.vocabulary``; the second
    argument of a singular detail command completes against live object
    names supplied by the adapter (``spec.object_names_fn(kind)``).  Shared
    by the RTOS command trees.
    """
    parts = text.split()
    if not parts:
        return prefix_candidates(word, spec.vocabulary)
    command = spec.aliases.get(parts[0].lower(), parts[0].lower())
    if command == "help" and " " in text and spec.help_topics is not None:
        return prefix_candidates(word, spec.help_topics)
    if command in spec.detail_kinds and (
        " " in text if spec.preserve_spaces else len(parts) > 1
    ):
        return prefix_candidates(word, spec.object_names_fn(spec.detail_kinds[command]))
    return prefix_candidates(word, spec.vocabulary)


# ---------------------------------------------------------------------------
# Add RTOS object to stop-time watch set for saving key typing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Watch:
    """One watched kind (``target`` is ``None``), singleton object, or backtrace."""

    kind: str
    target: str | None
    label: str
    max_depth: int | None = None


@dataclass(frozen=True)
class WatchSpec:
    """The watchable words of one RTOS command tree, for tab completion.

    ``kind_words`` maps a plural command word to its canonical kind,
    ``singular_kinds`` a singular detail word to its kind, and
    ``backtrace_word`` names the optional backtrace subcommand; all come from
    the tree's own help metadata so completion never drifts from routing.
    """

    kind_words: dict[str, str]
    singular_kinds: dict[str, str]
    aliases: dict[str, str]
    object_names_fn: Callable[[str], list[str]]
    preserve_spaces: bool = False
    backtrace_word: str | None = None


def complete_watch(spec: WatchSpec, text: str, word: str | None) -> list[str]:
    """Tab-completion for the words after ``watch``/``unwatch``.

    The first word completes against plural kinds, singular details and the
    optional backtrace command; the remainder of a singular detail or
    backtrace completes against live object/task names (or ``--max-depth``).
    Shared by the RTOS command trees.
    """
    top = [
        *spec.kind_words,
        *spec.singular_kinds,
        *([spec.backtrace_word] if spec.backtrace_word else []),
    ]
    parts = text.split()
    if len(parts) <= 1:
        return prefix_candidates(word, top)
    first = spec.aliases.get(parts[1].lower(), parts[1].lower())
    if (
        spec.backtrace_word
        and first == spec.backtrace_word
        and (len(parts) > 2 or text.endswith((" ", "\t")))
    ):
        if word and word.startswith("-"):
            return prefix_candidates(word, ["--max-depth"])
        if any(token.startswith("--max-depth") for token in parts[2:]):
            return []
        return prefix_candidates(word, spec.object_names_fn("task"))
    if first in spec.singular_kinds and (len(parts) > 2 or text.endswith((" ", "\t"))):
        return prefix_candidates(word, spec.object_names_fn(spec.singular_kinds[first]))
    return prefix_candidates(word, top)


def format_backtrace_watch_label(
    prefix: str, word: str, target: str | None, max_depth: int = 32
) -> str:
    """Format a compact, stable display label for a backtrace watch."""
    parts = [prefix, word.lower()] if prefix else [word.lower()]
    if target is not None:
        parts.append(target)
    if max_depth != 32:
        parts.append(f"--max-depth {max_depth}")
    return " ".join(parts)


def _supports_backtrace(adapter: object) -> bool:
    """Return whether *adapter* provides a concrete ``backtrace`` implementation."""
    fn = getattr(adapter, "backtrace", None)
    if not callable(fn):
        return False
    return getattr(type(adapter), "backtrace", None) is not RtosAdapter.backtrace


_watches: list[Watch] = []


def list_watches() -> list[Watch]:
    """The active watch set, in the order it was added."""
    return list(_watches)


def clear() -> None:
    """Drop every watch (session reset and adapter replacement)."""
    _watches.clear()


def _resolve_adapter_target(
    adapter: object, kind: str, target: str
) -> ObjectResolution[Any] | None:
    """Call ``adapter.resolve_target`` when the adapter implements it."""
    resolver = getattr(adapter, "resolve_target", None)
    if not callable(resolver):
        return None
    return cast(ObjectResolution[Any], resolver(kind, target))


def _validate_target(adapter: object, kind: str, target: str) -> str | None:
    """Validate that a singleton *kind*/*target* resolves on the live target."""
    try:
        resolution = _resolve_adapter_target(adapter, kind, target)
        if resolution is not None:
            if resolution.status == "ambiguous":
                return (
                    resolution.message
                    or f"{kind} {target!r}: ambiguous reference; specify a unique symbol or address"
                )
            if resolution.status != "found":
                return f"{kind} {target!r}: not found or type not enabled"
            return None
        found = (
            adapter.find_task(target)  # type: ignore[attr-defined]
            if kind == "task"
            else adapter.find_object(kind, target)  # type: ignore[attr-defined]
        )
        if found is None:
            return f"{kind} {target!r}: not found or type not enabled"
    except TARGET_ACCESS_ERRORS:
        return f"{kind} {target!r}: unreadable (target halted?)"
    return None


def add_watch(
    label: str,
    kind: str,
    target: str | None,
    *,
    max_depth: int | None = None,
) -> str | None:
    """Record one watch after validating it against the live target.

    Returns ``None`` when the watch was added, else the reason it was
    refused (unknown kind, missing object, ambiguous target, unreadable
    target). A singleton or named backtrace target must exist now; it is
    re-resolved on every stop, so a later detach renders as ``gone`` instead
    of a stale address.
    """
    adapter = active()
    if adapter is None:
        return "run `gdr init <rtos> <version>` first"
    if kind == "backtrace":
        if not _supports_backtrace(adapter):
            return "this adapter cannot unwind task stacks"
        if max_depth is not None and (max_depth < 1 or max_depth > 64):
            return "--max-depth must be between 1 and 64"
        if target is not None:
            normalized = target.strip()
            if not normalized:
                return "task name or address must not be empty"
            target = normalized
            failure = _validate_target(adapter, "task", target)
            if failure is not None:
                return failure
    else:
        if kind not in adapter.object_counts():
            return f"object kind {kind!r} is not reliably enumerable"
        if target is not None:
            failure = _validate_target(adapter, kind, target)
            if failure is not None:
                return failure
    for existing in _watches:
        if existing.kind == kind and existing.target == target:
            return f"already watching {existing.label}"
    _watches.append(Watch(kind=kind, target=target, label=label, max_depth=max_depth))
    return None


def remove_watch(kind: str, target: str | None) -> bool:
    """Drop one watch; ``False`` when the set never held it."""
    for index, entry in enumerate(_watches):
        if entry.kind == kind and entry.target == target:
            del _watches[index]
            return True
    return False


def render_watch_list() -> None:
    """List the active watch set, or note that it is empty."""
    if not _watches:
        info("no watches (add one with `<rtos> watch <kind>`)")
        return
    for index, entry in enumerate(_watches):
        info(f"[watch {index}] {entry.label}")


def render_watches() -> None:
    """Print every watched kind/object; the stop-hook entry point.

    Never raises: this runs inside GDB's event loop, where a raise would
    surface as raw noise mid-session (the same discipline as the generation
    bump beside it). One unreadable entry must not silence the rest, and an
    empty set costs a single length check.
    """
    try:
        watches = list(_watches)
        if not watches:
            return
        adapter = active()
        if adapter is None:
            return
        for entry in watches:
            # NOTE: _render_one never raises (see its guard), so one
            # unreadable entry cannot silence the rest; the entry stays
            # watched and retries on the next stop.
            _render_one(adapter, entry)
    except Exception:  # pragma: no cover - defensive event-loop guard
        return


def _render_one(adapter, entry: Watch) -> None:
    """Print one watch entry's fresh snapshot with a greppable header.

    Never raises: a stop-time read can fail for reasons the next stop heals
    (transient kernel update, dropped remote link).
    """
    try:
        _render_entry(adapter, entry)
    except Exception:  # pragma: no cover - defensive event-loop guard
        return


def _render_entry(adapter, entry: Watch) -> None:
    """One watch entry's fresh snapshot; raises on unreadable target."""
    if entry.kind == "backtrace":
        if not _supports_backtrace(adapter):
            info(
                f"[gdr watch: {entry.label}] skipped: this adapter cannot unwind task stacks"
            )
            return
        depth = entry.max_depth if entry.max_depth is not None else 32
        report = adapter.backtrace(entry.target, depth)
        if report is None:
            info(
                f"[gdr watch: {entry.label}] skipped: this adapter cannot unwind task stacks"
            )
            return
        if report.stop_reason == "task-not-found" and not report.frames:
            info(
                f"[gdr watch: {entry.label}] "
                f"{report.messages[0] if report.messages else 'gone (detached?)'}"
            )
            return
        if report.stop_reason == "ambiguous-target" and report.messages:
            info(f"[gdr watch: {entry.label}] {report.messages[0]}")
            return
        info(f"[gdr watch: {entry.label}]")
        render_backtrace_report(report)
        return
    if entry.kind not in adapter.object_counts():
        info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
        return
    if entry.target is None:
        table = (
            collect_task_table(adapter)
            if entry.kind == "task"
            else collect_object_listing(adapter, entry.kind)
        )
        if table is None:
            info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
            return
        info(f"[gdr watch: {entry.label}]")
        _render_object_table(table)
        return
    detail = collect_object_detail(adapter, entry.kind, entry.target)
    if detail is None:
        info(f"[gdr watch: {entry.label}] skipped: kind not enumerable now")
        return
    if not detail.found:
        info(f"[gdr watch: {entry.label}] {detail.message or 'gone (detached?)'}")
        return
    info(f"[gdr watch: {entry.label}]")
    print_detail(detail.pairs)


# ---------------------------------------------------------------------------
# GDB convenience functions.
# ---------------------------------------------------------------------------


_registered_functions: set[str] = set()
_registered_api_functions: set[str] = set()


if gdb is not None:  # pragma: no cover - gdb.Function exists only in GDB

    class GdrTaskFunction(gdb.Function):
        """Return a target-native task value by name, address or symbol.

        Usage: ``p $gdr_task("worker")`` or ``p $gdr_task("0x20000000")``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_task")

        @gdb_function_guard
        def invoke(self, name):
            adapter = active()
            if adapter is None:
                return gdb.Value(0)
            target = read_cstring(name) or ""
            resolution = _resolve_adapter_target(adapter, "task", target)
            if resolution is not None:
                if resolution.status == "ambiguous" and resolution.message:
                    warn(resolution.message)
                return (
                    resolution.value
                    if resolution.status == "found" and resolution.value is not None
                    else gdb.Value(0)
                )
            return adapter.find_task(target) or gdb.Value(0)

    class GdrTasksFunction(gdb.Function):
        """Return a target-native pointer array for all task values.

        Usage: ``p $gdr_tasks()[0]``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_tasks")

        @gdb_function_guard
        def invoke(self):
            adapter = active()
            return (
                make_pointer_array(list(adapter.iter_tasks()))
                if adapter
                else gdb.Value(0)
            )

    class GdrObjectFunction(gdb.Function):
        """Return a target-native object by semantic kind and target.

        Usage: ``p $gdr_object("semaphore", "work_sem")`` or
        ``p $gdr_object("semaphore", "0x20000000")``. An adapter
        returns zero when that kind is not reliably enumerable on its RTOS.
        """

        def __init__(self) -> None:
            super().__init__("gdr_object")

        @gdb_function_guard
        def invoke(self, kind, name):
            adapter = active()
            if adapter is None:
                return gdb.Value(0)
            kind_text = read_cstring(kind) or ""
            target = read_cstring(name) or ""
            resolution = _resolve_adapter_target(adapter, kind_text, target)
            if resolution is not None:
                if resolution.status == "ambiguous" and resolution.message:
                    warn(resolution.message)
                return (
                    resolution.value
                    if resolution.status == "found" and resolution.value is not None
                    else gdb.Value(0)
                )
            result = adapter.find_object(kind_text, target)
            return result if result is not None else gdb.Value(0)

    class GdrJsonFunction(gdb.Function):
        """Return one GDR API envelope for a verb as a JSON string.

        Usage: ``p $gdr_json("tasks")``. Small envelopes ride inline;
        larger ones return the chunked-transport meta frame, which the
        caller feeds to ``$gdr_json_chunk``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_json")

        @gdb_function_guard
        def invoke(self, verb, *rest):
            from core.api import transport_response

            args = [read_cstring(arg) or "" for arg in rest]
            return gdb.Value(transport_response(read_cstring(verb) or "", *args))

    class GdrJsonChunkFunction(gdb.Function):
        """Return one base64 frame of a GDR API envelope.

        Usage: ``p $gdr_json_chunk(0, "tasks")`` for the meta frame, then
        ``p $gdr_json_chunk(1, ...)`` up to the meta's ``total``.
        """

        def __init__(self) -> None:
            super().__init__("gdr_json_chunk")

        @gdb_function_guard
        def invoke(self, index, verb, *rest):
            from core.api import chunk_response

            args = [read_cstring(arg) or "" for arg in rest]
            try:
                chunk_index = int(index)
            except (TypeError, ValueError):
                chunk_index = -1
            return gdb.Value(
                chunk_response(chunk_index, read_cstring(verb) or "", *args)
            )

    class GdrDumpFunction(gdb.Function):
        """Write one GDR API envelope to a file.

        Usage: ``p $gdr_dump("/tmp/gdr-probe.json", "probe")``. Returns the
        number of bytes written, or -1 when the file could not be written: a
        scalar result is what a plugin's file transport needs, because a plugin
        that cannot carry a string through ``evaluate`` can still read a file.
        """

        def __init__(self) -> None:
            super().__init__("gdr_dump")

        @gdb_function_guard
        def invoke(self, path, verb, *rest):
            from core.api import dump_response

            args = [read_cstring(arg) or "" for arg in rest]
            return gdb.Value(
                dump_response(read_cstring(path) or "", read_cstring(verb) or "", *args)
            )


def register_functions() -> None:
    """Register generic function names, resuming after a partial failure."""
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    functions = (
        ("gdr_task", GdrTaskFunction),
        ("gdr_tasks", GdrTasksFunction),
        ("gdr_object", GdrObjectFunction),
    )
    for name, function_type in functions:
        if name in _registered_functions:
            continue
        function_type()
        _registered_functions.add(name)


def register_api_functions() -> None:
    """Register the machine-readable API transport functions.

    Kept separate from :func:`register_functions` so the human convenience
    functions and the plugin transport have independent registration
    state; both are called once per adapter setup.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    functions = (
        ("gdr_json", GdrJsonFunction),
        ("gdr_json_chunk", GdrJsonChunkFunction),
        ("gdr_dump", GdrDumpFunction),
    )
    for name, function_type in functions:
        if name in _registered_api_functions:
            continue
        function_type()
        _registered_api_functions.add(name)
