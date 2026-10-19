"""Unit tests for the typed-cell and capability contract.

These cover the neutral core pieces every adapter uses: cell construction,
table assembly that keeps text and machine values aligned, verdict
classification, and serialization of the new metadata.  Adapter-specific
column sets are asserted next to their adapters.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import (
    Any,
    cast,
)

import pytest

from core.adapter import (
    Cell,
    CheckFinding,
    ColumnSpec,
    ObjectDetail,
    ObjectTable,
    address_cell,
    check_findings,
    count_cell,
    merged_detail_table,
    number_cell,
    raw_cell,
    sections_from_pairs,
    symbol_cell,
    text_cell,
    verdict_severity,
    waiters_cell,
)
from core.api import (
    checks_to_dict,
    object_detail_to_dict,
    object_table_to_dict,
)
from core.formatting import SourceLoc
from core.gdb_bridge import parse_user_address


def _dict_list(value: object) -> list[dict[str, object]]:
    """Narrow a serialized list of objects for typed assertions."""
    assert isinstance(value, list)
    result: list[dict[str, object]] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


def test_cell_helpers_pair_text_with_their_machine_value():
    """Each column kind carries the value a plugin needs without re-parsing."""
    assert text_cell("worker").value is None
    assert number_cell(7).value == 7
    assert number_cell(None).text == "N/A"
    assert address_cell(0x2000).text == "0x2000"
    assert symbol_cell(0x1000, "worker_entry").text == "<worker_entry>"
    assert symbol_cell(0x1000, "worker_entry").value == 0x1000
    assert waiters_cell("2@a,b", ["a", "b"]).value == ["a", "b"]
    assert waiters_cell("0", []).value == []
    assert waiters_cell("N/A").value is None
    assert raw_cell("auto", 0x8).value == 0x8


def test_count_cell_keeps_states_without_a_tick_count_valueless():
    """``overdue`` and ``N/A`` are states, not numbers to fabricate."""
    assert count_cell("42").value == 42
    assert count_cell("overdue").value is None
    assert count_cell(None).text == "N/A"
    assert count_cell(None).value is None


def test_from_cells_splits_text_and_values_and_omits_untyped_rows():
    """Text and value come from one path, so they cannot drift apart."""
    table = ObjectTable.from_cells(
        ["Name", "Count"],
        [[text_cell("worker"), number_cell(3)]],
    )

    assert table.rows == [["worker", "3"]]
    assert table.value_rows == [[None, 3]]

    text_only = ObjectTable.from_cells(["Name"], [[text_cell("worker")]])
    assert text_only.value_rows is None


def test_from_specs_derives_headers_and_elastic_priority():
    """The spec list is the single source for headers and shrink flags."""
    specs = (
        ColumnSpec("Name", elastic=True),
        ColumnSpec("Count", kind="int", align="right"),
        ColumnSpec("Addr", kind="addr", elastic=True),
    )
    table = ObjectTable.from_specs(
        specs,
        [[text_cell("w"), number_cell(1), address_cell(0x10)]],
        elastic=("Addr", "Name"),
        id_key="Addr",
    )

    assert table.headers == ["Name", "Count", "Addr"]
    assert table.elastic == ("Addr", "Name")
    assert table.value_rows == [[None, 1, 0x10]]


def test_table_assembly_rejects_drift_between_text_and_values():
    """A row appended to only one of the two views is a defect, not data."""
    with pytest.raises(RuntimeError, match="column_specs"):
        ObjectTable.from_cells(
            ["Name"],
            [[text_cell("w")]],
            column_specs=(ColumnSpec("Name"), ColumnSpec("Extra")),
        )

    with pytest.raises(RuntimeError, match="elastic columns"):
        ObjectTable.from_specs(
            (ColumnSpec("Name"),),
            [[text_cell("w")]],
            elastic=("Name",),
        )

    with pytest.raises(RuntimeError, match="cells for"):
        ObjectTable.from_cells(["Name", "Count"], [[text_cell("w")]])

    with pytest.raises(RuntimeError, match="must mirror rows"):
        ObjectTable(headers=["Name"], rows=[["w"]], value_rows=[])


def test_verdict_severity_classifies_both_documented_conventions():
    """Verdict-prefixed values and named verdicts both classify."""
    assert verdict_severity("TaskCount", "ok") == "ok"
    assert verdict_severity("ListIntegrity", "fail: chain broken") == "fail"
    assert verdict_severity("HeapCrossCheck", "skipped: unreadable") == "skipped"
    assert verdict_severity("OffsetCheck", "ok") == "ok"
    assert verdict_severity("SlotCheck", "N/A") == "skipped"
    assert verdict_severity("AlignmentCheck", "size 0 must be positive") == "fail"
    assert verdict_severity("Checks", "ok") == "ok"
    assert verdict_severity("Checks", "2 failed, 3 verified") == "fail"
    assert verdict_severity("Name", "worker1") == "info"


def test_sections_from_pairs_groups_only_verdict_rows():
    """A detail section highlights the checks, never the plain facts."""
    sections = sections_from_pairs(
        [("Name", "worker1"), ("OffsetCheck", "ok"), ("SlotCheck", "bad")]
    )

    assert len(sections) == 1
    assert sections[0].title == "Checks"
    assert [(row.key, row.severity) for row in sections[0].rows] == [
        ("OffsetCheck", "ok"),
        ("SlotCheck", "fail"),
    ]
    assert sections_from_pairs([("Name", "worker1")]) == []


def test_check_findings_parses_verdicts_into_structured_rows():
    """The checks verb payload needs no string splitting on the client."""
    findings = check_findings(
        [("TaskCount", "ok"), ("Heap", "fail: blocks disagree")], scope="system"
    )

    assert [finding.verdict for finding in findings] == ["ok", "fail"]
    assert findings[0].detail == ""
    assert findings[1].detail == "blocks disagree"
    assert findings[1].scope == "system"


def test_serialized_table_carries_columns_values_kind_and_ids():
    """The envelope's table shape is what a UI builds a grid from."""
    table = ObjectTable.from_specs(
        (ColumnSpec("Name"), ColumnSpec("Addr", kind="addr")),
        [[text_cell("worker"), address_cell(0x2000)]],
        id_key="Addr",
    )

    rendered = object_table_to_dict(table, kind="task")

    assert rendered["kind"] == "task"
    assert rendered["value_rows"] == [[None, 0x2000]]
    assert rendered["ids"] == [0x2000]
    assert _dict_list(rendered["columns"])[1]["kind"] == "addr"


def test_symbol_cell_source_makes_a_located_table_only_when_present():
    """Only tables with a located cell carry ``source_rows``; others stay as before."""
    located = ObjectTable.from_specs(
        (ColumnSpec("Name"), ColumnSpec("Entry", kind="symbol")),
        [
            [
                text_cell("tshell"),
                symbol_cell(0x100, "main", SourceLoc("/src/shell.c", 12, "shell.c")),
            ]
        ],
    )
    assert located.source_rows == [[None, SourceLoc("/src/shell.c", 12, "shell.c")]]

    plain = ObjectTable.from_specs(
        (ColumnSpec("Name"), ColumnSpec("Entry", kind="symbol")),
        [[text_cell("tshell"), symbol_cell(0x100, "main")]],
    )
    assert plain.source_rows is None


def test_serialized_table_carries_source_rows_as_file_and_line():
    """The envelope names each located cell's file and line, or null."""
    table = ObjectTable.from_specs(
        (ColumnSpec("Name"), ColumnSpec("Entry", kind="symbol")),
        [
            [
                text_cell("tshell"),
                symbol_cell(0x100, "main", SourceLoc("/src/shell.c", 12, "shell.c")),
            ]
        ],
    )

    rendered = object_table_to_dict(table, kind="task")

    # Reason: "file" is GDB's resolved path; "name" is the path as the debug
    # information recorded it, which the extension falls back to.
    assert rendered["source_rows"] == [
        [
            None,
            {"file": "/src/shell.c", "name": "shell.c", "line": 12},
        ]
    ]


def test_serialized_detail_keeps_flat_pairs_and_adds_severity():
    """pairs stays the text contract; sections adds the grouped verdicts."""
    pairs = [("Name", "worker1"), ("OffsetCheck", "ok")]
    detail = ObjectDetail(pairs=pairs, sections=sections_from_pairs(pairs))

    rendered = object_detail_to_dict(detail)

    assert rendered["pairs"] == [["Name", "worker1"], ["OffsetCheck", "ok"]]
    assert rendered["sections"] == [
        {
            "title": "Checks",
            "rows": [{"key": "OffsetCheck", "value": "ok", "severity": "ok"}],
        }
    ]


def test_serialized_checks_report_findings_and_counts():
    """Counts let a badge render without walking the findings."""
    rendered = checks_to_dict(
        [
            CheckFinding("TaskCount", "ok"),
            CheckFinding("Heap", "fail", "disagree"),
            CheckFinding("TimerList", "fail", "broken"),
        ]
    )

    assert rendered["counts"] == {"fail": 2, "ok": 1}
    assert _dict_list(rendered["findings"])[1]["detail"] == "disagree"


def _specs(table):
    return {spec.key: spec for spec in table.column_specs}


def test_rows_are_objects_and_columns_the_union_of_their_properties():
    """Each object is one row; a property another object lacks reads N/A."""
    table = merged_detail_table(
        "semaphore",
        [
            [("Name", "sem_a"), ("Address", "0x2000"), ("Value", "1")],
            [
                ("Name", "sem_b"),
                ("Address", "0x2010"),
                ("Value", "0"),
                ("Policy", "PRIO"),
            ],
        ],
    )

    assert table.headers == ["Name", "Address", "Value", "Policy"]
    assert table.rows == [
        ["sem_a", "0x2000", "1", "N/A"],
        ["sem_b", "0x2010", "0", "PRIO"],
    ]


def test_address_is_the_row_identity_and_addr_kind():
    """The Address column is the id key and carries the addr kind."""
    table = merged_detail_table("mutex", [[("Name", "m"), ("Address", "0x3000")]])

    assert table.id_key == "Address"
    assert _specs(table)["Address"].kind == "addr"
    rendered = object_table_to_dict(table, kind="mutex")
    assert rendered["ids"] == ["0x3000"]


def test_repeating_keys_collapse_into_one_entry_column():
    """``Msg[n]``, ``Slot[n]`` and ``Waiter: <name>`` share a column per stem."""
    table = merged_detail_table(
        "msgqueue",
        [
            [
                ("Name", "q"),
                ("Address", "0x4000"),
                ("Msg[0]", "@0x1 payload=aa"),
                ("Msg[1]", "@0x2 payload=bb"),
            ],
            [("Name", "r"), ("Address", "0x4010")],
        ],
    )

    assert table.headers == ["Name", "Address", "Msg"]
    assert table.rows[0][2] == "Msg[0]: @0x1 payload=aa; Msg[1]: @0x2 payload=bb"
    # Reason: no entries is an empty cell, not N/A; N/A is for an absent property.
    assert table.rows[1][2] == ""


def test_a_verdict_value_marks_its_column_diag_and_others_stay_basic():
    """A consistency verdict puts its column in the diagnostic group."""
    table = merged_detail_table(
        "mailbox",
        [
            [("Name", "mb"), ("Address", "0x5000"), ("OffsetCheck", "ok")],
            [
                ("Name", "mc"),
                ("Address", "0x5010"),
                ("OffsetCheck", "fail: in_offset >= size"),
            ],
        ],
    )

    specs = _specs(table)
    assert specs["Name"].group == "basic"
    assert specs["Address"].group == "basic"
    assert specs["OffsetCheck"].group == "diag"


def test_name_is_elastic_and_the_table_is_serialized_with_groups():
    """Name may shrink on narrow widths; the serialized columns carry their group."""
    table = merged_detail_table(
        "timer", [[("Name", "t"), ("Address", "0x6000"), ("State", "active")]]
    )

    assert table.elastic == ("Name",)
    rendered = object_table_to_dict(table, kind="timer")
    columns = rendered["columns"]
    assert isinstance(columns, list)
    assert {column["key"]: column["group"] for column in columns} == {
        "Name": "basic",
        "Address": "basic",
        "State": "basic",
    }


def test_heap_dump_command_writes_a_file_or_inlines_by_terminal(monkeypatch, tmp_path):
    """``render_heap_dump`` writes the PNG to a file, or inlines it in iTerm2/Kitty."""
    from core import commands
    from core.adapter import HeapMap, HeapMapBlock

    heap = HeapMap(
        algorithm="small_mem",
        base=0,
        end=256,
        blocks=[HeapMapBlock(0, 64, True, "main"), HeapMapBlock(64, 192, False)],
    )

    class _Adapter:
        def heap_map(self):
            return heap

    infos: list[str] = []
    written: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "info", infos.append)
    monkeypatch.setattr(commands, "write_stdout", written.append)

    target = tmp_path / "heap.png"
    monkeypatch.setenv("GDR_HEAP_IMAGE", "file")
    commands.render_heap_dump("rtthread", str(target))
    assert target.read_bytes().startswith(b"\x89PNG")
    assert any("heap map written to" in line for line in infos)
    assert written == []

    monkeypatch.setenv("GDR_HEAP_IMAGE", "iterm")
    commands.render_heap_dump("rtthread", "")
    assert written and written[-1].startswith("\n\x1b]1337;File=inline=1;")
    # Reason: the file path wins when the caller names one, even on iTerm2.
    commands.render_heap_dump("rtthread", str(tmp_path / "again.png"))
    assert (tmp_path / "again.png").exists()


def test_heap_dump_reports_an_unwritable_path_without_raising(monkeypatch, tmp_path):
    """A bad path is a warning; the dump never raises into the GDB session."""
    from core import commands
    from core.adapter import HeapMap, HeapMapBlock

    class _Adapter:
        def heap_map(self):
            return HeapMap(
                algorithm="small_mem",
                base=0,
                end=64,
                blocks=[HeapMapBlock(0, 64, True)],
            )

    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(commands, "info", lambda _line: None)

    commands.render_heap_dump("rtthread", str(tmp_path / "missing-dir" / "heap.png"))

    assert warnings and "could not write the heap map" in warnings[0]


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
    from core import gdb_bridge as bridge

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
    from core.layout import KernelLayout, StructLayout
    from rtthread import adapter
    from rtthread import views as tableviews

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
    from core.layout import KernelLayout
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
    from freertos import navigation
    from freertos import views as details
    from freertos import views as tableviews
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
    from freertos import views as tableviews

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
    from freertos import navigation
    from freertos import objects as events
    from freertos import objects as streams
    from freertos import views as details
    from freertos import views as tableviews
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


def test_parse_user_address_accepts_hex_and_decimal():
    assert parse_user_address("0x20000100") == 0x20000100
    assert parse_user_address("0X20000100") == 0x20000100
    assert parse_user_address("  536874000  ") == 536874000


def test_parse_user_address_rejects_names_zero_and_garbage():
    assert parse_user_address("work_sem") is None
    assert parse_user_address("Tmr Svc") is None
    assert parse_user_address("") is None
    assert parse_user_address("   ") is None
    assert parse_user_address("0x0") is None
    assert parse_user_address("0") is None
    assert parse_user_address("0xZZ") is None
    assert parse_user_address("12px") is None
