"""RTOS-neutral semantic adapter contract used by GDR's public interface."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .formatting import (
    SourceLoc,
    format_address,
    format_optional_int,
    format_symbol_or_address,
)

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


@dataclass
class SystemSummary:
    """RTOS-neutral system data for an adapter-owned system command."""

    current_task: str | None = None
    task_count: int | None = None
    # Row label for task_count; adapters override it to name the evidence
    # source (a kernel counter vs a scheduler-list walk). Whether both
    # sources exist is adapter-specific, so the neutral default stays
    # unqualified.
    task_count_label: str = "Task number"
    tick_count: int | None = None
    # ``None`` when the adapter cannot read the scheduler lock/ISR nest counters;
    # the summary then omits the row instead of printing a fabricated value.
    scheduler_state: str | None = None
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
            ``percent``/``waiters``/``symbol``/``pc``/``enum``/``addresses``.
        align: ``left`` or ``right`` for the plugin grid.
        elastic: Whether the plugin may shrink this column on narrow widths.
        doc: One-line column meaning (tooltip source).
        group: ``basic`` for an identity/state property, ``diag`` for a
            consistency verdict column (the panel's child checkboxes group on it).
    """

    key: str
    kind: str = "text"
    align: str = "left"
    elastic: bool = False
    doc: str = ""
    group: str = "basic"


@dataclass(frozen=True)
class Cell:
    """One table cell: the terminal text plus its machine value.

    ``value`` is ``None`` for columns whose text already is the value (names,
    states, labels).  Numeric, address, waiter and symbol columns carry their
    machine form, so a plugin can sort, right-align, draw a percentage bar or
    open the memory view without re-parsing a rendered string.  ``source`` is
    the ``(fullname, line)`` of the code a symbol cell names, when it has line
    info, so a plugin can open the source without resolving it itself.
    """

    text: str
    value: object = None
    source: SourceLoc | None = None


def text_cell(value: str) -> Cell:
    """A cell whose text is its value (names, states, labels)."""
    return Cell(text=value)


def raw_cell(text: str, value: object = None) -> Cell:
    """A cell with an explicit text and machine value (enums, counts)."""
    return Cell(text=text, value=value)


def number_cell(value: int | None) -> Cell:
    """An integer column: ``N/A`` text with a ``None`` value when absent."""
    return Cell(text=format_optional_int(value), value=value)


def address_cell(value: int | None) -> Cell:
    """An address column: hexadecimal text, integer value."""
    return Cell(text=format_address(value), value=value)


def symbol_cell(
    address: int | None,
    symbol: str | None = None,
    source: SourceLoc | None = None,
) -> Cell:
    """A symbol-or-address column; the value stays the raw address."""
    return Cell(
        text=format_symbol_or_address(address, symbol),
        value=address,
        source=source,
    )


def waiters_cell(text: str, names: Sequence[str] | None = None) -> Cell:
    """A waiter column; the value is the waiter name list when known."""
    return Cell(text=text, value=list(names) if names is not None else None)


def count_cell(text: str | None) -> Cell:
    """A tick/remaining-count cell whose value is numeric only when it can be.

    A wrap-safe ``ExpiresIn`` renders ``overdue`` and an inactive timer renders
    ``N/A``; those states have no tick count, so the machine value stays
    ``None`` rather than a fabricated number.
    """
    rendered = text or "N/A"
    try:
        value = int(rendered)
    except ValueError:
        value = None
    return Cell(text=rendered, value=value)


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
    # Per-cell ``(fullname, line)`` source locations mirroring ``rows``; None
    # when no cell in the table has one, so most tables stay unchanged.
    source_rows: list[list[SourceLoc | None]] | None = None
    # Column whose value is the row's stable object identity (usually
    # ``Addr``); the serializer turns it into row ``ids`` a UI can key on
    # across refreshes.  Empty means the table has no stable identity.
    id_key: str = ""
    # Actionable build/config advice, separate from general CLI/runtime messages.
    build_hints: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Reason: rows and value_rows are two views of one row set, so a
        # width mismatch means a cell was appended to only one of them -- the
        # exact failure that would silently shift every typed column after it.
        if self.value_rows is None:
            return
        if len(self.value_rows) != len(self.rows):
            raise RuntimeError("ObjectTable value_rows must mirror rows")
        for row, values in zip(self.rows, self.value_rows, strict=True):
            if len(values) != len(row):
                raise RuntimeError("ObjectTable value_rows must mirror row width")
        if self.source_rows is not None and len(self.source_rows) != len(self.rows):
            raise RuntimeError("ObjectTable source_rows must mirror rows")

    @classmethod
    def from_specs(
        cls,
        specs: Sequence[ColumnSpec],
        cell_rows: Sequence[Sequence[Cell]],
        *,
        messages: Sequence[str] = (),
        elastic: Sequence[str] | None = None,
        id_key: str = "",
    ) -> ObjectTable:
        """Build a table whose headers come from the column specs.

        Taking the column contract as the single source removes any chance of
        a header list and a spec list disagreeing about a column's position.
        ``elastic`` stays explicit because a column's shrink *priority* is not
        its display order; ``None`` falls back to spec order.
        """
        return cls.from_cells(
            [spec.key for spec in specs],
            cell_rows,
            messages=messages,
            elastic=(
                tuple(elastic)
                if elastic is not None
                else tuple(spec.key for spec in specs if spec.elastic)
            ),
            column_specs=tuple(specs),
            id_key=id_key,
        )

    @classmethod
    def from_cells(
        cls,
        headers: Sequence[str],
        cell_rows: Sequence[Sequence[Cell]],
        *,
        messages: Sequence[str] = (),
        elastic: Sequence[str] = (),
        column_specs: Sequence[ColumnSpec] = (),
        id_key: str = "",
    ) -> ObjectTable:
        """Build a table from typed cells, keeping text and values aligned.

        Text and machine value come from one construction path, so a column
        cannot be typed into the wrong position.  ``value_rows`` stays empty
        when every cell is text-only, so a text-only table serializes exactly
        as it did before typing was introduced.
        """
        if column_specs and len(column_specs) != len(headers):
            raise RuntimeError(
                f"column_specs ({len(column_specs)}) must match "
                f"headers ({len(headers)})"
            )
        if column_specs and elastic:
            flagged = {spec.key for spec in column_specs if spec.elastic}
            unflagged = [key for key in elastic if key not in flagged]
            if unflagged:
                raise RuntimeError(
                    f"elastic columns are not flagged in the specs: {unflagged}"
                )
        rows: list[list[str]] = []
        values: list[list[object]] = []
        sources: list[list[SourceLoc | None]] = []
        typed = False
        located = False
        for cell_row in cell_rows:
            if len(cell_row) != len(headers):
                raise RuntimeError(
                    f"row has {len(cell_row)} cells for {len(headers)} headers"
                )
            rows.append([cell.text for cell in cell_row])
            values.append([cell.value for cell in cell_row])
            sources.append([cell.source for cell in cell_row])
            typed = typed or any(cell.value is not None for cell in cell_row)
            located = located or any(cell.source is not None for cell in cell_row)
        return cls(
            headers=list(headers),
            rows=rows,
            messages=list(messages),
            elastic=tuple(elastic),
            column_specs=tuple(column_specs),
            value_rows=values if typed else None,
            source_rows=sources if located else None,
            id_key=id_key,
        )


DetailSeverity = Literal["info", "ok", "fail", "skipped"]

# Verdict words the diagnostics modules prefix a value with; see
# ``verdict_severity`` for the two conventions in use.
_VERDICT_WORDS: dict[str, DetailSeverity] = {
    "ok": "ok",
    "fail": "fail",
    "skipped": "skipped",
}


@dataclass(frozen=True)
class DetailRow:
    """One vertical detail row with its semantic severity.

    ``severity`` is ``info`` for a plain fact and ``ok``/``fail``/``skipped``
    for a consistency verdict, so a plugin badges checks without parsing them.
    """

    key: str
    value: str
    severity: DetailSeverity = "info"


@dataclass(frozen=True)
class DetailSection:
    """A named group of detail rows (the consistency checks, say)."""

    title: str
    rows: tuple[DetailRow, ...] = ()


@dataclass(frozen=True)
class CheckFinding:
    """One consistency-check result for the ``checks`` verb.

    ``verdict`` is the parsed severity and ``detail`` the reason after the
    verdict's colon, so a client renders a badge and a reason without
    splitting strings itself.
    """

    name: str
    verdict: DetailSeverity
    detail: str = ""
    scope: str = ""


def verdict_severity(key: str, value: str) -> DetailSeverity:
    """Classify one detail row as a verdict row or a plain fact.

    Two deliberate conventions exist across the diagnostics modules, and both
    are matched here:

    * a verdict-prefixed value -- ``ok``, ``fail: reason``,
      ``skipped: reason`` (the ``*_checks`` helpers' return form);
    * a named verdict -- a ``*Check`` key whose value is ``ok`` or the failure
      reason (the inline style), plus an aggregated ``Checks`` row whose value
      is ``ok`` or an ``N failed`` summary.

    Anything else is a plain fact and stays ``info``.
    """
    word = value.partition(":")[0].strip().lower()
    severity = _VERDICT_WORDS.get(word)
    if severity is not None:
        return severity
    if key.endswith("Check"):
        text = value.strip().lower()
        if text == "ok":
            return "ok"
        if text.startswith(("n/a", "unavailable", "invalid", "<invalid>")):
            return "skipped"
        return "fail"
    if key == "Checks":
        return "ok" if value.strip().lower().startswith("ok") else "fail"
    return "info"


def sections_from_pairs(
    pairs: Sequence[tuple[str, str]], *, title: str = "Checks"
) -> list[DetailSection]:
    """Group the verdict rows of a flat detail block into one section.

    Adapters that render checks inline call this once so the JSON view carries
    per-row severity while ``pairs`` keeps printing the complete flat block
    unchanged.  Row order is preserved and a block with no verdict rows yields
    no sections.
    """
    checks = tuple(
        DetailRow(key=key, value=value, severity=verdict_severity(key, value))
        for key, value in pairs
        if verdict_severity(key, value) != "info"
    )
    return [DetailSection(title=title, rows=checks)] if checks else []


def check_findings(
    results: Sequence[tuple[str, str]], *, scope: str = ""
) -> list[CheckFinding]:
    """Convert ``(name, "ok"|"fail: reason"|"skipped: reason")`` results."""
    findings: list[CheckFinding] = []
    for name, status in results:
        head, _, rest = status.partition(":")
        severity = _VERDICT_WORDS.get(head.strip().lower(), "info")
        findings.append(
            CheckFinding(
                name=name,
                verdict=severity,
                detail=rest.strip() if severity != "ok" else "",
                scope=scope,
            )
        )
    return findings


# Detail keys whose number of entries varies per object: ``Msg[3]``,
# ``Slot[0]``, ``Waiter: <name>``. They collapse into one column per stem.
_DYNAMIC_DETAIL_KEY = re.compile(r"^(?P<stem>.+?)(?:\[\d+\]|: .+)$")


def _detail_stem(key: str) -> str:
    """The column a detail key belongs to: its stem when the key is dynamic."""
    match = _DYNAMIC_DETAIL_KEY.match(key)
    return match.group("stem") if match else key


def merged_detail_table(
    kind: str,
    rows: Sequence[Sequence[tuple[str, str]]],
    *,
    id_key: str = "Address",
    cell_overrides: Sequence[dict[str, Cell]] = (),
    column_specs: Sequence[ColumnSpec] = (),
) -> ObjectTable:
    """Merge the detail blocks of every object of one kind into one table.

    One row per object. Columns are the union of the detail keys in first-seen
    order; a key that repeats per entry (``Msg[n]``, ``Slot[n]``,
    ``Waiter: <name>``) collapses into one column whose cell lists its entries.
    A column is ``diag`` when any of its values is a consistency verdict
    (``verdict_severity``), so the panel's child checkboxes can group them.
    ``N/A`` marks a property an object does not carry; an empty entry cell
    means the object has no entries of that kind. Adapter-supplied
    ``cell_overrides`` and ``column_specs`` preserve machine values and source
    navigation without decoding RTOS detail strings in the core.
    """
    del kind  # the kind is carried by the table envelope, not the columns
    declared = {spec.key: spec for spec in column_specs}
    order: list[str] = []
    severities: dict[str, DetailSeverity] = {}
    dynamic: set[str] = set()
    cells: list[dict[str, list[str]]] = []
    for pairs in rows:
        row: dict[str, list[str]] = {}
        for key, value in pairs:
            stem = _detail_stem(key)
            if stem not in severities:
                order.append(stem)
                severities[stem] = "info"
            verdict = verdict_severity(key, value)
            if verdict != "info":
                severities[stem] = verdict
            if stem == key:
                row[stem] = [value]
            else:
                dynamic.add(stem)
                row.setdefault(stem, []).append(f"{key}: {value}")
        cells.append(row)
    specs = tuple(
        ColumnSpec(
            key=stem,
            kind=declared[stem].kind
            if stem in declared
            else "addr"
            if stem == id_key
            else "text",
            align=declared[stem].align if stem in declared else "left",
            doc=declared[stem].doc if stem in declared else "",
            elastic=stem == "Name",
            group="diag" if severities[stem] != "info" else "basic",
        )
        for stem in order
    )
    table_rows: list[list[Cell]] = []
    for index, row in enumerate(cells):
        overrides = cell_overrides[index] if index < len(cell_overrides) else {}
        line: list[Cell] = []
        for stem in order:
            if stem in dynamic:
                text = "; ".join(row.get(stem, []))
            else:
                text = row[stem][0] if stem in row else "N/A"
            line.append(overrides.get(stem, Cell(text=text, value=text)))
        table_rows.append(line)
    return ObjectTable.from_specs(
        specs,
        table_rows,
        elastic=("Name",) if "Name" in order else (),
        id_key=id_key if id_key in order else "",
    )


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
    # Grouped view of the verdict rows inside ``pairs`` (see
    # ``sections_from_pairs``).  ``pairs`` stays the complete flat block the
    # text renderer prints, so sections add severity without reordering or
    # replacing the terminal output.
    sections: list[DetailSection] = field(default_factory=list)


@dataclass
class HeapReport:
    """One heap snapshot formatted for a neutral ``render_heap``.

    ``pairs`` are the vertical basics (Algorithm/sizes/counters), ``table``
    the optional block/occupancy table, and ``messages`` capability notes
    (heap_3 wraps malloc, no allocator, ...) printed above the table.  Both
    adapters return this shape so the core owns a single ``render_heap``
    for the RTOS heap command.  ``table_title`` names what the table lists
    (e.g. free blocks vs. used blocks per thread) so one label never has to
    stand for two different tables.
    """

    pairs: list[tuple[str, str]] = field(default_factory=list)
    table: ObjectTable | None = None
    messages: list[str] = field(default_factory=list)
    table_title: str = ""
    # Verdict rows (system-wide consistency checks) grouped as detail sections,
    # so a client shows them as diagnostic columns of the heap table.
    sections: list[DetailSection] = field(default_factory=list)


@dataclass(frozen=True)
class HeapMapBlock:
    """One heap block for the heap-dump bitmap: a span and who owns it.

    ``owner`` is the allocating thread's name when the allocator records it
    (an allocator's owner tracing), ``None`` otherwise; free blocks carry ``used=False``.
    """

    address: int
    size: int
    used: bool
    owner: str | None = None


@dataclass
class HeapMap:
    """The heap's block layout, the input of ``<rtos> heap dump``.

    ``base``/``end`` bound the arena; ``blocks`` are in address order. ``owners``
    tells whether owner names exist for this allocator (False leaves every used
    block's owner as ``N/A``). ``truncated`` marks a walk that stopped early.
    """

    algorithm: str
    base: int
    end: int
    blocks: list[HeapMapBlock] = field(default_factory=list)
    owners: bool = False
    truncated: bool = False
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
    # Only suggestions that can improve the target build belong in the panel.
    build_hints: list[str] = field(default_factory=list)


_active: RtosAdapter | None = None
_active_rtos: str | None = None
_active_version: str | None = None
_active_verification_note: str | None = None


def register(
    adapter: RtosAdapter,
    *,
    rtos: str | None = None,
    version: str | None = None,
    verification_note: str | None = None,
) -> None:
    """Register one session adapter and reject replacement by another.

    Args:
        adapter: The initialized semantic adapter.
        rtos / version: Session identity the adapter was initialized for; it
            rides in every API envelope so a plugin can show what the
            current session decodes without parsing init logs.  ``gdr reset``
            clears both.
        verification_note: Why the target could not confirm this adapter, when
            that happened. A plugin cannot read ``gdr init``'s console output,
            so the note is kept on the session and repeated in every later
            envelope's ``warnings`` (see ``gdr.api``); ``None`` means the
            target confirmed the request.
    """
    global _active, _active_rtos, _active_version, _active_verification_note
    if _active is None:
        _active = adapter
        _active_rtos = rtos
        _active_version = version
        _active_verification_note = verification_note
    elif _active is not adapter:
        raise RuntimeError("an RTOS adapter is already initialized")


def active() -> RtosAdapter | None:
    return _active


def session_meta() -> tuple[str | None, str | None]:
    """The ``(rtos, version)`` identity registered at init time."""
    return _active_rtos, _active_version


def verification_note() -> str | None:
    """Why the target could not confirm the registered adapter, if it could not.

    ``None`` means the target's own evidence agreed with the request. The API
    layer prepends this to every envelope's warnings, so a client shows the
    same caveat the prompt warned about instead of silently trusting reads
    that may not mean anything.
    """
    return _active_verification_note


def is_initialized() -> bool:
    return _active is not None


def reset() -> None:
    """Drop the semantic session so ``gdr init`` can select a new adapter.

    Composition-root code is responsible for also clearing adapter-owned
    caches (``RtosAdapter.reset``) and the bridge's loadable-ranges cache;
    this only resets the semantic session itself.
    """
    global _active, _active_rtos, _active_version, _active_verification_note
    _active = None
    _active_rtos = None
    _active_version = None
    _active_verification_note = None


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

    def find_task(self, target: str) -> gdb.Value | None: ...

    def find_object(self, kind: str, target: str) -> gdb.Value | None: ...

    def object_counts(self) -> dict[str, int]: ...

    def object_table(self, kind: str) -> ObjectTable | None: ...

    def object_detail(self, kind: str, target: str) -> ObjectDetail | None: ...

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

    def detail_table(self, kind: str) -> ObjectTable | None:
        """Every object of one kind as a merged detail table, or ``None``.

        Rows are the objects' full detail blocks (one row each), columns the
        union of their properties, with diagnostic verdict columns grouped as
        ``diag``. ``None`` means the adapter has no detail table for the kind;
        callers fall back to :meth:`object_table`.
        """
        del kind
        return None

    def heap_map(self) -> HeapMap | None:
        """The heap's block layout for ``<rtos> heap dump``, or ``None``.

        ``None`` means the adapter has no heap to draw.
        """
        return None

    def reset(self) -> None:
        """Clear session-scoped caches (symbol scans, snapshots).

        Called by ``gdr reset`` and the ``new_objfile`` event hook when a
        reflash/reset changed the symbol set.  No-op by default.
        """
        return None

    def diagnostics_report(self) -> list[CheckFinding] | None:
        """System-wide consistency findings for the ``checks`` verb.

        ``None`` means the adapter has no system-level checks; otherwise it
        returns the same conclusions its system view renders, structured so a
        plugin can badge them (``gdr.api`` maps this onto the verb).
        """
        return None
