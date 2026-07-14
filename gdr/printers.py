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

from gdr.gdb_bridge import read_cstring, read_int
from gdr.layout import KernelLayout, StructLayout, read_field

# Display name for each struct type (short tag for the folded output)
_DISPLAY_NAMES: dict[str, str] = {
    "struct rt_thread": "Thread",
    "struct rt_timer": "Timer",
    "struct rt_semaphore": "Semaphore",
    "struct rt_mutex": "Mutex",
    "struct rt_event": "Event",
    "struct rt_mailbox": "Mailbox",
    "struct rt_messagequeue": "MsgQueue",
    "struct rt_memheap": "MemHeap",
    "struct rt_mempool": "MemPool",
}


def _format_field(value, kind: str) -> str:
    """Format a ``gdb.Value`` for one-line display based on its kind hint.

    Args:
        value: ``gdb.Value`` or ``None``.
        kind: Field kind hint (``"string"``, ``"ptr"``, ``"enum"``,
            ``"flags"``, or ``""`` for plain int).

    Returns:
        Human-readable string.  ``"N/A"`` for inaccessible values.
    """
    if value is None:
        return "N/A"

    if kind == "string":
        s = read_cstring(value)
        return f'"{s}"' if s else "N/A"

    if kind == "ptr":
        addr = read_int(value)
        if addr is None:
            return "N/A"
        if addr == 0:
            return "NULL"
        # Reason: for pointers to structs with a name field (e.g. mutex.owner
        # pointing to rt_thread), showing the name is far more useful than
        # a raw address.
        try:
            deref = value.dereference()
            name_val = deref["name"]
            name = read_cstring(name_val)
            if name:
                return f'"{name}"'
        except (gdb.error, gdb.MemoryError, IndexError, TypeError):
            pass
        return hex(addr)

    if kind == "enum":
        val = read_int(value)
        if val is None:
            return "N/A"
        return str(val)

    if kind == "flags":
        val = read_int(value)
        if val is None:
            return "N/A"
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
        self.display_name = _DISPLAY_NAMES.get(layout.struct_name, layout.struct_name)

    def to_string(self) -> str:
        """Return the one-line folded representation."""
        parts = []
        for f_name, field in self.layout.fields.items():
            if not field.summary:
                continue
            value = read_field(self.val, self.layout, f_name)
            formatted = _format_field(value, field.kind)
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
    """Register layout-driven pretty-printers with GDB.

    Args:
        kl: Kernel layout with struct descriptions.
    """
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    lookup_fn = _make_lookup_function(kl)
    gdb.pretty_printers.append(lookup_fn)


def unregister_printers(kl: KernelLayout) -> None:
    """Remove previously registered printers (for reload during development)."""
    if gdb is None:
        return
    # Reason: matching by function identity is fragile after reload; instead
    # we filter by checking the closure's type_map contents.
    new_list = []
    for fn in gdb.pretty_printers:
        if hasattr(fn, "__closure__") and fn.__closure__:
            for cell in fn.__closure__:
                if isinstance(cell.cell_contents, KernelLayout) and (
                    cell.cell_contents is kl
                ):
                    break
            else:
                new_list.append(fn)
        else:
            new_list.append(fn)
    gdb.pretty_printers[:] = new_list
