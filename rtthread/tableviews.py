"""Typed detail-table cells owned by the RT-Thread presentation layer."""

from __future__ import annotations

from gdr.adapter_api import Cell, ColumnSpec, symbol_cell
from gdr.gdb_bridge import (
    lookup_symbol_at,
    read_int,
    source_definition_at,
    source_location_at,
)
from gdr.layout import read_field
from rtthread.layout import KernelLayout
from rtthread.navigation import find_thread

DETAIL_SPECS = (
    ColumnSpec("Callback", kind="symbol"),
    ColumnSpec("Parameter", kind="symbol"),
    ColumnSpec("Waiters", kind="waiters"),
    ColumnSpec("RecvWait", kind="waiters"),
    ColumnSpec("SendWait", kind="waiters"),
    ColumnSpec("Slot", kind="addresses"),
)


def detail_cells(kind: str, value, layout: KernelLayout, pairs) -> dict[str, Cell]:
    """Keep typed waiter/slot values and timer navigation alongside CLI text."""
    from rtthread.adapter import value_to_timer

    cells: dict[str, Cell] = {}
    for key, text in pairs:
        if key in ("Waiters", "RecvWait", "SendWait"):
            # Reason: these strings are produced by this adapter's bounded
            # suspend-list walk; N/A must remain distinct from an empty list.
            names = text.partition("@")[2].split(",") if "@" in text else []
            records = []
            for name in names:
                thread = find_thread(name, layout)
                entry = (
                    read_int(
                        read_field(thread, layout.structs["struct rt_thread"], "entry")
                    )
                    if thread is not None and "struct rt_thread" in layout.structs
                    else None
                )
                source = source_location_at(entry) if entry else None
                records.append(
                    {
                        "name": name,
                        "address": entry,
                        "source": {
                            "file": source.fullname,
                            "name": source.name,
                            "line": source.line,
                        }
                        if source
                        else None,
                    }
                )
            cells[key] = Cell(text, None if text == "N/A" else records)
    slots = []
    for key, text in pairs:
        if key.startswith("Slot["):
            try:
                address = int(text, 16)
            except ValueError:
                address = None
            slots.append({"text": f"{key}: {text}", "address": address})
    if slots:
        cells["Slot"] = Cell(
            text="; ".join(slot["text"] for slot in slots), value=slots
        )
    if kind == "timer":
        timer = value_to_timer(value, layout)
        for key, address in (
            ("Callback", timer.callback),
            ("Parameter", timer.parameter),
        ):
            cells[key] = symbol_cell(
                address,
                lookup_symbol_at(address) if address else None,
                (
                    source_location_at(address)
                    if key == "Callback"
                    else source_definition_at(address)
                )
                if address
                else None,
            )
    return cells
