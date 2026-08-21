"""Unit tests for RT-Thread single-object diagnostics."""

from __future__ import annotations

import rtthread.diagnostics as detail
from gdr.layout import KernelLayout, StructField, StructLayout
from rtthread.adapter import (
    Mailbox,
    MemoryPool,
    MessageQueue,
    Thread,
    Timer,
)
from rtthread.layout import ThreadState


def test_thread_detail_keeps_error_and_remaining_tick_in_detail(monkeypatch):
    """Thread diagnostics stay in detail and never widen the shared task table."""
    monkeypatch.setattr(detail, "lookup_symbol_at", lambda _address: None)
    thread = Thread(
        name="worker1",
        address=0x1000,
        state=int(ThreadState.READY),
        current_priority=20,
        init_priority=20,
        error=5,
        remaining_tick=123,
    )

    pairs = detail.thread_detail(thread)
    pairs_dict = dict(pairs)

    assert pairs_dict["Error"] == "5"
    assert pairs_dict["RemainingTick"] == "123"


def test_timer_detail_shows_parameter(monkeypatch):
    """Timer detail exposes the callback parameter pointer."""
    monkeypatch.setattr(detail, "lookup_symbol_at", lambda _address: None)
    timer = Timer(name="heartbeat", callback=0x2000, parameter=0x3000, active=True)

    pairs = dict(detail.timer_detail(timer))

    assert pairs["Parameter"] == "0x3000"
    assert pairs["Callback"] == "0x2000"
    assert pairs["TimerType"] == "hard"


def test_mailbox_detail_without_raw_value_omits_slots():
    """The raw slot walk only runs when the value and layout are provided."""
    mailbox = Mailbox(name="input", entry=2, size=8, address=0x1000)

    pairs = dict(detail.mailbox_detail(mailbox))

    assert pairs["Entry"] == "2"
    assert "SlotCheck" not in pairs


def test_mailbox_detail_validates_offsets_and_walks_slots(monkeypatch):
    """FIFO slot walk reports invalid offsets and reads pool slots."""
    mailbox = Mailbox(
        name="input", entry=2, size=8, in_offset=3, out_offset=1, address=0x1000
    )
    layout = KernelLayout(
        structs={"struct rt_mailbox": StructLayout("struct rt_mailbox")}
    )
    value = object()
    read_calls: list[tuple[int, int]] = []

    def fake_read_field(_value, _sl, field_name):
        return {
            "msg_pool": 0x8000,
        }.get(field_name)

    def fake_read_int(field):
        return field

    def fake_read_bytes(_addr, size):
        read_calls.append((_addr, size))
        return (1).to_bytes(size, "little")

    monkeypatch.setattr(detail, "read_field", fake_read_field)
    monkeypatch.setattr(detail, "read_int", fake_read_int)
    monkeypatch.setattr(detail, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.mailbox_detail(mailbox, value, layout))

    assert pairs["MsgPool"] == "0x8000"
    assert pairs["Slot[1]"] == "0x1"
    assert pairs["Slot[2]"] == "0x1"
    assert pairs["OffsetCheck"] == "ok"
    assert read_calls == [(0x8000 + 1 * 4, 4), (0x8000 + 2 * 4, 4)]


def test_mailbox_detail_reports_out_of_range_offsets(monkeypatch):
    """Offsets outside the ring capacity are flagged as invalid."""
    mailbox = Mailbox(
        name="input", entry=8, size=4, in_offset=2, out_offset=4, address=0x1000
    )
    layout = KernelLayout(
        structs={"struct rt_mailbox": StructLayout("struct rt_mailbox")}
    )
    monkeypatch.setattr(detail, "read_field", lambda _v, _sl, _f: 0x8000)
    monkeypatch.setattr(detail, "read_int", lambda field: field)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: b"\x00\x00\x00\x00")
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.mailbox_detail(mailbox, object(), layout))

    assert "OffsetCheck" in pairs
    assert "out_offset 4 >= size 4" in pairs["OffsetCheck"]
    assert "entry 8 > size 4" in pairs["OffsetCheck"]


def test_mailbox_detail_reports_unavailable_slot_memory(monkeypatch):
    """An unreadable message pool preserves the offset verdict and slot status."""
    mailbox = Mailbox(name="input", entry=1, size=4, address=0x1000)
    layout = KernelLayout(
        structs={"struct rt_mailbox": StructLayout("struct rt_mailbox")}
    )
    monkeypatch.setattr(detail, "read_field", lambda _v, _sl, _f: None)
    monkeypatch.setattr(detail, "read_int", lambda field: field)

    pairs = dict(detail.mailbox_detail(mailbox, object(), layout))

    assert pairs["OffsetCheck"] == "ok"
    assert pairs["MsgPool"] == "N/A"
    assert pairs["SlotCheck"] == "N/A"


def test_messagequeue_detail_validates_node_counts(monkeypatch):
    """MQ node walk compares active/free nodes against entry and max_msgs."""
    msgqueue = MessageQueue(
        name="work", entry=2, msg_size=16, max_msgs=8, address=0x2000
    )
    layout = KernelLayout(
        structs={"struct rt_messagequeue": StructLayout("struct rt_messagequeue")}
    )
    node_addrs = [0x9000, 0x9010]
    free_addrs = [0x9100 + 0x10 * i for i in range(6)]

    def fake_read_field(_value, _sl, field_name):
        return {
            "msg_queue_head": node_addrs[0],
            "msg_queue_free": free_addrs[0],
            "msg_pool": 0x8000,
        }.get(field_name)

    def fake_read_int(field):
        return field

    def fake_read_bytes(_addr, size):
        # At a node address the first pointer is the next node; elsewhere
        # payload bytes.
        if _addr in node_addrs:
            nxt = (
                node_addrs[node_addrs.index(_addr) + 1]
                if _addr != node_addrs[-1]
                else 0
            )
            return nxt.to_bytes(size, "little")
        if _addr in free_addrs:
            nxt = (
                free_addrs[free_addrs.index(_addr) + 1]
                if _addr != free_addrs[-1]
                else 0
            )
            return nxt.to_bytes(size, "little")
        return b"\xaa\xbb\xcc\xdd"

    monkeypatch.setattr(detail, "read_field", fake_read_field)
    monkeypatch.setattr(detail, "read_int", fake_read_int)
    monkeypatch.setattr(detail, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.messagequeue_detail(msgqueue, object(), layout))

    assert pairs["Consistency"] == "ok (entry=2, free=6, max=8)"
    assert pairs["ActiveNodes"] == "2"
    assert pairs["FreeNodes"] == "6"
    assert "Msg[0]" in pairs
    assert "Msg[1]" in pairs
    assert pairs["Msg[0]"].startswith("@0x9000 payload=")


def test_messagequeue_detail_flags_entry_mismatch(monkeypatch):
    """A corrupted entry count is reported instead of silently trusted."""
    msgqueue = MessageQueue(
        name="work", entry=5, msg_size=16, max_msgs=8, address=0x2000
    )
    layout = KernelLayout(
        structs={"struct rt_messagequeue": StructLayout("struct rt_messagequeue")}
    )

    def fake_read_field(_value, _sl, field_name):
        return {"msg_queue_head": 0x9000, "msg_queue_free": 0x9100}.get(field_name)

    def fake_read_int(field):
        return field

    def fake_read_bytes(_addr, size):
        return (0).to_bytes(size, "little")

    monkeypatch.setattr(detail, "read_field", fake_read_field)
    monkeypatch.setattr(detail, "read_int", fake_read_int)
    monkeypatch.setattr(detail, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.messagequeue_detail(msgqueue, object(), layout))

    assert pairs["Consistency"].startswith("mismatch:")
    assert "entry 5 != active nodes 1" in pairs["Consistency"]


def test_messagequeue_detail_reports_unavailable_list_pointers(monkeypatch):
    """Unreadable active/free heads are not misreported as empty lists."""
    msgqueue = MessageQueue(name="work", entry=0, max_msgs=8, address=0x2000)
    layout = KernelLayout(
        structs={"struct rt_messagequeue": StructLayout("struct rt_messagequeue")}
    )
    monkeypatch.setattr(detail, "read_field", lambda _v, _sl, _f: None)
    monkeypatch.setattr(detail, "read_int", lambda field: field)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.messagequeue_detail(msgqueue, object(), layout))

    assert pairs["ActiveNodes"] == "N/A"
    assert pairs["FreeNodes"] == "N/A"
    assert pairs["Consistency"] == "N/A (message-list pointers unavailable)"


def test_messagequeue_consistency_verdict():
    """Consistency verdicts cover matching, mismatched, and empty cases."""
    assert detail._messagequeue_consistency(0, 0, 8, 8) == "ok (entry=0, free=8, max=8)"
    assert "entry 2 != active nodes 1" in detail._messagequeue_consistency(2, 1, 7, 8)
    assert "free 0 + active 2 != max_msgs 4" in detail._messagequeue_consistency(
        2, 2, 0, 4
    )


def test_memorypool_detail_validates_alignment_and_free_count(monkeypatch):
    """Pool detail checks block alignment and the free-list count."""
    pool = MemoryPool(
        name="blocks",
        block_size=32,
        block_total_count=10,
        block_free_count=3,
        address=0x3000,
    )
    layout = KernelLayout(
        structs={"struct rt_mempool": StructLayout("struct rt_mempool")}
    )
    free_addrs = [0xA000, 0xA020, 0xA040]

    def fake_read_field(_value, _sl, field_name):
        return {
            "start_address": 0x9F00,
            "size": 0x100,
            "block_list": free_addrs[0],
        }.get(field_name)

    def fake_read_int(field):
        return field

    def fake_read_bytes(_addr, size):
        if _addr in free_addrs:
            nxt = (
                free_addrs[free_addrs.index(_addr) + 1]
                if _addr != free_addrs[-1]
                else 0
            )
            return nxt.to_bytes(size, "little")
        return b"\x00" * size

    monkeypatch.setattr(detail, "read_field", fake_read_field)
    monkeypatch.setattr(detail, "read_int", fake_read_int)
    monkeypatch.setattr(detail, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.memorypool_detail(pool, object(), layout))

    assert pairs["StartAddress"] == "0x9f00"
    assert pairs["PoolSize"] == "256"
    assert pairs["BlockList"] == "0xa000"
    assert pairs["AlignmentCheck"] == "ok"
    assert pairs["FreeCountCheck"] == "ok (3)"


def test_memorypool_detail_flags_alignment_and_count(monkeypatch):
    """Misaligned block sizes and stale cached free counts are reported."""
    pool = MemoryPool(
        name="blocks",
        block_size=6,
        block_total_count=10,
        block_free_count=5,
        address=0x3000,
    )
    layout = KernelLayout(
        structs={"struct rt_mempool": StructLayout("struct rt_mempool")}
    )
    monkeypatch.setattr(detail, "read_field", lambda _v, _sl, _f: 0xA000)
    monkeypatch.setattr(detail, "read_int", lambda field: field)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: b"\x00" * 4)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    pairs = dict(detail.memorypool_detail(pool, object(), layout))

    assert "not 4-aligned" in pairs["AlignmentCheck"]
    assert pairs["FreeCountCheck"].startswith("listed 1 != cached 5")


def test_read_field_at_uses_dwarf_offsets(monkeypatch):
    """Block-header reads derive offsets from the DWARF type via member_offset."""
    layout = StructLayout(
        "struct heap_mem",
        fields={"magic": StructField("magic", ("magic",))},
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: 0)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: b"\xa0\x1e")

    assert (
        detail._read_field_at(0x1000, "struct heap_mem", layout, "magic", 2, "little")
        == 0x1EA0
    )


def test_read_field_at_degrades_when_offset_unavailable(monkeypatch):
    """A missing DWARF type makes Blocks/Holes unavailable instead of crashing."""
    layout = StructLayout(
        "struct heap_mem",
        fields={"magic": StructField("magic", ("magic",))},
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: None)

    assert (
        detail._read_field_at(0x1000, "struct heap_mem", layout, "magic", 2, "little")
        is None
    )


def test_small_mem_chain_walk_counts_blocks_and_holes(monkeypatch):
    """The 4.0 small_mem chain produces used/free counts and free-block holes."""
    item_layout = StructLayout("struct heap_mem")
    items = {
        0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
        0x1040: {"magic": 0x1EA0, "used": 0, "next": 0x90},
        0x1090: {"magic": 0x1EA0, "used": 1, "next": 0x100},
    }

    def fake_read_field_at(addr, _type_name, _layout, field, _width, _endian):
        return items.get(addr, {}).get(field)

    monkeypatch.setattr(detail, "_read_field_at", fake_read_field_at)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.used_blocks == 2
    assert walk.free_blocks == 1
    assert walk.hole_sizes == [64]
    assert walk.corrupt is False
    assert walk.truncated is False
    assert walk.used_bytes == 144
    assert walk.total_bytes == 256


def test_small_mem_chain_walk_uses_pool_ptr_lsb_for_41(monkeypatch):
    """4.1 ``rt_small_mem_item`` marks used via the pool_ptr LSB."""
    item_layout = StructLayout("struct rt_small_mem_item")
    items = {
        0x2000: {"pool_ptr": 0x1001, "next": 0x30},
        0x2030: {"pool_ptr": 0x1000, "next": 0x60},
        0x2060: {"pool_ptr": 0x1001, "next": 0x100},
    }

    def fake_read_field_at(addr, _type_name, _layout, field, _width, _endian):
        return items.get(addr, {}).get(field)

    monkeypatch.setattr(detail, "_read_field_at", fake_read_field_at)

    walk = detail._walk_small_mem_chain(
        0x2000,
        0x2100,
        "struct rt_small_mem_item",
        item_layout,
        16,
        used_from_pool_ptr=True,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.used_blocks == 2
    assert walk.free_blocks == 1


def test_small_mem_chain_walk_aggregates_memtrace_occupancy(monkeypatch):
    """MEMTRACE names produce per-thread block/byte occupancy."""
    item_layout = StructLayout(
        "struct heap_mem",
        fields={"thread": StructField("thread", ("thread",), kind="string")},
    )
    items = {
        0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
        0x1040: {"magic": 0x1EA0, "used": 1, "next": 0x80},
        0x1080: {"magic": 0x1EA0, "used": 1, "next": 0x100},
    }

    def fake_read_field_at(addr, _type_name, _layout, field, _width, _endian):
        return items.get(addr, {}).get(field)

    def fake_read_name_at(addr, _type_name, _layout, _field, _width, _endian):
        return {0x1000: "main", 0x1040: "main", 0x1080: "worker1"}.get(addr)

    monkeypatch.setattr(detail, "_read_field_at", fake_read_field_at)
    monkeypatch.setattr(detail, "_read_name_at", fake_read_name_at)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.occupancy == [
        ("main", 2, 96),
        ("worker1", 1, 112),
    ]


def test_small_mem_chain_walk_reads_memtrace_name_as_four_bytes(monkeypatch):
    """MEMTRACE ``thread[4]`` is not pointer-width; 64-bit must still read 4."""
    item_layout = StructLayout(
        "struct heap_mem",
        fields={"thread": StructField("thread", ("thread",), kind="string")},
    )
    widths: list[int] = []
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x100},
        }.get(addr, {}).get(field),
    )

    def fake_read_name_at(_addr, _type_name, _layout, _field, width, _endian):
        widths.append(width)
        return "main"

    monkeypatch.setattr(detail, "_read_name_at", fake_read_name_at)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=8,
        endian="little",
    )

    assert walk is not None
    assert widths == [detail._MEMTRACE_NAME_WIDTH]
    assert walk.occupancy == [("main", 1, 240)]


def test_small_mem_chain_walk_omits_occupancy_without_memtrace(monkeypatch):
    """Without a ``thread`` field the walk reports no per-thread occupancy."""
    item_layout = StructLayout("struct heap_mem")
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x100},
        }[addr][field],
    )

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.occupancy == []


def test_small_mem_chain_walk_reports_corrupt_magic(monkeypatch):
    """A bad magic value after valid blocks stops the walk and flags corruption."""
    item_layout = StructLayout("struct heap_mem")

    def fake_read_field_at(addr, _type_name, _layout, field, _width, _endian):
        return {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
            0x1040: {"magic": 0xDEAD, "used": 1, "next": 0x80},
        }.get(addr, {}).get(field)

    monkeypatch.setattr(detail, "_read_field_at", fake_read_field_at)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.corrupt is True
    assert walk.used_blocks == 1
    assert walk.used_bytes is None
    assert walk.total_bytes is None


def test_small_mem_chain_walk_reports_truncated_beyond_bound(monkeypatch):
    """Exceeding the traversal bound stops the walk and reports truncation."""
    item_layout = StructLayout("struct heap_mem")

    def fake_read_field_at(addr, _type_name, _layout, field, _width, _endian):
        return {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
            0x1040: {"magic": 0x1EA0, "used": 1, "next": 0x80},
            0x1080: {"magic": 0x1EA0, "used": 1, "next": 0x100},
        }.get(addr, {}).get(field)

    monkeypatch.setattr(detail, "GDR_MAX_TRAVERSAL_COUNT", 2)
    monkeypatch.setattr(detail, "_read_field_at", fake_read_field_at)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.truncated is True
    assert walk.used_blocks == 2
    assert walk.used_bytes is None
    assert walk.total_bytes is None


def test_memheap_walk_counts_circular_block_list(monkeypatch):
    """The memheap block_list is circular; free blocks form the holes."""
    heap = object()
    heap_layout = StructLayout(
        "struct rt_memheap",
        fields={"block_list": StructField("block_list", ("block_list",))},
    )
    layout = KernelLayout(
        structs={
            "struct rt_memheap": heap_layout,
            "struct rt_memheap_item": StructLayout("struct rt_memheap_item"),
        }
    )
    head_addr = 0x8000
    blocks = {
        0x8000: {"magic": 0x1EA01EA0, "next": 0x8028},
        0x8028: {"magic": 0x1EA01EA1, "next": 0x8050},
        0x8050: {"magic": 0x1EA01EA1, "next": 0x8078},
        0x8078: {"magic": 0x1EA01EA1, "next": 0x8000},  # tailer, not counted
    }
    monkeypatch.setattr(
        detail, "lookup_symbol", lambda name: heap if name == "_heap" else None
    )
    monkeypatch.setattr(
        detail,
        "read_field",
        lambda value, _layout, field: (
            object() if value is heap and field == "block_list" else None
        ),
    )
    monkeypatch.setattr(detail, "read_int", lambda _value: head_addr)
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: blocks.get(addr, {}).get(field),
    )
    monkeypatch.setattr(detail, "_header_size", lambda _type_name: 24)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    walk = detail._walk_memheap(layout)

    assert walk is not None
    assert walk.used_blocks == 2
    assert walk.free_blocks == 1
    assert walk.hole_sizes == [16]
    assert walk.corrupt is False
    assert walk.used_bytes == 128
    assert walk.total_bytes == 144


def test_memheap_walk_aggregates_owner_thread_names(monkeypatch):
    """MEMTRACE owner names on ``rt_memheap_item`` feed per-thread occupancy."""
    heap = object()
    heap_layout = StructLayout(
        "struct rt_memheap",
        fields={"block_list": StructField("block_list", ("block_list",))},
    )
    item_layout = StructLayout(
        "struct rt_memheap_item",
        fields={
            "owner_thread_name": StructField(
                "owner_thread_name", ("owner_thread_name",)
            )
        },
    )
    layout = KernelLayout(
        structs={
            "struct rt_memheap": heap_layout,
            "struct rt_memheap_item": item_layout,
        }
    )
    head_addr = 0x8000
    blocks = {
        0x8000: {"magic": 0x1EA01EA1, "next": 0x8028},
        0x8028: {"magic": 0x1EA01EA1, "next": 0x8050},
        0x8050: {"magic": 0x1EA01EA1, "next": 0x8078},
        0x8078: {"magic": 0x1EA01EA1, "next": 0x8000},  # tailer, not counted
    }
    owners = {0x8000: "main", 0x8028: "worker1", 0x8050: "main", 0x8078: "idle"}
    monkeypatch.setattr(
        detail, "lookup_symbol", lambda name: heap if name == "_heap" else None
    )
    monkeypatch.setattr(
        detail,
        "read_field",
        lambda value, _layout, field: (
            object() if value is heap and field == "block_list" else None
        ),
    )
    monkeypatch.setattr(detail, "read_int", lambda _value: head_addr)
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: blocks.get(addr, {}).get(field),
    )
    monkeypatch.setattr(
        detail,
        "_read_name_at",
        lambda addr, _t, _l, _f, _w, _e: owners.get(addr),
    )
    monkeypatch.setattr(detail, "_header_size", lambda _type_name: 24)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    walk = detail._walk_memheap(layout)

    assert walk is not None
    assert walk.occupancy == [("main", 2, 32), ("worker1", 1, 16)]


def test_memheap_walk_skips_the_circular_tailer(monkeypatch):
    """The tailer whose next points at ``block_list`` is not a used block."""
    heap = object()
    layout = KernelLayout(
        structs={
            "struct rt_memheap": StructLayout(
                "struct rt_memheap",
                fields={"block_list": StructField("block_list", ("block_list",))},
            ),
            "struct rt_memheap_item": StructLayout("struct rt_memheap_item"),
        }
    )
    monkeypatch.setattr(
        detail, "lookup_symbol", lambda name: heap if name == "_heap" else None
    )
    monkeypatch.setattr(
        detail,
        "read_field",
        lambda value, _layout, field: (
            object() if value is heap and field == "block_list" else None
        ),
    )
    monkeypatch.setattr(detail, "read_int", lambda _value: 0x8000)
    monkeypatch.setattr(detail, "_header_size", lambda _type_name: 24)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x8000: {"magic": 0x1EA01EA1, "next": 0x8000},
        }.get(addr, {}).get(field),
    )

    walk = detail._walk_memheap(layout)

    assert walk is not None
    assert walk.used_blocks == 0
    assert walk.free_blocks == 0
    assert walk.hole_sizes == []
    assert walk.used_bytes == 24
    assert walk.total_bytes == 24


def test_slab_walk_counts_free_and_used_pages(monkeypatch):
    """Slab free-page runs become hole sizes; pages lack a chunk owner ABI."""
    layout = KernelLayout()
    pages = [
        b"\x00\x00\x00\x00",  # FREE
        b"\x01\x00\x00\x00",  # SMALL
        b"\x00\x00\x00\x00",  # FREE
        b"\x02\x00\x00\x00",  # LARGE
    ]
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: {
            "heap_start": 0x1000,
            "heap_end": 0x5000,
            "memusage": 0x6000,
        }.get(name),
    )
    monkeypatch.setattr(detail, "read_int", lambda value: value)
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: 4096)
    monkeypatch.setattr(
        detail,
        "read_bytes",
        lambda addr, _size: pages[(addr - 0x6000) // 4],
    )
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    walk = detail._walk_slab_pages(layout)

    assert walk is not None
    assert walk.used_blocks == 2
    assert walk.free_blocks == 2
    assert walk.hole_sizes == [4096, 4096]
    assert walk.occupancy == []


def test_walk_system_heap_dispatches_by_algorithm(monkeypatch):
    """The public walk route covers every heap algorithm and ``none``."""
    layout = KernelLayout()
    monkeypatch.setattr(detail, "_walk_small_mem", lambda _kl: "small")
    monkeypatch.setattr(detail, "_walk_memheap", lambda _kl: "mem")
    monkeypatch.setattr(detail, "_walk_slab_pages", lambda _kl: "slab")

    assert detail.walk_system_heap("small_mem", layout) == "small"
    assert detail.walk_system_heap("memheap", layout) == "mem"
    assert detail.walk_system_heap("slab", layout) == "slab"
    assert detail.walk_system_heap("none", layout) is None


def test_read_field_at_returns_none_for_missing_field_or_memory(monkeypatch):
    """Field absence, missing DWARF offset, and unreadable memory degrade."""
    layout = StructLayout(
        "struct heap_mem",
        fields={"magic": StructField("magic", ("magic",))},
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: 4)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: None)

    assert (
        detail._read_field_at(0x1000, "struct heap_mem", layout, "absent", 2, "little")
        is None
    )
    assert (
        detail._read_field_at(0x1000, "struct heap_mem", layout, "magic", 2, "little")
        is None
    )


def test_read_name_at_decodes_strips_and_degrades(monkeypatch):
    """MEMTRACE names decode, strip padding, and degrade on blank/unreadable."""
    layout = StructLayout(
        "struct heap_mem",
        fields={"thread": StructField("thread", ("thread",))},
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: 12)
    monkeypatch.setattr(
        detail,
        "read_bytes",
        lambda _addr, _size: b"main\x00xx",
    )
    assert (
        detail._read_name_at(0x1000, "struct heap_mem", layout, "thread", 4, "little")
        == "main"
    )
    assert (
        detail._read_name_at(0x1000, "struct heap_mem", layout, "absent", 4, "little")
        is None
    )
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: b"    ")
    assert (
        detail._read_name_at(0x1000, "struct heap_mem", layout, "thread", 4, "little")
        is None
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: None)
    assert (
        detail._read_name_at(0x1000, "struct heap_mem", layout, "thread", 4, "little")
        is None
    )
    monkeypatch.setattr(detail, "member_offset", lambda _t, _p: 12)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: None)
    assert (
        detail._read_name_at(0x1000, "struct heap_mem", layout, "thread", 4, "little")
        is None
    )


def test_header_size_computes_aligned_size(monkeypatch):
    """Header size mirrors RT_ALIGN(sizeof, RT_ALIGN_SIZE) with fallbacks."""
    monkeypatch.setattr(
        detail, "lookup_type", lambda _name: type("_T", (), {"sizeof": 12})()
    )
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: 8)
    assert detail._header_size("struct heap_mem") == 16
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    assert detail._header_size("struct heap_mem") == 12
    monkeypatch.setattr(detail, "_arch", lambda: (8, "little"))
    assert detail._header_size("struct heap_mem") == 16
    monkeypatch.setattr(
        detail, "lookup_type", lambda _name: type("_T", (), {"sizeof": "bad"})()
    )
    assert detail._header_size("struct heap_mem") is None
    monkeypatch.setattr(detail, "lookup_type", lambda _name: None)
    assert detail._header_size("struct heap_mem") is None


def test_small_mem_chain_walk_reports_range_corruption(monkeypatch):
    """A backward next offset is a range error reported as corruption."""
    item_layout = StructLayout("struct heap_mem")
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
            0x1040: {"magic": 0x1EA0, "used": 1, "next": 0x20},
        }.get(addr, {}).get(field),
    )

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.corrupt is True
    assert walk.used_blocks == 1


def test_small_mem_chain_walk_reports_next_past_heap_end(monkeypatch):
    """A next offset that lands past ``heap_end`` is corruption, not a walk."""
    item_layout = StructLayout("struct heap_mem")
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x40},
            0x1040: {"magic": 0x1EA0, "used": 1, "next": 0x200},
        }.get(addr, {}).get(field),
    )

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.corrupt is True
    assert walk.used_blocks == 1


def test_small_mem_chain_walk_returns_none_when_empty():
    """A zero-length chain (heap_ptr == heap_end) yields no walk."""
    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1000,
        "struct heap_mem",
        StructLayout("struct heap_mem"),
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is None


def test_small_mem_chain_walk_skips_blank_owners(monkeypatch):
    """Blank MEMTRACE names never enter the occupancy table."""
    item_layout = StructLayout(
        "struct heap_mem",
        fields={"thread": StructField("thread", ("thread",), kind="string")},
    )
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x1000: {"magic": 0x1EA0, "used": 1, "next": 0x100},
        }.get(addr, {}).get(field),
    )
    monkeypatch.setattr(detail, "_read_name_at", lambda *_args: None)

    walk = detail._walk_small_mem_chain(
        0x1000,
        0x1100,
        "struct heap_mem",
        item_layout,
        16,
        used_from_pool_ptr=False,
        ptrsize=4,
        endian="little",
    )

    assert walk is not None
    assert walk.used_blocks == 1
    assert walk.occupancy == []


class _AddrHandle:
    def __init__(self, address: int):
        self._address = address

    def __int__(self) -> int:
        return self._address


def test_walk_small_mem_41_reads_system_heap_bounds(monkeypatch):
    """4.1 small_mem resolves bounds from the ``system_heap`` object."""
    layout = KernelLayout(
        structs={
            "struct rt_small_mem": StructLayout("struct rt_small_mem"),
            "struct rt_small_mem_item": StructLayout("struct rt_small_mem_item"),
        }
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: _AddrHandle(0x2000) if name == "system_heap" else None,
    )
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda _addr, _t, _l, field, _w, _e: {
            "heap_ptr": 0x1000,
            "heap_end": 0x2000,
        }.get(field),
    )
    monkeypatch.setattr(detail, "_header_size", lambda _t: 16)
    monkeypatch.setattr(
        detail,
        "_walk_small_mem_chain",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "chain-result",
    )

    assert detail._walk_small_mem(layout) == "chain-result"
    args, kwargs = calls[0]
    assert args[0] == 0x1000
    assert args[1] == 0x2000
    assert args[2] == "struct rt_small_mem_item"
    assert args[4] == 16
    assert kwargs["used_from_pool_ptr"] is True


def test_walk_small_mem_40_reads_globals(monkeypatch):
    """4.0 small_mem resolves bounds from the ``heap_ptr``/``heap_end`` globals."""
    layout = KernelLayout(structs={"struct heap_mem": StructLayout("struct heap_mem")})
    calls: list[tuple] = []
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: (
            0x1000 if name == "heap_ptr" else 0x2000 if name == "heap_end" else None
        ),
    )
    monkeypatch.setattr(detail, "read_int", lambda value: value)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    monkeypatch.setattr(detail, "_header_size", lambda _t: 16)
    monkeypatch.setattr(
        detail,
        "_walk_small_mem_chain",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "chain-result",
    )

    assert detail._walk_small_mem(layout) == "chain-result"
    args, kwargs = calls[0]
    assert args[0] == 0x1000
    assert args[1] == 0x2000
    assert args[2] == "struct heap_mem"
    assert kwargs["used_from_pool_ptr"] is False


def test_walk_small_mem_degrades_when_unresolvable(monkeypatch):
    """Missing layouts or bounds make the small_mem walk unavailable."""
    monkeypatch.setattr(detail, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    assert detail._walk_small_mem(KernelLayout()) is None
    layout = KernelLayout(structs={"struct heap_mem": StructLayout("struct heap_mem")})
    monkeypatch.setattr(detail, "read_int", lambda _value: None)
    monkeypatch.setattr(detail, "_header_size", lambda _t: 16)
    assert detail._walk_small_mem(layout) is None


def test_walk_memheap_degrades_when_unresolvable(monkeypatch):
    """Missing heap objects, layouts, or a null block list yield no walk."""
    layout = KernelLayout(
        structs={"struct rt_memheap": StructLayout("struct rt_memheap")}
    )
    monkeypatch.setattr(detail, "lookup_symbol", lambda _name: None)
    assert detail._walk_memheap(layout) is None

    heap = object()
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: heap if name == "_heap" else None,
    )
    monkeypatch.setattr(
        detail,
        "read_field",
        lambda _value, _layout, _field: object(),
    )
    monkeypatch.setattr(detail, "read_int", lambda _value: None)
    assert detail._walk_memheap(layout) is None


def test_walk_memheap_reports_truncated_and_corrupt(monkeypatch):
    """Memheap walks stop and report truncation/corruption at the chain edge."""
    heap = object()
    layout = KernelLayout(
        structs={
            "struct rt_memheap": StructLayout(
                "struct rt_memheap",
                fields={"block_list": StructField("block_list", ("block_list",))},
            ),
            "struct rt_memheap_item": StructLayout("struct rt_memheap_item"),
        }
    )
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: heap if name == "_heap" else None,
    )
    monkeypatch.setattr(
        detail,
        "read_field",
        lambda _value, _layout, _field: object(),
    )
    monkeypatch.setattr(detail, "read_int", lambda _value: 0x8000)
    monkeypatch.setattr(detail, "_header_size", lambda _t: 24)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    # A self-looping next pointer reports truncation via the seen-set.
    monkeypatch.setattr(
        detail,
        "GDR_MAX_TRAVERSAL_COUNT",
        4096,
    )
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x8000: {"magic": 0x1EA01EA0, "next": 0x8040},
            0x8040: {"magic": 0x1EA01EA0, "next": 0x8040},
        }.get(addr, {}).get(field),
    )
    walk = detail._walk_memheap(layout)
    assert walk is not None
    assert walk.truncated is True

    # A bad magic value on a later block reports corruption with partial data.
    monkeypatch.setattr(detail, "GDR_MAX_TRAVERSAL_COUNT", 4096)
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda addr, _t, _l, field, _w, _e: {
            0x8000: {"magic": 0x1EA01EA0, "next": 0x8028},
            0x8028: {"magic": 0xBAD, "next": 0x8050},
        }.get(addr, {}).get(field),
    )
    walk = detail._walk_memheap(layout)
    assert walk is not None
    assert walk.corrupt is True
    assert walk.free_blocks == 1

    # A chain with no readable items returns no walk at all.
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda _addr, _t, _l, _field, _w, _e: None,
    )
    assert detail._walk_memheap(layout) is None


def test_walk_slab_pages_41_reads_system_heap(monkeypatch):
    """4.1 slab reads bounds from the ``system_heap`` object fields."""
    layout = KernelLayout(structs={"struct rt_slab": StructLayout("struct rt_slab")})
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: _AddrHandle(0x5000) if name == "system_heap" else None,
    )
    monkeypatch.setattr(
        detail,
        "_read_field_at",
        lambda _addr, _t, _l, field, _w, _e: {
            "heap_start": 0x1000,
            "heap_end": 0x3000,
            "memusage": 0x7000,
        }.get(field),
    )
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: 4096)
    monkeypatch.setattr(
        detail,
        "read_bytes",
        lambda addr, _size: (
            b"\x00\x00\x00\x00" if addr < 0x7004 else b"\x01\x00\x00\x00"
        ),
    )
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    walk = detail._walk_slab_pages(layout)

    assert walk is not None
    assert walk.free_blocks == 1
    assert walk.used_blocks == 1
    assert walk.hole_sizes == [4096]


def test_walk_slab_pages_degrades_when_unresolvable(monkeypatch):
    """Missing slab bounds, page size fallback, or no pages yield no walk."""
    layout = KernelLayout()
    monkeypatch.setattr(detail, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(detail, "read_int", lambda _value: None)
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: None)
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    assert detail._walk_slab_pages(layout) is None

    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: {  # 4.0 globals with a zero-length heap
            "heap_start": 0x1000,
            "heap_end": 0x1000,
            "memusage": 0x6000,
        }.get(name),
    )
    monkeypatch.setattr(detail, "read_int", lambda value: value)
    assert detail._walk_slab_pages(layout) is None


def test_walk_slab_pages_reports_corrupt_on_unreadable_pages(monkeypatch):
    """An unreadable memusage descriptor flags corruption on the walk."""
    layout = KernelLayout()
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: {
            "heap_start": 0x1000,
            "heap_end": 0x3000,
            "memusage": 0x6000,
        }.get(name),
    )
    monkeypatch.setattr(detail, "read_int", lambda value: value)
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: 4096)
    monkeypatch.setattr(
        detail,
        "read_bytes",
        lambda addr, _size: b"\x00\x00\x00\x00" if addr < 0x6004 else None,
    )
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))

    walk = detail._walk_slab_pages(layout)

    assert walk is not None
    assert walk.corrupt is True
    assert walk.free_blocks == 1
    assert walk.used_blocks == 0


def test_walk_slab_pages_truncates_beyond_bound(monkeypatch):
    """A huge or corrupt page table stops at the shared traversal bound."""
    layout = KernelLayout()
    monkeypatch.setattr(
        detail,
        "lookup_symbol",
        lambda name: {
            "heap_start": 0x1000,
            "heap_end": 0x5000,
            "memusage": 0x6000,
        }.get(name),
    )
    monkeypatch.setattr(detail, "read_int", lambda value: value)
    monkeypatch.setattr(detail, "read_macro_int", lambda _name: 4096)
    monkeypatch.setattr(detail, "read_bytes", lambda _addr, _size: b"\x01\x00\x00\x00")
    monkeypatch.setattr(detail, "_arch", lambda: (4, "little"))
    monkeypatch.setattr(detail, "GDR_MAX_TRAVERSAL_COUNT", 2)

    walk = detail._walk_slab_pages(layout)

    assert walk is not None
    assert walk.truncated is True
    assert walk.used_blocks == 2
    assert walk.free_blocks == 0
