"""Unit tests for the merged per-kind detail table (pure, no GDB)."""

from __future__ import annotations

from gdr.adapter_api import merged_detail_table
from gdr.serialize import object_table_to_dict


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
    from gdr import commands
    from gdr.adapter_api import HeapMap, HeapMapBlock

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
    from gdr import commands
    from gdr.adapter_api import HeapMap, HeapMapBlock

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
