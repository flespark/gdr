"""Unit tests for RT-Thread single-object diagnostics."""

from __future__ import annotations

import rtthread.diagnostics as detail
from gdr.layout import KernelLayout, StructLayout
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
