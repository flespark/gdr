"""FreeRTOS merged detail tables, preserving typed navigation and waiters."""

from __future__ import annotations

from gdr.adapter_api import Cell, ColumnSpec, merged_detail_table, symbol_cell
from gdr.gdb_bridge import (
    lookup_symbol_at,
    read_int,
    source_definition_at,
    source_location_at,
)
from gdr.layout import read_field

DETAIL_SPECS = (
    ColumnSpec("Callback", kind="symbol"),
    ColumnSpec("ID", kind="symbol"),
    ColumnSpec("Waiters", kind="waiters"),
    ColumnSpec("RecvWait", kind="waiters"),
    ColumnSpec("SendWait", kind="waiters"),
    ColumnSpec("Item", kind="addresses"),
    *(
        ColumnSpec(key, kind="symbol")
        for key in ("Head", "Tail", "WriteTo", "ReadFrom", "Set")
    ),
    ColumnSpec("Owner", kind="symbol"),
)


def waiter_records(names, adapter, cache):
    """Typed waiter navigation: proven saved PC/source, otherwise the TCB.

    FreeRTOS retains no task entry field. Never infer one from a task name;
    duplicate names stay non-actionable instead of selecting the wrong task.
    """
    from freertos.adapter import iter_converted_tasks

    if names is None:
        return None
    if names and not cache:
        for task in iter_converted_tasks(adapter.layout):
            cache.setdefault(task.name, []).append(task)
    records = []
    for name in names:
        tasks = cache.get(name, [])
        task = tasks[0] if len(tasks) == 1 else None
        pc = adapter._pc_cell(task) if task is not None else None
        source = pc.source if pc is not None else None
        records.append(
            {
                "name": name,
                "address": pc.value
                if pc is not None and pc.value
                else task.address
                if task
                else None,
                "source": {
                    "file": source.fullname,
                    "name": source.name,
                    "line": source.line,
                }
                if source
                else None,
            }
        )
    return records


def detail_table(adapter, kind: str):
    """Build one table from the same bounded detail builders used by the CLI."""
    from freertos.adapter import (
        _cast_object,
        value_to_queue_object,
        value_to_timer_object,
    )
    from freertos.details import (
        event_group_detail,
        mutex_detail,
        queue_detail,
        semaphore_detail,
        stream_buffer_detail,
        timer_detail,
        waiter_summary,
    )
    from freertos.events import value_to_event_group_object
    from freertos.navigation import discover
    from freertos.streams import value_to_stream_buffer_object

    routes = {
        "timer": (value_to_timer_object, timer_detail),
        "queue": (value_to_queue_object, queue_detail),
        "semaphore": (value_to_queue_object, semaphore_detail),
        "mutex": (value_to_queue_object, mutex_detail),
        "eventgroup": (value_to_event_group_object, event_group_detail),
        "streambuffer": (value_to_stream_buffer_object, stream_buffer_detail),
    }
    if kind not in routes:
        return None
    converter, builder = routes[kind]
    rows = []
    overrides = []
    waiter_cache = {}
    for found in discover(kind, adapter.layout):
        value = _cast_object(found.address, kind, adapter.layout)
        obj = converter(value, found, adapter.layout)
        pairs = builder(obj, value, adapter.layout)
        cells: dict[str, Cell] = {}
        if kind == "queue":
            ql = adapter.layout.structs["struct QueueDefinition"]
            for key, field in (
                ("Head", "head"),
                ("Tail", "tail"),
                ("WriteTo", "write_to"),
                ("ReadFrom", "read_from"),
                ("Set", "set_container"),
            ):
                if field not in ql.fields:
                    continue
                address = read_int(read_field(value, ql, field))
                cells[key] = (
                    symbol_cell(address, lookup_symbol_at(address))
                    if address
                    else Cell("-" if address == 0 else "N/A", None)
                )
        elif kind == "mutex" and obj.holder_address:
            cells["Owner"] = Cell(obj.holder or "-", obj.holder_address)
        if kind == "timer":
            for key, address in (("Callback", obj.callback), ("ID", obj.id)):
                if address:
                    cells[key] = symbol_cell(
                        address,
                        lookup_symbol_at(address),
                        source_location_at(address)
                        if key == "Callback"
                        else source_definition_at(address),
                    )
        elif kind == "eventgroup":
            names = [waiter.task for waiter in obj.waiters]
            cells["Waiters"] = Cell(
                str(len(names)), waiter_records(names, adapter, waiter_cache)
            )
        elif kind == "streambuffer":
            for key, name in (
                ("RecvWait", obj.recv_waiter),
                ("SendWait", obj.send_waiter),
            ):
                names = [name] if name and name != "-" else []
                cells[key] = Cell(
                    name or "-", waiter_records(names, adapter, waiter_cache)
                )
        else:
            channels = (
                (("SendWait", obj.send_waiters), ("RecvWait", obj.recv_waiters))
                if kind == "queue"
                else (("Waiters", obj.recv_waiters),)
            )
            for key, names in channels:
                cells[key] = Cell(
                    waiter_summary(names), waiter_records(names, adapter, waiter_cache)
                )
            items = []
            for key, text in pairs:
                if key.startswith("Item["):
                    address_text = text
                    try:
                        address = int(address_text, 16)
                    except ValueError:
                        address = None
                    items.append({"text": f"{key}: {address_text}", "address": address})
            if items:
                cells["Item"] = Cell("; ".join(item["text"] for item in items), items)
        # Reason: queue/semaphore/mutex subtypes still carry diagnostic value;
        # unlike RT-Thread's class label, do not erase them from the table.
        rows.append(
            [("Subtype" if key == "Type" else key, text) for key, text in pairs]
        )
        overrides.append(cells)
    return merged_detail_table(
        kind, rows, cell_overrides=overrides, column_specs=DETAIL_SPECS
    )
