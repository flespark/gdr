"""Typed merged tables preserve navigation without changing vertical CLI data."""

from types import SimpleNamespace
from typing import Any, cast

from gdr.adapter_api import Cell, ColumnSpec, merged_detail_table
from gdr.formatting import SourceLoc
from gdr.serialize import object_table_to_dict


def test_merge_preserves_typed_overrides_and_source_rows():
    source = SourceLoc("/src/main.c", 12, "main.c")
    table = merged_detail_table(
        "timer",
        [[("Name", "t"), ("Address", "0x1000"), ("Callback", "<cb>")]],
        cell_overrides=[{"Callback": Cell("<cb>", 0x2000, source)}],
        column_specs=[ColumnSpec("Callback", kind="symbol")],
    )
    data = cast(Any, object_table_to_dict(table))
    assert data["value_rows"][0][-1] == 0x2000
    assert data["source_rows"][0][-1] == {
        "file": "/src/main.c",
        "name": "main.c",
        "line": 12,
    }
    assert data["columns"][-1]["kind"] == "symbol"


def test_data_symbol_definition_and_code_fallback(monkeypatch):
    from gdr import gdb_bridge as bridge

    monkeypatch.setattr(bridge, "gdb", SimpleNamespace(error=RuntimeError))
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _addr: "payload+4")
    symtab = SimpleNamespace(fullname=lambda: "/src/main.c", filename="main.c")
    monkeypatch.setattr(
        bridge,
        "_lookup_gdb_symbol",
        lambda name: (
            SimpleNamespace(symtab=symtab, line=12) if name == "payload" else None
        ),
    )
    assert bridge.source_definition_at(0x1004) == SourceLoc("/src/main.c", 12, "main.c")
    monkeypatch.setattr(bridge, "lookup_symbol_at", lambda _addr: None)
    monkeypatch.setattr(bridge, "source_location_at", lambda _addr: None)
    assert bridge.source_definition_at(0x9000) is None


def test_rtthread_waiter_slot_and_parameter_cells(monkeypatch):
    from gdr.layout import KernelLayout, StructLayout
    from rtthread import adapter, tableviews

    location = SourceLoc("/src/main.c", 9, "main.c")
    monkeypatch.setattr(tableviews, "find_thread", lambda _name, _layout: object())
    monkeypatch.setattr(tableviews, "read_field", lambda *_args: 0x2000)
    monkeypatch.setattr(tableviews, "read_int", lambda value: value)
    monkeypatch.setattr(tableviews, "source_location_at", lambda _addr: location)
    monkeypatch.setattr(tableviews, "source_definition_at", lambda _addr: location)
    monkeypatch.setattr(tableviews, "lookup_symbol_at", lambda _addr: "cb")
    monkeypatch.setattr(
        adapter,
        "value_to_timer",
        lambda *_args: SimpleNamespace(callback=0x2000, parameter=0x3000),
    )
    layout = KernelLayout(
        structs={"struct rt_thread": StructLayout("struct rt_thread")}
    )
    cells = cast(
        Any,
        tableviews.detail_cells(
            "timer",
            object(),
            layout,
            [
                ("Waiters", "1@worker"),
                ("SendWait", "N/A"),
                ("Slot[0]", "0x4000"),
                ("Slot[1]", "<invalid>"),
            ],
        ),
    )
    assert cells["Waiters"].value[0]["name"] == "worker"
    assert cells["Waiters"].value[0]["source"]["line"] == 9
    assert cells["SendWait"].value is None
    assert cells["Slot"].value[0]["address"] == 0x4000
    assert cells["Slot"].value[1]["address"] is None
    assert cells["Parameter"].value == 0x3000
    assert cells["Parameter"].source == location


def test_rtthread_table_omits_redundant_type_and_tick_but_keeps_cli(monkeypatch):
    from gdr.layout import KernelLayout
    from rtthread.adapter import RtThreadAdapter

    adapter = RtThreadAdapter(KernelLayout())
    pairs = [
        ("Name", "t"),
        ("Address", "0x1000"),
        ("Type", "Timer"),
        ("KernelTick", "3"),
        ("ExpiresIn", "5"),
    ]
    monkeypatch.setattr(adapter, "_raw_objects", lambda _kind: iter([object()]))
    monkeypatch.setattr(adapter, "_detail_pairs_for", lambda *_args: pairs)
    import rtthread.adapter as module

    monkeypatch.setattr(module, "detail_cells", lambda *_args: {})
    table = adapter.detail_table("timer")
    assert table is not None
    assert table.headers == ["Name", "Address", "ExpiresIn"]
    assert dict(pairs)["KernelTick"] == "3"


def test_freertos_merged_queue_preserves_waiters_and_item_addresses(monkeypatch):
    from freertos import adapter as module
    from freertos import details, navigation, tableviews
    from freertos.layout import FreeRtosConfig, build_layout

    layout = build_layout(FreeRtosConfig(queue_sets=True))
    adapter = module.FreeRtosAdapter(layout)
    monkeypatch.setattr(module, "iter_converted_tasks", lambda _layout: iter([]))
    found = SimpleNamespace(address=0x1000)
    monkeypatch.setattr(
        tableviews,
        "read_field",
        lambda _value, _layout, field: {
            "head": 0x2100,
            "tail": 0x2200,
            "write_to": 0x2104,
            "read_from": 0x2100,
            "set_container": 0,
        }.get(field),
    )
    monkeypatch.setattr(tableviews, "read_int", lambda value: value)
    monkeypatch.setattr(
        tableviews, "lookup_symbol_at", lambda address: f"storage+{address - 0x2100}"
    )
    monkeypatch.setattr(navigation, "discover", lambda *_args: [found])
    monkeypatch.setattr(module, "_cast_object", lambda *_args: object())
    monkeypatch.setattr(
        module,
        "value_to_queue_object",
        lambda *_args: SimpleNamespace(send_waiters=["sender"], recv_waiters=[]),
    )
    monkeypatch.setattr(
        details,
        "queue_detail",
        lambda *_args: [
            ("Name", "q"),
            ("Address", "0x1000"),
            ("Type", "queue"),
            ("SendWait", "1@sender"),
            ("RecvWait", "0"),
            ("Item[0]", "0x2000"),
            ("Head", "0x2100"),
            ("Tail", "0x2200"),
            ("WriteTo", "0x2104"),
            ("ReadFrom", "0x2100"),
            ("Set", "-"),
        ],
    )
    table = cast(Any, adapter.detail_table("queue"))
    assert "Type" not in table.headers
    assert "Subtype" in table.headers
    assert table.value_rows[0][table.headers.index("SendWait")] == [
        {"name": "sender", "address": None, "source": None}
    ]
    assert table.value_rows[0][table.headers.index("Item")][0]["address"] == 0x2000
    for key, address in (
        ("Head", 0x2100),
        ("Tail", 0x2200),
        ("WriteTo", 0x2104),
        ("ReadFrom", 0x2100),
    ):
        index = table.headers.index(key)
        assert table.column_specs[index].kind == "symbol"
        assert table.value_rows[0][index] == address
        assert table.rows[0][index].startswith("<storage+")
    assert table.value_rows[0][table.headers.index("Set")] is None
    assert adapter.detail_table("unknown") is None


def test_freertos_waiters_use_proven_pc_or_tcb_without_guessing_entries(monkeypatch):
    from freertos import adapter as module
    from freertos import tableviews

    tasks = [
        SimpleNamespace(name="sender", address=0x3000),
        SimpleNamespace(name="receiver", address=0x4000),
        SimpleNamespace(name="duplicate", address=0x5000),
        SimpleNamespace(name="duplicate", address=0x6000),
    ]
    walks = []

    def converted(_layout):
        walks.append(1)
        return iter(tasks)

    monkeypatch.setattr(module, "iter_converted_tasks", converted)
    source = SourceLoc("/src/tasks.c", 27, "tasks.c")
    adapter = SimpleNamespace(
        layout=None,
        _pc_cell=lambda task: (
            Cell("<send>", 0x1100, source) if task.name == "sender" else Cell("N/A")
        ),
    )
    cache = {}
    assert tableviews.waiter_records(None, adapter, cache) is None
    assert tableviews.waiter_records([], adapter, cache) == []
    records = tableviews.waiter_records(
        ["sender", "receiver", "missing", "duplicate"], adapter, cache
    )
    assert records == [
        {
            "name": "sender",
            "address": 0x1100,
            "source": {"file": "/src/tasks.c", "name": "tasks.c", "line": 27},
        },
        {"name": "receiver", "address": 0x4000, "source": None},
        {"name": "missing", "address": None, "source": None},
        {"name": "duplicate", "address": None, "source": None},
    ]
    tableviews.waiter_records(["sender"], adapter, cache)
    assert len(walks) == 1


def test_freertos_timer_event_and_stream_typed_details(monkeypatch):
    from freertos import adapter as module
    from freertos import details, events, navigation, streams, tableviews
    from freertos.layout import FreeRtosConfig, build_layout

    adapter = module.FreeRtosAdapter(build_layout(FreeRtosConfig()))
    monkeypatch.setattr(module, "iter_converted_tasks", lambda _layout: iter([]))
    monkeypatch.setattr(
        navigation, "discover", lambda *_args: [SimpleNamespace(address=0x1000)]
    )
    monkeypatch.setattr(module, "_cast_object", lambda *_args: object())
    monkeypatch.setattr(
        module,
        "value_to_timer_object",
        lambda *_args: SimpleNamespace(callback=0x2000, id=0x3000),
    )
    monkeypatch.setattr(
        details,
        "timer_detail",
        lambda *_args: [
            ("Name", "t"),
            ("Address", "0x1000"),
            ("Callback", "cb"),
            ("ID", "id"),
        ],
    )
    location = SourceLoc("/src/main.c", 9, "main.c")
    monkeypatch.setattr(tableviews, "lookup_symbol_at", lambda _addr: "resolved")
    monkeypatch.setattr(tableviews, "source_location_at", lambda _addr: location)
    monkeypatch.setattr(tableviews, "source_definition_at", lambda _addr: location)
    table = cast(Any, adapter.detail_table("timer"))
    assert table.value_rows[0][2:] == [0x2000, 0x3000]
    assert table.source_rows[0][3] == location
    monkeypatch.setattr(
        events,
        "value_to_event_group_object",
        lambda *_args: SimpleNamespace(waiters=[SimpleNamespace(task="waiter")]),
    )
    monkeypatch.setattr(
        details, "event_group_detail", lambda *_args: [("Name", "eg"), ("Waiters", "1")]
    )
    table = cast(Any, adapter.detail_table("eventgroup"))
    assert table.value_rows[0][1] == [
        {"name": "waiter", "address": None, "source": None}
    ]
    monkeypatch.setattr(
        streams,
        "value_to_stream_buffer_object",
        lambda *_args: SimpleNamespace(recv_waiter="receiver", send_waiter=None),
    )
    monkeypatch.setattr(
        details,
        "stream_buffer_detail",
        lambda *_args: [
            ("Name", "sb"),
            ("Type", "stream"),
            ("RecvWait", "receiver"),
            ("SendWait", "-"),
        ],
    )
    table = cast(Any, adapter.detail_table("streambuffer"))
    assert table.value_rows[0][2:] == [
        [{"name": "receiver", "address": None, "source": None}],
        [],
    ]
    assert "Subtype" in table.headers
