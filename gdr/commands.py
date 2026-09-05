"""Shared RTOS-neutral renderers used by adapter-owned command trees."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gdr.adapter_api import active
from gdr.formatting import format_optional_int
from gdr.gdb_bridge import (
    gdb_command_guard,
    info,
    print_detail,
    print_table,
    warn,
)


@gdb_command_guard
def render_tasks() -> None:
    """Render the active adapter's task table."""
    adapter = active()
    if adapter is None:
        warn("run `gdr init <rtos> <version>` first")
        return
    table = adapter.task_table()
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
    detail = adapter.object_detail(requested, name)
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
    summary = adapter.system_summary()
    pairs: list[tuple[str, str]] = [
        ("Kernel version", summary.kernel_version),
        ("Current task", summary.current_task or "N/A"),
        (
            "Task count",
            str(summary.task_count) if summary.task_count is not None else "N/A",
        ),
        (
            "Tick count",
            str(summary.tick_count) if summary.tick_count is not None else "N/A",
        ),
        ("Scheduler state", summary.scheduler_state),
    ]
    pairs.extend((state, str(count)) for state, count in summary.state_counts.items())
    pairs.extend(
        (kind, str(count)) for kind, count in sorted(summary.object_counts.items())
    )
    pairs.append(("Heap allocator", summary.heap_allocator or "N/A"))
    if summary.heap_used is not None or summary.heap_total is not None:
        pairs.append(("Heap used", format_optional_int(summary.heap_used)))
        pairs.append(("Heap total", format_optional_int(summary.heap_total)))
    if summary.heap_status is not None:
        pairs.append(("Heap status", summary.heap_status))
    pairs.extend(summary.extra_pairs)
    print_detail(pairs)


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
    if requested:
        if requested == "task":
            render_tasks()
            return
        table = adapter.object_table(requested)
        if table is not None:
            for message in table.messages:
                info(message)
            print_table(table.rows, table.headers, elastic=table.elastic)
            return
        counts = adapter.object_counts()
        if requested not in counts:
            warn(f"object kind {requested!r} is not reliably enumerable")
            return
        print_table([[requested, str(counts[requested])]], ["Kind", "Count"])
        return
    # The provenance summary wins when the adapter provides one (its
    # per-object source channels); otherwise fall back to the plain
    # Kind/Count table so every RTOS shares one renderer.
    summary = adapter.object_summary_table()
    if summary is not None:
        for message in summary.messages:
            info(message)
        print_table(summary.rows, summary.headers, elastic=summary.elastic)
        return
    counts = adapter.object_counts()
    print_table(
        [[name, str(count)] for name, count in sorted(counts.items())],
        ["Kind", "Count"],
    )


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
    report = adapter.heap_report()
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
    if command in spec.detail_kinds and (
        " " in text if spec.preserve_spaces else len(parts) > 1
    ):
        return prefix_candidates(word, spec.object_names_fn(spec.detail_kinds[command]))
    return prefix_candidates(word, spec.vocabulary)
