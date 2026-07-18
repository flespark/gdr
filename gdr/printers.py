"""Layout-driven pretty-printer framework.

Registers GDB pretty-printers that fold kernel structs into one-line
summaries.  The printers are fully driven by ``StructLayout`` descriptions —
no struct-specific code is needed.  Fields marked ``summary=True`` in the
layout appear in the folded output; all others are left to GDB's default
recursive display.

This follows the Asterinas approach: wrapper types (locks, IPC objects,
threads) are the primary source of GDB output noise.  Folding them into
one-line summaries improves all display paths (``p``, ``bt full``,
``info locals``, watchpoint hits).
"""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import lookup_symbol_at, read_cstring, read_int
from gdr.layout import KernelLayout, StructField, StructLayout, read_field, read_path

_PRINTER_MARKER = "_gdr_layout_printer"


def _format_field(value, field: StructField) -> str:
    """Format a ``gdb.Value`` for one-line display based on its field hint.

    Args:
        value: ``gdb.Value`` or ``None``.
        field: ``StructField`` carrying ``kind`` and optional ``enum_map``.

    Returns:
        Human-readable string.  ``"N/A"`` for inaccessible values.
    """
    if value is None:
        return "N/A"

    kind = field.kind

    if kind == "string":
        s = read_cstring(value)
        return f'"{s}"' if s else "N/A"

    if kind == "ptr":
        addr = read_int(value)
        if addr is None:
            return "N/A"
        if addr == 0:
            return "NULL"
        if field.pointee_string_path is not None:
            try:
                pointee = read_path(value.dereference(), field.pointee_string_path)
                text = read_cstring(pointee)
                if text:
                    return f'"{text}"'
            except (gdb.error, gdb.MemoryError, IndexError, TypeError):
                pass
        symbol = lookup_symbol_at(addr)
        if symbol is not None:
            return f"<{symbol}>"
        return hex(addr)

    if kind == "enum":
        val = read_int(value)
        if val is None:
            return "N/A"
        # Reason: symbolic state names ("READY") beat raw ints ("3") at a
        # glance when an adapter supplies an enum map.
        if field.enum_map is not None:
            name = field.enum_map.get(val)
            if name is not None:
                return name
        return str(val)

    if kind == "flags":
        val = read_int(value)
        if val is None:
            return "N/A"
        if field.enum_map is not None:
            names = [n for bit, n in field.enum_map.items() if val & bit]
            if names:
                return "|".join(names)
        return hex(val)

    # Default: plain integer
    val = read_int(value)
    if val is None:
        return "N/A"
    return str(val)


class LayoutPrinter:
    """Pretty-printer that folds a struct into a one-line summary.

    Only fields with ``summary=True`` are shown; GDB's default display
    handles the rest when the user drills down.
    """

    def __init__(self, val: gdb.Value, layout: StructLayout):
        self.val = val
        self.layout = layout
        self.display_name = layout.display_name or layout.struct_name

    def to_string(self) -> str:
        """Return the one-line folded representation."""
        parts = []
        for f_name, field in self.layout.fields.items():
            if not field.summary:
                continue
            value = read_field(self.val, self.layout, f_name)
            formatted = _format_field(value, field)
            parts.append(f"{f_name}={formatted}")
        return f"{self.display_name}({', '.join(parts)})"

    def display_hint(self) -> str:
        """Hint GDB that this is a one-line aggregate."""
        return "string"


def _make_lookup_function(kl: KernelLayout):
    """Create a pretty-printer lookup function for GDB.

    The returned function is registered with ``gdb.pretty_printers``.  GDB
    calls it for every value it needs to display; it returns a
    ``LayoutPrinter`` if the value's type matches a known struct, else
    ``None``.
    """

    # Build a mapping from type tag to StructLayout for quick lookup
    type_map: dict[str, StructLayout] = {}
    for struct_name, layout in kl.structs.items():
        if struct_name.startswith("struct "):
            tag = struct_name[len("struct ") :]
            type_map[tag] = layout

    def lookup_function(val: gdb.Value) -> LayoutPrinter | None:
        try:
            type_tag = val.type.tag
        except AttributeError:
            return None
        if type_tag is None:
            return None
        layout = type_map.get(type_tag)
        if layout is None:
            return None
        return LayoutPrinter(val, layout)

    return lookup_function


def register_printers(kl: KernelLayout) -> None:
    """Register layout-driven pretty-printers with GDB once.

    Args:
        kl: Kernel layout with struct descriptions.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    # Reason: GDB retains global printer lookups across repeated `source gdr.py`.
    if any(_is_gdr_printer(printer) for printer in gdb.pretty_printers):
        return
    lookup_fn = _make_lookup_function(kl)
    setattr(lookup_fn, _PRINTER_MARKER, True)
    gdb.pretty_printers.append(lookup_fn)


def unregister_printers() -> None:
    """Remove GDR printers for an explicit development reload."""
    if gdb is None:
        return
    gdb.pretty_printers[:] = [
        printer for printer in gdb.pretty_printers if not _is_gdr_printer(printer)
    ]


def _is_gdr_printer(printer: object) -> bool:
    """Return whether a lookup function was created by GDR."""
    return getattr(printer, _PRINTER_MARKER, False) is True
