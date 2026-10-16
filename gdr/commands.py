"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from gdr import heapmap
from gdr.adapter_api import HeapReport, ObjectDetail, ObjectTable, active
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_detail,
    print_table,
    warn,
    write_stdout,
)
from gdr.serialize import system_summary_pairs


@gdb_command_guard
def render_tasks() -> None:
    """Render the active adapter's task table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    table = collect_task_table(adapter)
    for message in table.messages:
        info(message)
    print_table(table.rows, table.headers, elastic=table.elastic)


@gdb_command_guard
def render_object_detail(kind: str, target: str) -> None:
    """Render one object's complete detail as a Key: Value block.

    *target* is a registry name, an explicit ``0x``-hex or decimal address,
    or a plain C identifier naming the object's static symbol or handle.

    # Reason: terminal detail is focused and lossless; merged tables belong to
    # the API/Panel, not to a command selecting one object by name or address.
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
    for message in table.messages:
        info(message)
    print_table(table.rows, table.headers, elastic=table.elastic)


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
    otherwise (shape errors carry *usage*), so both adapters share one
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
    # Reason: flags may follow the path, so compatibility is checked after
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
    from gdr.api import current_generation

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
        for message in report.table.messages:
            info(message)
        print_table(
            report.table.rows, report.table.headers, elastic=report.table.elastic
        )


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
