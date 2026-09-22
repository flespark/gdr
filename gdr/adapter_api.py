"""RTOS-neutral semantic adapter contract used by GDR's public interface."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


@dataclass
class SystemSummary:
    """RTOS-neutral system data for an adapter-owned system command."""

    kernel_version: str = "unknown"
    current_task: str | None = None
    task_count: int | None = None
    # Row label for task_count; adapters override it to name the evidence
    # source (a kernel counter vs a scheduler-list walk). Whether both
    # sources exist is adapter-specific, so the neutral default stays
    # unqualified.
    task_count_label: str = "Task count"
    tick_count: int | None = None
    scheduler_state: str = "N/A"
    state_counts: dict[str, int] = field(default_factory=dict)
    object_counts: dict[str, int] = field(default_factory=dict)
    heap_allocator: str | None = None
    heap_used: int | None = None
    heap_total: int | None = None
    heap_status: str | None = None
    extra_pairs: list[tuple[str, str]] = field(default_factory=list)
    # Adapter-owned vertical rows beyond the normalized fields (consistency
    # checks, provenance notes).  ``gdr.commands.render_system`` appends them
    # verbatim, so RTOS-specific verdict strings never leak into the core.


@dataclass(frozen=True)
class ColumnSpec:
    """Plugin-facing metadata for one list column.

    The text renderer ignores this metadata (table geometry drives its
    alignment); the JSON serializer emits it so a plugin can right-align
    numeric columns, render percentage bars and offer address navigation
    without re-parsing display strings.  Empty ``column_specs`` on a table
    means "derive from text" -- adapters fill real specs incrementally.

    Attributes:
        key: Column header (matches ``ObjectTable.headers``).
        kind: Value semantics: ``text``/``int``/``hex``/``addr``/
            ``percent``/``waiters``/``symbol``.
        align: ``left`` or ``right`` for the plugin grid.
        elastic: Whether the plugin may shrink this column on narrow widths.
        doc: One-line column meaning (tooltip source).
    """

    key: str
    kind: str = "text"
    align: str = "left"
    elastic: bool = False
    doc: str = ""


@dataclass
class ObjectTable:
    """Adapter-provided rows for a reliably enumerable object kind.

    ``elastic`` lists the headers that may shrink when the natural table
    width exceeds the terminal width (see ``gdr.gdb_bridge.print_table``),
    ordered from first to last shrink priority. Adapters own this metadata so
    renderers never guess it from header text.

    ``column_specs``/``value_rows`` are the optional plugin-facing half of
    the table: typed cell semantics and raw numeric/address values next to
    the rendered text rows.  Both default to empty so existing adapters keep
    producing text-only tables with zero changes.
    """

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    elastic: tuple[str, ...] = ()
    column_specs: tuple[ColumnSpec, ...] = ()
    value_rows: list[list[object]] | None = None


@dataclass
class ObjectDetail:
    """Vertical key/value detail for one named object.

    ``found`` is ``False`` when the object does not exist or its type is not
    enabled in the current target configuration.  ``message`` replaces the
    renderer's generic "not found" line with a precise reason -- e.g. the name
    resolved to a different object kind -- so the user gets an actionable
    redirect instead of a misleading detail block.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    found: bool = True
    message: str | None = None


@dataclass
class HeapReport:
    """One heap snapshot formatted for a neutral ``render_heap``.

    ``pairs`` are the vertical basics (Algorithm/sizes/counters), ``table``
    the optional block/occupancy table, and ``messages`` capability notes
    (heap_3 wraps malloc, no allocator, ...) printed above the table.  Both
    adapters return this shape so the core owns a single ``render_heap``
    for the RTOS heap command.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    table: ObjectTable | None = None
    messages: list[str] = field(default_factory=list)


@dataclass
class KindCapability:
    """What the active adapter can show for one semantic object kind.

    ``note`` explains a limitation the text renderer used to print as a
    table footer message (registry-only discovery, a gated column); a plugin
    renders it as an inline hint instead of scraping stderr.  ``columns``
    lists the kind's list columns when the adapter declares ``column_specs``.
    """

    enumerable: bool = False
    detail: bool = False
    columns: tuple[str, ...] = ()
    note: str | None = None


@dataclass
class Capabilities:
    """Adapter-owned capability claims, serialized by ``gdr api capabilities``.

    ``extra`` carries adapter-specific facts (core count, config probes, ...)
    as a flat dict of JSON-safe scalars.
    """

    kinds: dict[str, KindCapability] = field(default_factory=dict)
    extra: dict[str, object] = field(default_factory=dict)


_active: RtosAdapter | None = None
_active_rtos: str | None = None
_active_version: str | None = None


def register(
    adapter: RtosAdapter,
    *,
    rtos: str | None = None,
    version: str | None = None,
) -> None:
    """Register one session adapter and reject replacement by another.

    Args:
        adapter: The initialized semantic adapter.
        rtos / version: Session identity the adapter was initialized for; it
            rides in every API envelope so a plugin can show what the
            current session decodes without parsing init logs.  ``gdr reset``
            clears both.
    """
    global _active, _active_rtos, _active_version
    if _active is None:
        _active = adapter
        _active_rtos = rtos
        _active_version = version
    elif _active is not adapter:
        raise RuntimeError("an RTOS adapter is already initialized")


def active() -> RtosAdapter | None:
    return _active


def session_meta() -> tuple[str | None, str | None]:
    """The ``(rtos, version)`` identity registered at init time."""
    return _active_rtos, _active_version


def is_initialized() -> bool:
    return _active is not None


def reset() -> None:
    """Drop the semantic session so ``gdr init`` can select a new adapter.

    Composition-root code is responsible for also clearing adapter-owned
    caches (``RtosAdapter.reset``) and the bridge's loadable-ranges cache;
    this only resets the semantic session itself.
    """
    global _active, _active_rtos, _active_version
    _active = None
    _active_rtos = None
    _active_version = None


def default_capabilities(adapter: RtosAdapter) -> Capabilities:
    """Introspect a protocol adapter into a truthful, plain capability claim.

    Adapters override :meth:`RtosAdapter.capabilities` when they want to
    describe gated columns and reason text; the default derives enumerable
    kinds from ``object_counts`` and leaves detail/columns unclaimed.
    """
    kinds = {kind: KindCapability(enumerable=True) for kind in adapter.object_counts()}
    return Capabilities(kinds=kinds)


class RtosAdapter(Protocol):
    """Operations required by the stable generic GDB API."""

    def find_task(self, name: str) -> gdb.Value | None: ...

    def find_object(self, kind: str, name: str) -> gdb.Value | None: ...

    def object_counts(self) -> dict[str, int]: ...

    def object_table(self, kind: str) -> ObjectTable | None: ...

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None: ...

    def iter_tasks(self) -> Iterator[gdb.Value]: ...

    def task_table(self) -> ObjectTable: ...

    def system_summary(self) -> SystemSummary: ...

    def object_summary_table(self) -> ObjectTable | None:
        """Per-kind provenance summary for the objects command, or ``None``.

        ``None`` (the default) makes the neutral renderer fall back to the
        plain Kind/Count table from :meth:`object_counts`; an adapter that
        can show where each object came from (provenance channels) returns
        its own table.
        """
        return None

    def heap_report(self) -> HeapReport | None:
        """One heap snapshot formatted for the neutral ``render_heap``.

        ``None`` means the RTOS has no heap to report; adapters without a
        heap command return it so the neutral renderer prints nothing.
        """
        return None

    def capabilities(self) -> Capabilities | None:
        """Structured capability claims, or ``None`` for introspection.

        ``None`` makes ``gdr api capabilities`` fall back to
        :func:`default_capabilities`; adapters override this to describe
        gated columns and the config macros that unlock them.
        """
        return None

    def reset(self) -> None:
        """Clear session-scoped caches (symbol scans, snapshots).

        Called by ``gdr reset`` and the ``new_objfile`` event hook when a
        reflash/reset changed the symbol set.  No-op by default.
        """
        return None
