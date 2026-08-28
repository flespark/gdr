"""Unit tests for the FreeRTOS stream/message/batching buffer geometry.

Covers the wrap-safe bytes/space arithmetic, the asymmetric batching
trigger comparison, the six ucFlags classifications, the deleted-buffer
short-circuit (no division on a zero length), the NextMsg precondition and
the message-length ``size_t`` assumption fallback.  All GDB entry points
stay monkeypatchable through module-level helpers, following
``test_timers.py``.
"""

from __future__ import annotations

import pytest

import freertos.streams as streams
from freertos.details import stream_buffer_detail
from freertos.layout import FreeRtosConfig, build_layout
from freertos.navigation import DiscoveredObject
from freertos.streams import FreeRtosStreamBufferObject


def test_bytes_in_buffer_handles_wrap():
    """xHead < xTail wraps without modulo pitfalls (stream_buffer.c)."""
    assert streams.bytes_in_buffer(2, 30, 33) == 5
    assert streams.spaces_available(2, 30, 33) == 27
    # Empty ring: head == tail.
    assert streams.bytes_in_buffer(0, 0, 33) == 0
    assert streams.spaces_available(0, 0, 33) == 32
    # Full ring: head one slot past tail; one byte always stays reserved.
    assert streams.bytes_in_buffer(32, 0, 33) == 32
    assert streams.spaces_available(32, 0, 33) == 0
    # Wrap position: head == tail at a non-zero offset is still empty.
    assert streams.bytes_in_buffer(32, 32, 33) == 0
    assert streams.spaces_available(32, 32, 33) == 32


def test_trigger_met_is_asymmetric_for_batching():
    """Batching notifies on `>` only; every other buffer on `>=`."""
    assert streams.trigger_met(5, 5, batching=False) is True
    assert streams.trigger_met(5, 5, batching=True) is False
    assert streams.trigger_met(6, 5, batching=True) is True
    assert streams.trigger_met(4, 5, batching=False) is False
    assert streams.trigger_met(5, None, batching=False) is False


def test_ucflags_render_six_values():
    """ucFlags {0,1,2,3,4,6} map to the six documented labels."""
    labels = [streams.stream_label(flags) for flags in (0, 1, 2, 3, 4, 6)]
    assert labels == [
        "stream",
        "message",
        "stream+static",
        "message+static",
        "batching",
        "batching+static",
    ]


def test_stream_kind_leaves_static_out():
    """The table Type keeps the base kind; static lives in Src/detail."""
    assert streams.stream_kind(0) == "stream"
    assert streams.stream_kind(2) == "stream"
    assert streams.stream_kind(3) == "message"
    assert streams.stream_kind(4) == "batching"
    assert streams.stream_kind(None) == "unknown"


def test_deleted_buffer_short_circuits(monkeypatch):
    """xLength==0 && pucBuffer==NULL stops geometry before any division."""
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_sb", source="symbol"
    )
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    monkeypatch.setattr(
        streams,
        "read_path",
        lambda _value, path: {("xLength",): 0, ("pucBuffer",): 0}.get(path),
    )
    obj = streams.value_to_stream_buffer_object(object(), found, layout)

    assert obj.deleted is True
    assert obj.capacity is None
    assert obj.bytes_used is None
    assert obj.space is None
    assert obj.trigger is None


def test_deleted_buffer_detail_short_circuits(monkeypatch):
    """The detail explains the deletion; no ZeroDivisionError is ever raised."""
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_sb", source="symbol"
    )
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    monkeypatch.setattr(
        streams,
        "read_path",
        lambda _value, path: {("xLength",): 0, ("pucBuffer",): 0}.get(path),
    )
    obj = streams.value_to_stream_buffer_object(object(), found, layout)
    pairs = stream_buffer_detail(obj, object(), layout)

    pairs_by_key = {key: value for key, value in pairs}
    assert pairs_by_key["Type"] == "deleted"
    assert "Bytes" not in pairs_by_key
    assert "Space" not in pairs_by_key


def _message_buffer_object():
    """A live message buffer model whose reads come from a path table."""
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_mb", source="symbol"
    )
    paths = {
        ("xLength",): 33,
        ("pucBuffer",): 0x1000,
        ("xHead",): 0,
        ("xTail",): 0,
        ("xTriggerLevelBytes",): 1,
        ("ucFlags",): 1,
        ("xTaskWaitingToReceive",): 0,
        ("xTaskWaitingToSend",): 0,
    }
    return found, paths


def test_next_message_only_for_message_buffer(monkeypatch):
    """A stream buffer renders NextMsg N/A; a message buffer only when a
    length prefix is stored (bytes > sbBYTES_TO_STORE_MESSAGE_LENGTH)."""
    monkeypatch.setattr(streams, "lookup_type", lambda _name: None)

    # Message buffer with an empty ring: no length prefix stored -> None.
    found, paths = _message_buffer_object()
    monkeypatch.setattr(streams, "read_path", lambda _v, path: paths.get(path))
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    assert obj.kind == "message"
    assert obj.next_message is None

    # A plain stream buffer never computes NextMsg (N/A, not 0).
    stream_paths = dict(paths)
    stream_paths[("ucFlags",)] = 0
    monkeypatch.setattr(streams, "read_path", lambda _v, path: stream_paths.get(path))
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    assert obj.kind == "stream"
    assert obj.next_message is None


def test_message_length_size_falls_back_to_size_t(monkeypatch):
    """Without the macro typedef, sizeof(size_t) is used and marked assumed."""
    size_t = type("T", (), {"sizeof": 8})()

    def lookup(name):
        if name == "configMESSAGE_BUFFER_LENGTH_TYPE":
            return None
        if name == "size_t":
            return size_t
        return None

    monkeypatch.setattr(streams, "lookup_type", lookup)
    size, assumed = streams.message_length_size()
    assert (size, assumed) == (8, True)

    def lookup_precise(name):
        if name == "configMESSAGE_BUFFER_LENGTH_TYPE":
            return type("T", (), {"sizeof": 2})()
        return None

    monkeypatch.setattr(streams, "lookup_type", lookup_precise)
    size, assumed = streams.message_length_size()
    assert (size, assumed) == (2, False)


def test_notification_index_absent_before_11_1():
    """A kernel without uxNotificationIndex renders the gate, not a fade."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))  # gate off
    obj = FreeRtosStreamBufferObject(
        name="g", address=0x10, kind="message", flags=1, source="symbol"
    )
    pairs = stream_buffer_detail(obj, object(), layout)
    pairs_by_key = {key: value for key, value in pairs}
    assert pairs_by_key["NotificationIndex"] == "N/A (kernel < 11.1.0)"


def test_notification_index_renders_when_gated(monkeypatch):
    """With the V11.1+ member gated on, the index renders instead."""
    layout = build_layout(
        FreeRtosConfig(stream_buffer_notification_index=True), (11, 1, 0)
    )
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_sb", source="symbol"
    )
    paths = {
        ("xLength",): 33,
        ("pucBuffer",): 0x1000,
        ("xHead",): 0,
        ("xTail",): 0,
        ("xTriggerLevelBytes",): 1,
        ("ucFlags",): 0,
        ("xTaskWaitingToReceive",): 0,
        ("xTaskWaitingToSend",): 0,
        ("uxNotificationIndex",): 7,
    }
    monkeypatch.setattr(streams, "lookup_type", lambda _name: None)
    monkeypatch.setattr(streams, "read_path", lambda _v, path: paths.get(path))
    obj = streams.value_to_stream_buffer_object(object(), found, layout)

    assert obj.notification_index == 7
    pairs = stream_buffer_detail(obj, object(), layout)
    pairs_by_key = {key: value for key, value in pairs}
    assert pairs_by_key["NotificationIndex"] == "7"


@pytest.mark.parametrize(
    ("flags", "kind", "capacity", "bytes", "space"),
    [
        (0, "stream", 32, 0, 32),
        (1, "message", 32, 0, 32),
        (4, "batching", 32, 0, 32),
    ],
)
def test_stream_buffer_table_contract(monkeypatch, flags, kind, capacity, bytes, space):
    """frt streambuffers owns the verbatim 11-column contract."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_sb", source="symbol"
    )
    paths = {
        ("xLength",): 33,
        ("pucBuffer",): 0x1000,
        ("xHead",): 0,
        ("xTail",): 0,
        ("xTriggerLevelBytes",): 1,
        ("ucFlags",): flags,
        ("xTaskWaitingToReceive",): 0,
        ("xTaskWaitingToSend",): 0,
    }
    monkeypatch.setattr(streams, "lookup_type", lambda _name: None)
    monkeypatch.setattr(streams, "read_path", lambda _v, path: paths.get(path))
    obj = streams.value_to_stream_buffer_object(object(), found, layout)
    table = streams.stream_buffer_table([obj], layout)

    assert table.headers == [
        "Name",
        "Type",
        "Bytes",
        "Space",
        "Capacity",
        "Trigger",
        "NextMsg",
        "RecvWait",
        "SendWait",
        "Src",
        "Addr",
    ]
    row = table.rows[0]
    assert row[:5] == ["gdr_sb", kind, str(bytes), str(space), str(capacity)]
    assert row[5] == "1"  # Trigger
    assert row[6] == ("-" if kind == "message" else "N/A")  # NextMsg
    assert row[7] == "-" and row[8] == "-"  # single-handle waiters empty
    assert any("no waiter discovery channel" in m for m in table.messages)


def test_next_message_reads_prefix_at_buffer_plus_tail(monkeypatch):
    """NextMsg reads the length prefix at pucBuffer[xTail], not at the raw
    offset (stream_buffer.c prvReadBytesFromBuffer reads pucBuffer[xTail]).
    """
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_mb", source="symbol"
    )
    # 8 bytes stored, tail at offset 4 -> the 4-byte prefix lives at 0x1004.
    paths = {
        ("xLength",): 33,
        ("pucBuffer",): 0x1000,
        ("xHead",): 12,
        ("xTail",): 4,
        ("xTriggerLevelBytes",): 1,
        ("ucFlags",): 1,
        ("xTaskWaitingToReceive",): 0,
        ("xTaskWaitingToSend",): 0,
    }
    reads: list[tuple[int, int]] = []

    def fake_read_bytes(addr, size):
        reads.append((addr, size))
        return bytes([0x21, 0, 0, 0])

    monkeypatch.setattr(streams, "lookup_type", lambda _name: None)
    monkeypatch.setattr(streams, "read_path", lambda _v, path: paths.get(path))
    monkeypatch.setattr(streams, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(streams, "get_arch_info", lambda: None)
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )

    assert obj.next_message == 0x21
    assert reads == [(0x1004, 4)], reads


def test_next_message_prefix_wraps_ring_end(monkeypatch):
    """A length prefix straddling the ring end reads both segments, matching
    prvReadBytesFromBuffer's two-memcpy wrap (stream_buffer.c)."""
    found = DiscoveredObject(
        kind="streambuffer", address=0x2000, name="gdr_mb", source="symbol"
    )
    # Prefix width 4 (size_t fallback), tail at offset 31 of a 33-byte ring:
    # 2 bytes at pucBuffer+31, then 2 bytes wrapped to pucBuffer+0.
    paths = {
        ("xLength",): 33,
        ("pucBuffer",): 0x1000,
        ("xHead",): 39 - 33,
        ("xTail",): 31,
        ("xTriggerLevelBytes",): 1,
        ("ucFlags",): 1,
        ("xTaskWaitingToReceive",): 0,
        ("xTaskWaitingToSend",): 0,
    }
    reads: list[tuple[int, int]] = []

    def fake_read_bytes(addr, size):
        reads.append((addr, size))
        return bytes([addr & 0xFF]) * size

    monkeypatch.setattr(streams, "lookup_type", lambda _name: None)
    monkeypatch.setattr(streams, "read_path", lambda _v, path: paths.get(path))
    monkeypatch.setattr(streams, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(streams, "get_arch_info", lambda: None)
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )

    # 8 bytes stored (head wrapped to 2): bytes > 4 so the prefix is read.
    assert obj.bytes_used == 8
    assert reads == [(0x101F, 2), (0x1000, 2)], reads
