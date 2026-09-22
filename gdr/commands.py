"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gdr.adapter_api import HeapReport, ObjectDetail, ObjectTable, active
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_detail,
    print_table,
    warn,
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
def render_object_detail(kind: str, name: str) -> None:
    """Render one object as a vertical key/value detail block.

    The adapter supplies RTOS-neutral ``(key, value)`` pairs so no adapter
    field names leak into the generic renderer.
    """
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    requested = _canonical_kind(kind)
    if not name.strip():
        warn("object name must not be empty")
        return
    detail = collect_object_detail(adapter, requested, name)
    if detail is None:
        warn(f"object kind {requested!r} is not reliably enumerable")
        return
    if not detail.found:
        warn(detail.message or f"{requested} {name!r}: not found or type not enabled")
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


def collect_object_detail(adapter, kind: str, name: str) -> ObjectDetail | None:
    """One object's vertical detail, or ``None`` when the kind is unknown."""
    return adapter.object_detail(_canonical_kind(kind), name)


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
