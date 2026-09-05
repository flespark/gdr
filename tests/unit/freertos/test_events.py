"""Unit tests for the FreeRTOS event-group model and control-bit decoding.

Covers the width-derived control masks, the ``raw xItemValue`` decode
(stripping ``IN_USE``/control bits), the ALL vs ANY missing computation,
the ``(satisfied — mid-unblock)`` transient marker, and the
``ucStaticallyAllocated`` config gate.  All GDB entry points stay
monkeypatchable through module-level helpers, following ``test_timers.py``.
"""

from __future__ import annotations

import pytest

import freertos.details as details_module
import freertos.events as events
from freertos.details import event_group_detail
from freertos.events import EventWaiter, FreeRtosEventGroupObject
from freertos.layout import FreeRtosConfig, build_layout
from freertos.navigation import DiscoveredObject

# EventBits_t is TickType_t: the control bits sit in the top byte, so every
# mask follows the tick width (event_groups.h), never a 32-bit literal.
_EXPECTED_MASKS = {
    16: (0x0100, 0x0200, 0x0400, 0xFF00),
    32: (0x01000000, 0x02000000, 0x04000000, 0xFF000000),
    64: (
        0x0100000000000000,
        0x0200000000000000,
        0x0400000000000000,
        0xFF00000000000000,
    ),
}


def test_control_bit_masks_follow_tick_width():
    """CLEAR_ON_EXIT/UNBLOCKED/WAIT_FOR_ALL/CONTROL_BYTES derive from W."""
    for tick_bits, expected in _EXPECTED_MASKS.items():
        assert events.event_bit_masks(tick_bits) == expected


def test_waiter_decode_strips_in_use_bit():
    """xItemValue keeps wants|WAIT_FOR_ALL|IN_USE; decode strips them."""
    masks = events.event_bit_masks(32)
    raw = 0x3 | masks[2] | (1 << 31)  # wants=0x3, WAIT_FOR_ALL, eventIN_USE
    waiter = events.decode_waiter(raw, 0, masks)

    assert waiter is not None
    assert waiter.wants == 0x3
    assert waiter.wait_all is True
    assert waiter.in_use is True
    assert waiter.raw == raw


def test_waiter_decode_suspicious_in_use_clear():
    """A linked item without eventIN_USE is suspicious, not silently valid."""
    masks = events.event_bit_masks(32)
    waiter = events.decode_waiter(0x3, 0, masks)

    assert waiter is not None
    assert waiter.in_use is False
    assert "(IN_USE clear" in events.format_waiter_line(waiter)


def test_missing_bits_all_vs_any():
    """ALL keeps wants&~cur as missing; ANY stays unsatisfied per missing set."""
    masks = events.event_bit_masks(32)
    all_waiter = events.decode_waiter(0x3 | masks[2] | (1 << 31), 0x1, masks)
    assert all_waiter is not None
    assert all_waiter.satisfied is False
    assert all_waiter.missing == 0x2

    any_waiter = events.decode_waiter(0x3 | (1 << 31), 0x1, masks)
    assert any_waiter is not None
    assert any_waiter.wait_all is False
    assert any_waiter.satisfied is True
    assert any_waiter.missing == 0x3

    # ALL satisfied only when every wanted bit is set.
    done = events.decode_waiter(0x3 | masks[2] | (1 << 31), 0x3, masks)
    assert done is not None
    assert done.satisfied is True
    assert done.missing == 0x0


def test_satisfied_waiter_reports_mid_unblock():
    """A satisfied waiter still on the list renders the mid-unblock marker."""
    waiter = EventWaiter(
        task="gdr_evw",
        raw=0x80000003,
        wants=0x3,
        wait_all=True,
        clear_on_exit=True,
        satisfied=True,
        missing=0x0,
        in_use=True,
    )
    line = events.format_waiter_line(waiter)
    assert "wants=0x3 mode=ALL clearOnExit=yes missing=0x0" in line
    assert "(satisfied — mid-unblock)" in line

    obj = FreeRtosEventGroupObject(
        name="gdr_evg", address=0x10, bits=0x3, waiters=[waiter], source="symbol"
    )
    pairs = event_group_detail(
        obj, object(), build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    waiter_rows = [value for key, value in pairs if key.startswith("Waiter[")]
    # The detail prefixes each line with the waiter's task name.
    assert waiter_rows == [f"gdr_evw {line}"]


def test_event_group_static_flag_only_when_gated(monkeypatch):
    """ucStaticallyAllocated only exists when static AND dynamic are possible."""
    found = DiscoveredObject(
        kind="eventgroup", address=0x2000, name="gdr_eg", source="symbol"
    )
    off = build_layout(FreeRtosConfig(), (10, 3, 1))
    monkeypatch.setattr(events, "read_field", lambda _value, _sl, _field: None)
    assert (
        events.value_to_event_group_object(object(), found, off).statically_allocated
        is None
    )

    on = build_layout(FreeRtosConfig(static_and_dynamic=True), (10, 3, 1))

    def gated_read(_value, _sl, field):
        return 1 if field == "static_alloc" else None

    monkeypatch.setattr(events, "read_field", gated_read)
    gated = events.value_to_event_group_object(object(), found, on)
    assert gated.statically_allocated is True


def test_event_group_table_contract():
    """frt eventgroups owns the verbatim header contract."""
    obj = FreeRtosEventGroupObject(
        name="gdr_evg", address=0x2000, bits=0x3, waiters=[], source="symbol"
    )
    table = events.event_group_table([obj], build_layout(FreeRtosConfig(), (10, 3, 1)))
    assert table.headers == ["Name", "Bits", "Waiters", "Src", "Addr"]
    assert table.rows == [["gdr_evg", "0x3", "0", "symbol", "0x2000"]]


def test_event_group_table_absence_is_a_note_not_empty_row(monkeypatch):
    """A build without event groups says so instead of printing an empty table."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))
    monkeypatch.setattr(events, "read_field", lambda _value, _sl, _field: None)
    table = events.event_group_table([], layout)
    assert table.rows == []
    assert any("no event groups" in message for message in table.messages)


@pytest.mark.parametrize("tick_bits", [16, 32, 64])
def test_decoder_is_width_agnostic(tick_bits):
    """The same wants decode works on 16/32/64-bit EventBits_t."""
    masks = events.event_bit_masks(tick_bits)
    in_use = 1 << (tick_bits - 1)
    # WAIT_FOR_ALL mode, no wanted bit set: unsatisfied, missing == wants.
    raw = 0x3 | masks[2] | in_use
    waiter = events.decode_waiter(raw, 0x0, masks)
    assert waiter is not None
    assert waiter.wants == 0x3
    assert waiter.wait_all is True
    assert waiter.satisfied is False
    assert waiter.missing == 0x3
    assert details_module


def test_iter_waiters_walks_the_item_chain(monkeypatch):
    """iter_waiters decodes each list item's request bits and owner name."""
    layout = build_layout(FreeRtosConfig(tick_bits=32), (10, 3, 1))
    head, end, n1, n2 = object(), object(), object(), object()
    # Identity-based chain: waiting.head -> xListEnd -> n1 -> n2 -> end.
    chain = {head: n1, end: n1, n1: n2, n2: end}
    item_value = {n1: 0x80000003, n2: 0x80000005}  # wants 0x3 / 0x5, IN_USE
    owner_of = {n1: 0x2000, n2: 0x2000}
    step = {"n": 0}

    def fake_read_field(value, _sl, field):
        # Field-by-field: waiting/end/next walk the chain, value/owner read
        # the item's own request/owner (never the chain node).
        if field == "waiting":
            return head
        if field == "end":
            return end
        if field == "next":
            return chain[value]
        if field == "value":
            return item_value.get(value)
        if field == "owner":
            return owner_of.get(value)
        return None

    def fake_safe_dereference(node):
        return node  # nodes are already struct values in this fake

    addrs = {id(n1): 0x2000, id(n2): 0x2008, id(end): 0x1000}

    def fake_value_address(value):
        step["n"] += 1
        return addrs[id(value)]

    def fake_safe_int(value):
        return fake_value_address(value)

    def fake_task_name_at(_owner, _layout):
        return "gdr_blocked"

    monkeypatch.setattr(events, "read_field", fake_read_field)
    monkeypatch.setattr(events, "safe_dereference", fake_safe_dereference)
    monkeypatch.setattr(events, "value_address", fake_value_address)
    monkeypatch.setattr(events, "safe_int", fake_safe_int)
    monkeypatch.setattr(events, "mapped_ranges", lambda: ())
    monkeypatch.setattr(events, "task_name_at", fake_task_name_at)

    waiters = list(events.iter_waiters(object(), layout, 0x3))
    assert step["n"] >= 2  # n1 and n2 both walked
    assert len(waiters) == 2
    assert all(w.task == "gdr_blocked" for w in waiters)
    assert waiters[0].wants == 0x3
    assert waiters[1].wants == 0x5
