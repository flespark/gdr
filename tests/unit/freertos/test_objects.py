"""Unit tests for the FreeRTOS software-timer model and daemon queue.

Covers the display model (``value_to_timer_object``), the ``frt timers``
table contract (headers, epoch-aware ExpiresIn, active-first partition,
early exit), the ``frt timer <name>`` detail (List / OwnerCheck), and the
daemon command queue ring read including wrap.  All GDB entry points stay
monkeypatchable through module-level helpers, following ``test_queues.py``.
"""

from __future__ import annotations

import types

import pytest

import freertos.adapter as adapter_module
import freertos.diagnostics as diagnostics
import freertos.navigation as navigation_module
import freertos.objects as events
import freertos.objects as streams
import freertos.objects as timers
import freertos.views as details_module
from freertos.adapter import FreeRtosTimerObject
from freertos.layout import (
    FreeRtosConfig,
    build_layout,
)
from freertos.navigation import DiscoveredObject
from freertos.objects import (
    EventWaiter,
    FreeRtosEventGroupObject,
    FreeRtosStreamBufferObject,
    TimerCommand,
)
from freertos.views import (
    event_group_detail,
    stream_buffer_detail,
)

# (code, rendered name) pairs for every tmrCOMMAND_* id (include/timers.h).
_COMMAND_NAMES = [
    (-2, "execute-callback-from-isr"),
    (-1, "execute-callback"),
    (0, "start-dont-trace"),
    (1, "start"),
    (2, "reset"),
    (3, "stop"),
    (4, "change-period"),
    (5, "delete"),
    (6, "start-from-isr"),
    (7, "reset-from-isr"),
    (8, "stop-from-isr"),
    (9, "change-period-from-isr"),
]


def _patch_reads(monkeypatch, module, paths: dict):
    """Wire *module*'s read_path/read_field against a path -> value dict.

    ``paths`` keys are raw member-path tuples; the ``read_field`` mock
    resolves logical names through the layout's field paths.
    """
    if hasattr(module, "read_path"):
        monkeypatch.setattr(module, "read_path", lambda _value, path: paths.get(path))

    def fake_read_field(_value, struct_layout, field):
        f = struct_layout.fields.get(field)
        return paths.get(f.path) if f is not None else None

    if hasattr(module, "read_field"):
        monkeypatch.setattr(module, "read_field", fake_read_field)
    monkeypatch.setattr(module, "read_int", lambda value: value)


def _patch_value_reads(monkeypatch, module, by_value: dict):
    """Wire read_field per ``id(value)`` so two timers can differ.

    ``by_value`` keys are raw member-path tuples; the ``read_field`` mock
    resolves the logical name through the layout's field path so both the
    plain and the nested (list-item) reads hit the same data.
    """
    if hasattr(module, "read_path"):
        monkeypatch.setattr(
            module,
            "read_path",
            lambda value, path: by_value.get(id(value), {}).get(path),
        )

    def fake_read_field(value, struct_layout, field):
        f = struct_layout.fields.get(field)
        return by_value.get(id(value), {}).get(f.path) if f is not None else None

    if hasattr(module, "read_field"):
        monkeypatch.setattr(module, "read_field", fake_read_field)
    monkeypatch.setattr(module, "read_int", lambda value: value)


# ---------------------------------------------------------------------------
# state / mode / owner / expiry cells
# ---------------------------------------------------------------------------


def test_owner_check_three_states():
    """uninitialised is decided by the container, never by pvOwner itself."""
    assert timers.owner_check(None, 0xDEADBEEF, 0x2000) == "uninitialised"
    assert timers.owner_check(0, 0xDEADBEEF, 0x2000) == "uninitialised"
    assert timers.owner_check(0x3000, 0x2000, 0x2000) == "ok"
    assert timers.owner_check(0x3000, 0x4000, 0x2000) == "mismatch (pvOwner=0x4000)"
    assert timers.owner_check(0x3000, None, 0x2000) == "unreadable"


def test_state_and_mode_cells_cover_transitions():
    """A '?' marks an in-flight START/STOP, never a guess about who wins."""
    assert timers.state_cell("active", 0x05) == "active"
    assert timers.state_cell("symbol", 0x04) == "dormant"
    assert timers.state_cell("active", 0x04) == "active?"
    assert timers.state_cell("symbol", 0x05) == "dormant?"
    assert timers.state_cell("symbol", None) == "unknown"
    assert timers.mode_cell(0x04) == "auto"
    assert timers.mode_cell(0x01) == "one-shot"
    assert timers.mode_cell(None) == "N/A"


def test_state_cell_treats_active_extra_source_as_linked():
    """A pool-of-record timer corroborated by the active channel is active.

    On an MPU-wrappers build the pool claims every timer first and the
    active channel only adds itself as an extra source; ucStatus and list
    membership do agree, so the row must not render the in-flight ``?``.
    """
    assert timers.timer_is_on_lists("mpu-pool", ("active",)) is True
    assert timers.timer_is_on_lists("mpu-pool", ()) is False
    assert timers.timer_is_on_lists("active", ("mpu-pool",)) is True
    assert timers.state_cell("mpu-pool", 0x05, ("active",)) == "active"
    assert timers.state_cell("mpu-pool", 0x04, ("active",)) == "active?"
    assert timers.state_cell("mpu-pool", 0x05) == "dormant?"


def test_timer_expires_in_handles_overdue_and_overflow():
    """Current-list deltas render overdue instead of a giant wrap value; the
    overflow list belongs to the next tick epoch."""
    mask = (1 << 32) - 1
    assert timers.timer_expires_in(1200, 1000, mask, False) == "200"
    assert timers.timer_expires_in(1000, 1000, mask, False) == "0"
    assert timers.timer_expires_in(900, 1000, mask, False) == "overdue"
    assert timers.timer_expires_in(0x10, 0xFFFFFFF0, mask, True) == "32"
    assert timers.timer_expires_in(None, 1000, mask, False) == "N/A"
    assert timers.timer_expires_in(1200, None, mask, False) == "N/A"


def test_id_and_callback_cells(monkeypatch):
    """A NULL pvTimerID renders '-'; the callback resolves to a symbol."""
    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_timer_callback")
    assert timers.id_cell(None) == "-"
    assert timers.id_cell(0) == "-"
    assert timers.id_cell(7) == "0x7"
    assert timers.callback_cell(None) == "-"
    assert timers.callback_cell(0x80001000) == "<gdr_timer_callback>"


# ---------------------------------------------------------------------------
# timer list epoch / label
# ---------------------------------------------------------------------------


class _FakeListValue:
    def __init__(self, address: int):
        self.address = address


def _wire_timer_lists(
    monkeypatch,
    current_addr: int,
    overflow_addr: int,
    list1_addr: int,
    list2_addr: int,
):
    """The current/overflow pointers dereference to the given List_t values."""
    current = _FakeListValue(current_addr)
    overflow = _FakeListValue(overflow_addr)

    def fake_list_value(name):
        return current if name == "pxCurrentTimerList" else overflow

    def fake_lookup_symbol(name):
        symbols = {
            "xActiveTimerList1": _FakeListValue(list1_addr),
            "xActiveTimerList2": _FakeListValue(list2_addr),
        }
        return symbols.get(name)

    monkeypatch.setattr(timers, "_timer_list_value", fake_list_value)
    monkeypatch.setattr(timers, "value_address", lambda value: value.address)
    monkeypatch.setattr(timers, "lookup_symbol", fake_lookup_symbol)


def test_timer_list_label_resolves_current_and_overflow_symbols(monkeypatch):
    """The label names the *underlying* list symbol, not a guessable index."""
    _wire_timer_lists(monkeypatch, 0x3000, 0x3100, list1_addr=0x3000, list2_addr=0x3100)
    assert timers.timer_list_label(0x3000) == "current(xActiveTimerList1)"
    assert timers.timer_list_label(0x3100) == "overflow(xActiveTimerList2)"
    assert timers.timer_list_label(None) == "none"
    assert timers.timer_list_label(0) == "none"


def test_timer_epoch_follows_swapped_pointers(monkeypatch):
    """After a tick overflow the pointers swap, and the epoch follows them."""
    _wire_timer_lists(monkeypatch, 0x3100, 0x3000, list1_addr=0x3100, list2_addr=0x3000)
    assert timers.timer_epoch(0x3100) == "current"
    assert timers.timer_epoch(0x3000) == "overflow"
    assert timers.timer_epoch(0x9999) is None


# ---------------------------------------------------------------------------
# daemon-is-current
# ---------------------------------------------------------------------------


def test_daemon_is_current_detects_running_daemon(monkeypatch):
    """xTimerTaskHandle == pxCurrentTCB earns the mid-update warning."""
    daemon_ref = _FakeListValue(0x2000)
    monkeypatch.setattr(
        timers,
        "lookup_symbol",
        lambda name: daemon_ref if name == "xTimerTaskHandle" else None,
    )
    monkeypatch.setattr(timers, "safe_dereference", lambda value: value)
    monkeypatch.setattr(timers, "current_tasks", lambda _layout: [(0, 0x2000)])
    assert timers.daemon_is_current(build_layout(FreeRtosConfig())) is True
    monkeypatch.setattr(timers, "current_tasks", lambda _layout: [(0, 0x3000)])
    assert timers.daemon_is_current(build_layout(FreeRtosConfig())) is False
    monkeypatch.setattr(timers, "current_tasks", lambda _layout: [])
    assert timers.daemon_is_current(build_layout(FreeRtosConfig())) is False


# ---------------------------------------------------------------------------
# value_to_timer_object / table rows
# ---------------------------------------------------------------------------


def _timer_layout() -> FreeRtosConfig:
    # Reason: the fixture's default build spells the list-item container
    # member pvContainer (configENABLE_BACKWARD_COMPATIBILITY), so the
    # paths served below use that spelling.
    return FreeRtosConfig(
        timers=True, trace_facility=True, list_item_container_field="pvContainer"
    )


def _table_cells(monkeypatch, found, by_address, tick=1000, daemon=False):
    """Build the timer table for one mocked discovery result.

    ``by_address`` maps each timer address to ``(value, paths)`` so the
    table reads each object's own fields.
    """
    layout = build_layout(_timer_layout(), (10, 3, 1))
    by_value = {id(value): paths for _address, (value, paths) in by_address.items()}
    _patch_value_reads(monkeypatch, adapter_module, by_value)

    def fake_cast(address, _kind, _layout):
        entry = by_address.get(address)
        return entry[0] if entry else None

    monkeypatch.setattr(adapter_module, "_cast_object", fake_cast)
    monkeypatch.setattr(adapter_module, "discover", lambda _kind, _l: found)
    monkeypatch.setattr(adapter_module, "timer_subsystem_ready", lambda: True)
    monkeypatch.setattr(adapter_module, "system_value", lambda _key, _layout: tick)
    monkeypatch.setattr(adapter_module, "daemon_is_current", lambda _l: daemon)
    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_timer_callback")
    monkeypatch.setattr(
        adapter_module, "lookup_symbol_at", lambda _addr: "gdr_timer_callback"
    )
    monkeypatch.setattr(adapter_module, "source_location_at", lambda _addr: None)
    monkeypatch.setattr(adapter_module, "source_definition_at", lambda _addr: None)
    _wire_timer_lists(monkeypatch, 0x3000, 0x3100, list1_addr=0x3000, list2_addr=0x3100)
    return adapter_module.FreeRtosAdapter(layout)._timer_table()


def _timer_paths(expiry, container, owner, status=0x05):
    return {
        ("xTimerPeriodInTicks",): 100,
        ("ucStatus",): status,
        ("xTimerListItem", "xItemValue"): expiry,
        ("pxCallbackFunction",): 0x80001000,
        ("pvTimerID",): 0,
        ("xTimerListItem", "pvOwner"): owner,
        ("xTimerListItem", "pvContainer"): container,
        ("uxTimerNumber",): 7,
    }


def test_timers_table_headers(monkeypatch):
    """The header contract is the exact ten-column Plan list."""
    found = [
        DiscoveredObject(kind="timer", address=0x2000, name="gdr_a", source="active")
    ]
    by_address = {0x2000: (object(), _timer_paths(1200, 0x3000, 0x2000))}
    table = _table_cells(monkeypatch, found, by_address)
    assert table.headers == [
        "Name",
        "State",
        "Mode",
        "Period",
        "Expiry",
        "ExpiresIn",
        "Callback",
        "ID",
        "Src",
        "Addr",
    ]
    assert any("Kernel tick" in message for message in table.messages)


def test_active_and_overflow_rows_expire_in(monkeypatch):
    """current-list rows use expiry - tick; overflow rows add the next epoch."""
    found = [
        DiscoveredObject(kind="timer", address=0x2000, name="gdr_a", source="active"),
        DiscoveredObject(kind="timer", address=0x2100, name="gdr_b", source="active"),
    ]
    by_address = {
        0x2000: (object(), _timer_paths(1200, 0x3000, 0x2000)),
        0x2100: (object(), _timer_paths(0x20, 0x3100, 0x2100)),
    }
    table = _table_cells(monkeypatch, found, by_address, tick=1000)
    row_a = table.rows[0]
    row_b = table.rows[1]
    assert row_a[4] == "1200"  # Expiry
    assert row_a[5] == "200"  # ExpiresIn = expiry - tick
    assert row_b[5] == str((2**32 - 1000) + 0x20)  # (2^bits - tick) + expiry


def test_null_current_timer_list_early_exit(monkeypatch):
    """pxCurrentTimerList == NULL means the subsystem never ran: no rows."""
    layout = build_layout(_timer_layout(), (10, 3, 1))
    monkeypatch.setattr(timers, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(timers, "safe_dereference", lambda _value: None)
    table = adapter_module.FreeRtosAdapter(layout)._timer_table()
    assert table.rows == []
    assert any("not initialised" in message for message in table.messages)
    assert not any("Kernel tick" in message for message in table.messages)


def test_dormant_rows_sort_after_active_and_render_na(monkeypatch):
    """Active (source=active) rows come first; dormant rows never render a
    stale list-item value as Expiry/ExpiresIn."""
    found = [
        DiscoveredObject(
            kind="timer", address=0x2200, name="gdr_idle", source="symbol"
        ),
        DiscoveredObject(kind="timer", address=0x2000, name="gdr_a", source="active"),
    ]
    by_address = {
        0x2000: (object(), _timer_paths(1200, 0x3000, 0x2000)),
        # The dormant timer keeps a stale (never-initialised) list item.
        0x2200: (object(), _timer_paths(0xDEADBEEF, 0, 0xDEADBEEF, status=0x00)),
    }
    table = _table_cells(monkeypatch, found, by_address, tick=1000)
    assert [row[0] for row in table.rows] == ["gdr_a", "gdr_idle"]
    assert table.rows[1][1] == "dormant"
    assert table.rows[1][4] == "N/A"  # Expiry
    assert table.rows[1][5] == "N/A"  # ExpiresIn
    assert any("not referenced by any kernel global" in m for m in table.messages)


def test_transition_state_adds_pending_command_message(monkeypatch):
    """A status/list disagreement surfaces as a pending-command note."""
    found = [
        DiscoveredObject(kind="timer", address=0x2000, name="gdr_x", source="active")
    ]
    by_address = {0x2000: (object(), _timer_paths(1200, 0x3000, 0x2000, status=0x04))}
    table = _table_cells(monkeypatch, found, by_address, tick=1000)
    assert table.rows[0][1] == "active?"
    assert any("queued start/stop" in message for message in table.messages)


def test_daemon_current_warning_lands_in_table_messages(monkeypatch):
    found = [
        DiscoveredObject(kind="timer", address=0x2000, name="gdr_a", source="active")
    ]
    by_address = {0x2000: (object(), _timer_paths(1200, 0x3000, 0x2000))}
    table = _table_cells(monkeypatch, found, by_address, tick=1000, daemon=True)
    assert any("mid-update" in message for message in table.messages)


def test_value_to_timer_object_gates_timer_number_on_trace_facility(monkeypatch):
    """uxTimerNumber is only read when the trace facility exists."""
    found = DiscoveredObject(
        kind="timer", address=0x2000, name="gdr_a", source="active"
    )
    paths = {
        ("xTimerPeriodInTicks",): 100,
        ("ucStatus",): 0x05,
        ("xTimerListItem", "xItemValue"): 1200,
        ("pxCallbackFunction",): 0x80001000,
        ("pvTimerID",): 7,
        ("xTimerListItem", "pvOwner"): 0x2000,
        ("xTimerListItem", "pvContainer"): 0x3000,
        ("uxTimerNumber",): 7,
    }
    _patch_reads(monkeypatch, adapter_module, paths)
    on = adapter_module.value_to_timer_object(
        object(),
        found,
        build_layout(
            FreeRtosConfig(
                trace_facility=True, list_item_container_field="pvContainer"
            ),
            (10, 3, 1),
        ),
    )
    off = adapter_module.value_to_timer_object(
        object(),
        found,
        build_layout(
            FreeRtosConfig(
                trace_facility=False, list_item_container_field="pvContainer"
            ),
            (10, 3, 1),
        ),
    )
    assert on.timer_number == 7
    assert on.id == 7
    assert off.timer_number is None

    none_value = adapter_module.value_to_timer_object(
        None, found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    assert none_value.name == "gdr_a"
    assert none_value.address == 0x2000
    assert none_value.period is None


# ---------------------------------------------------------------------------
# daemon command queue
# ---------------------------------------------------------------------------


def _wire_command_queue(monkeypatch, paths: dict, msg_size: int = 12):
    """Serve xTimerQueue + the DaemonTaskMessage_t type through helpers."""
    queue_ref = object()
    monkeypatch.setattr(
        timers,
        "lookup_symbol",
        lambda name: queue_ref if name == "xTimerQueue" else None,
    )
    monkeypatch.setattr(
        timers, "safe_dereference", lambda value: value if value is queue_ref else None
    )
    msg_type = types.SimpleNamespace(sizeof=msg_size)
    monkeypatch.setattr(
        timers,
        "lookup_type",
        lambda name: msg_type if name == "struct tmrTimerQueueMessage" else None,
    )
    _patch_reads(monkeypatch, timers, paths)
    # iter_queue_items now lives in navigation (C4), so its layout reads and
    # payload bytes are mocked there.
    _patch_reads(monkeypatch, navigation_module, paths)
    monkeypatch.setattr(
        navigation_module, "read_bytes", lambda _address, size: b"\x00" * size
    )
    monkeypatch.setattr(timers, "_callback_arm_present", lambda _t: False)
    return queue_ref, msg_type


def test_timer_commands_ring_wraps(monkeypatch):
    """Three pending commands, pcReadFrom on the last slot: the walk wraps
    back past pcHead (kernel >= wrap), and every slot is decoded."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    paths = {
        ("pcHead",): 0x2000,
        ("u", "xQueue", "pcTail"): 0x2010,
        ("u", "xQueue", "pcReadFrom"): 0x200C,  # last of 4 slots
        ("uxLength",): 4,
        ("uxMessagesWaiting",): 3,
        ("uxItemSize",): 12,
        ("xMessageID",): 3,  # tmrCOMMAND_STOP
        ("u", "xTimerParameters", "pxTimer"): 0x3000,
        ("u", "xTimerParameters", "xMessageValue"): 77,
    }
    _wire_command_queue(monkeypatch, paths)
    seen: list[int] = []

    def fake_message_value(address, _msg_type):
        seen.append(address)
        return object()

    monkeypatch.setattr(timers, "_message_value", fake_message_value)

    messages, commands = timers.iter_timer_commands(layout)

    assert messages == []
    assert seen == [0x2008, 0x2004, 0x2000]  # wrapped past pcTail back to pcHead
    assert [cmd.seq for cmd in commands] == [0, 1, 2]
    assert all(cmd.message_id == 3 for cmd in commands)
    assert all(cmd.timer_address == 0x3000 for cmd in commands)
    assert all(cmd.message_value == 77 for cmd in commands)


def test_timer_commands_reject_wrong_item_size(monkeypatch):
    """uxItemSize != sizeof(DaemonTaskMessage_t) is a refused queue."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    _wire_command_queue(monkeypatch, {("uxItemSize",): 16}, msg_size=12)

    messages, commands = timers.iter_timer_commands(layout)

    assert commands == []
    assert messages == ["skipped: item size 16 != sizeof(DaemonTaskMessage_t) 12"]


def test_timer_commands_reports_empty_queue(monkeypatch):
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    _wire_command_queue(monkeypatch, {("uxItemSize",): 12, ("uxMessagesWaiting",): 0})
    messages, commands = timers.iter_timer_commands(layout)
    assert messages == ["no pending timer commands"]
    assert commands == []


def test_timer_commands_waiting_but_unwalkable_is_not_reported_empty(monkeypatch):
    """uxMessagesWaiting > 0 with an unwalkable queue must not render as
    "no pending" -- that would mask a corrupt queue as an empty one."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    paths = {
        ("uxLength",): 4,
        ("uxMessagesWaiting",): 3,
        ("uxItemSize",): 12,
        # pcHead / pcTail / pcReadFrom unreadable: the FIFO walk cannot run.
    }
    _wire_command_queue(monkeypatch, paths)
    messages, commands = timers.iter_timer_commands(layout)
    assert commands == []
    assert messages == ["timer command queue slots unreadable (3 message(s) waiting)"]


def test_timer_commands_degrade_without_queue_or_type(monkeypatch):
    layout = build_layout(FreeRtosConfig(timers=False), (10, 3, 1))
    messages, commands = timers.iter_timer_commands(layout)
    assert messages == ["no software timers in this build (configUSE_TIMERS=0)"]
    assert commands == []

    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    monkeypatch.setattr(timers, "lookup_symbol", lambda _name: None)
    messages, _commands = timers.iter_timer_commands(layout)
    assert messages == ["timer command queue xTimerQueue is unavailable"]


@pytest.mark.parametrize(("code", "name"), _COMMAND_NAMES)
def test_command_names_cover_all_codes(code, name):
    """Every tmrCOMMAND_* id -2..9 renders a stable name."""
    assert timers.command_name(code) == name
    assert timers.command_name(42) == "unknown(42)"


def test_negative_id_without_callback_arm_is_reported(monkeypatch):
    """A negative xMessageID can only be a pended callback when the union arm
    exists; without it the slot is reported, never decoded as a callback."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    paths = {
        ("pcHead",): 0x2000,
        ("u", "xQueue", "pcTail"): 0x200C,
        ("u", "xQueue", "pcReadFrom"): 0x2000,
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 1,
        ("uxItemSize",): 12,
        ("xMessageID",): -1,  # tmrCOMMAND_EXECUTE_CALLBACK
    }
    _wire_command_queue(monkeypatch, paths)
    monkeypatch.setattr(timers, "_message_value", lambda _a, _t: object())

    messages, commands = timers.iter_timer_commands(layout)
    assert messages == []
    assert commands[0].message_id == -1
    assert commands[0].callback_arm is False

    cell = timers.commands_cell(messages, commands, layout)
    assert "pended-callback arm absent (INCLUDE_xTimerPendFunctionCall=0)" in cell


def test_commands_cell_renders_table_names_and_messages(monkeypatch):
    """Names resolve to the pcTimerName; the arm-present branch renders the
    pended-callback cell; a messages list is rendered verbatim."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    monkeypatch.setattr(
        timers, "timer_name_at", lambda address, _l: "gdr_active" if address else None
    )
    commands = [
        TimerCommand(seq=0, message_id=1, timer_address=0x3000, message_value=77),
        TimerCommand(seq=1, message_id=3, timer_address=0x3100, message_value=0),
        TimerCommand(
            seq=2,
            message_id=-1,
            callback_arm=True,
            callback_function=0x80001000,
            callback_parameter1=0xCAFEF00D,
            callback_parameter2=0x5A5A5A5A,
        ),
        TimerCommand(seq=3, message_id=None),
    ]
    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_pended_function")
    cell = timers.commands_cell([], commands, layout)
    assert "start" in cell and "gdr_active" in cell
    assert "execute-callback" in cell
    assert "gdr_pended_function" in cell
    assert "0xcafef00d" in cell and "0x5a5a5a5a" in cell
    assert "unreadable" in cell
    assert "Seq" in cell and "Command" in cell and "Timer" in cell and "Value" in cell

    assert timers.commands_cell(["no pending timer commands"], [], layout) == (
        "no pending timer commands"
    )


def test_negative_id_decodes_callback_parameters(monkeypatch):
    """With the union arm present the negative-ID slot carries the real
    u.xCallbackParameters (function + both arguments), not a label."""
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    paths: dict[tuple[str, ...], object] = {
        ("pcHead",): 0x2000,
        ("u", "xQueue", "pcTail"): 0x200C,
        ("u", "xQueue", "pcReadFrom"): 0x2000,
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 1,
        ("uxItemSize",): 12,
        ("xMessageID",): -1,  # tmrCOMMAND_EXECUTE_CALLBACK
        # The callback union arm, reached through the daemon-message layout.
        ("u", "xCallbackParameters", "pxCallbackFunction"): 0x80001000,
        ("u", "xCallbackParameters", "pvParameter1"): 0xCAFEF00D,
        ("u", "xCallbackParameters", "ulParameter2"): 0x5A5A5A5A,
    }

    _wire_command_queue(monkeypatch, paths)
    monkeypatch.setattr(timers, "_message_value", lambda _a, _t: object())
    monkeypatch.setattr(timers, "_callback_arm_present", lambda _t: True)

    messages, commands = timers.iter_timer_commands(layout)
    assert messages == []
    assert commands[0].message_id == -1
    assert commands[0].callback_arm is True
    assert commands[0].callback_function == 0x80001000
    assert commands[0].callback_parameter1 == 0xCAFEF00D
    assert commands[0].callback_parameter2 == 0x5A5A5A5A

    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_pended_function")
    cell = timers.commands_cell(messages, commands, layout)
    assert "gdr_pended_function" in cell
    assert "0xcafef00d" in cell and "0x5a5a5a5a" in cell
    assert "pended-callback arm absent" not in cell


def test_timer_commands_skip_slot_when_message_unreadable(monkeypatch):
    layout = build_layout(FreeRtosConfig(timers=True), (10, 3, 1))
    paths = {
        ("pcHead",): 0x2000,
        ("u", "xQueue", "pcTail"): 0x200C,
        ("u", "xQueue", "pcReadFrom"): 0x2000,
        ("uxLength",): 1,
        ("uxMessagesWaiting",): 1,
        ("uxItemSize",): 12,
    }
    _wire_command_queue(monkeypatch, paths)
    monkeypatch.setattr(timers, "_message_value", lambda _a, _t: None)

    messages, commands = timers.iter_timer_commands(layout)
    assert messages == []
    assert commands[0].message_id is None


# ---------------------------------------------------------------------------
# timer detail contract
# ---------------------------------------------------------------------------


def test_timer_detail_key_contract_and_commands_section(monkeypatch):
    """frt timer <name> emits the documented keys, Checks, and the Commands
    section (honest empty state from the daemon queue)."""
    layout = build_layout(_timer_layout(), (10, 3, 1))
    obj = FreeRtosTimerObject(
        name="gdr_active",
        address=0x2000,
        source="active",
        period=100,
        status=0x05,
        expiry=1200,
        callback=0x80001000,
        id=0,
        owner=0x2000,
        container=0x3000,
    )
    _wire_timer_lists(monkeypatch, 0x3000, 0x3100, list1_addr=0x3000, list2_addr=0x3100)
    monkeypatch.setattr(details_module, "system_value", lambda _key, _layout: 1000)
    monkeypatch.setattr(
        timers,
        "iter_timer_commands",
        lambda _layout: (["no pending timer commands"], []),
    )
    monkeypatch.setattr(timers, "daemon_is_current", lambda _layout: False)
    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_timer_callback")
    _patch_reads(
        monkeypatch,
        diagnostics,
        {
            ("ucStatus",): 0x05,
            ("xTimerPeriodInTicks",): 100,
            ("pxCallbackFunction",): 0x80001000,
            ("xTimerListItem", "pvContainer"): 0x3000,
        },
    )
    monkeypatch.setattr(diagnostics, "safe_dereference", lambda _v: None)
    monkeypatch.setattr(diagnostics, "lookup_symbol", lambda _n: None)
    monkeypatch.setattr(diagnostics, "lookup_type", lambda _n: None)

    pairs = details_module.timer_detail(obj, object(), layout)
    keys = [key for key, _value in pairs]
    assert keys[:12] == [
        "Name",
        "Address",
        "State",
        "Mode",
        "Period",
        "Expiry",
        "ExpiresIn",
        "Callback",
        "ID",
        "List",
        "OwnerCheck",
        "Src",
    ]
    assert "Checks" not in keys
    assert "Check[TimerQueueItemSize]" in keys
    assert "Commands" in keys
    pairs_by_key = dict(pairs)
    assert pairs_by_key["State"] == "active"
    assert pairs_by_key["List"] == "current(xActiveTimerList1)"
    assert pairs_by_key["OwnerCheck"] == "ok"
    assert pairs_by_key["ExpiresIn"] == "200"
    assert pairs_by_key["Commands"] == "no pending timer commands"

    # While the daemon runs the warning leads the section, but a decodable
    # command table must survive beneath it.
    monkeypatch.setattr(timers, "daemon_is_current", lambda _layout: True)
    pairs = details_module.timer_detail(obj, object(), layout)
    commands_value = dict(pairs)["Commands"]
    assert "mid-update" in commands_value
    assert "no pending timer commands" in commands_value


def test_timer_checks_consistency(monkeypatch):
    """StatusListSync flags a queued-command disagreement; a healthy timer
    with a matched command queue passes every check."""
    paths = {
        ("ucStatus",): 0x01,  # active bit set but item unlinked => mismatch
        ("xTimerPeriodInTicks",): 0,
        ("pxCallbackFunction",): 0,
        ("xTimerListItem", "pvContainer"): 0,
        ("uxItemSize",): 12,
    }
    _patch_reads(monkeypatch, diagnostics, paths)
    layout = build_layout(_timer_layout(), (10, 3, 1))
    monkeypatch.setattr(diagnostics, "safe_dereference", lambda _v: object())
    monkeypatch.setattr(diagnostics, "lookup_symbol", lambda _n: object())
    msg_type = types.SimpleNamespace(sizeof=12)
    monkeypatch.setattr(diagnostics, "lookup_type", lambda _n: msg_type)

    results = dict(diagnostics.timer_checks(object(), layout))

    assert results["StatusListSync"].startswith("fail: ")
    assert results["Period"].startswith("fail: ")
    assert results["Callback"].startswith("fail: ")
    assert results["TimerQueueItemSize"] == "ok"

    # A healthy timer agrees with its list and has a sane period/callback.
    healthy = {
        ("ucStatus",): 0x05,
        ("xTimerPeriodInTicks",): 100,
        ("pxCallbackFunction",): 0x80001000,
        ("xTimerListItem", "pvContainer"): 0x3000,
        ("uxItemSize",): 12,
    }
    _patch_reads(monkeypatch, diagnostics, healthy)
    results = dict(diagnostics.timer_checks(object(), layout))
    assert all(status == "ok" for status in results.values())

    # Unreadable fields keep the check honest as skipped, and item-size
    # mismatch fails instead of passing.
    _patch_reads(monkeypatch, diagnostics, {})
    results = dict(diagnostics.timer_checks(object(), layout))
    assert results["StatusListSync"] == "skipped: unreadable"
    assert results["TimerQueueItemSize"] == "skipped: unreadable"

    _patch_reads(monkeypatch, diagnostics, {("uxItemSize",): 16})
    results = dict(diagnostics.timer_checks(object(), layout))
    assert results["TimerQueueItemSize"].startswith("fail: ")


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


def test_unreadable_current_bits_reports_unknown_missing():
    """Unreadable uxEventBits must leave missing as None / '-' rather than assuming 0."""
    masks = events.event_bit_masks(32)
    for raw in (0x3 | masks[2] | (1 << 31), 0x3 | (1 << 31)):
        waiter = events.decode_waiter(raw, None, masks)
        assert waiter is not None
        assert waiter.satisfied is False
        assert waiter.missing is None
        assert "missing=-" in events.format_waiter_line(waiter)


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


@pytest.mark.parametrize(
    ("version", "raw_val"),
    [((10, 4, 6), 0x84000003), ((11, 1, 0), 0x8400000000000003)],
)
def test_iter_waiters_handles_64bit_tick_control_bit_era(monkeypatch, version, raw_val):
    """Pre-10.5.0 64-bit TickType_t kernels still place control/IN_USE bits in bits [31:24]."""
    layout = build_layout(FreeRtosConfig(tick_bits=64), version)
    head, end, node = object(), object(), object()
    chain = {head: node, end: node, node: end}

    def fake_read_field(value, _sl, field):
        if field == "waiting":
            return head
        if field == "end":
            return end
        if field == "next":
            return chain[value]
        if field == "value":
            return raw_val
        if field == "owner":
            return 0x2000
        return None

    monkeypatch.setattr(events, "read_field", fake_read_field)
    monkeypatch.setattr(events, "safe_dereference", lambda v: v)
    monkeypatch.setattr(events, "value_address", lambda v: 0x100 if v is end else 0x200)
    monkeypatch.setattr(events, "safe_int", lambda v: 0x100 if v is end else 0x200)
    monkeypatch.setattr(events, "task_name_at", lambda _o, _l: "evw")
    monkeypatch.setattr(events, "mapped_ranges", lambda: ())

    waiters = list(events.iter_waiters(object(), layout, current_bits=0))
    assert len(waiters) == 1
    assert waiters[0].wants == 0x3
    assert waiters[0].wait_all is True
    assert waiters[0].in_use is True


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


def _patch_stream_reads(monkeypatch, paths: dict):
    """Wire ``streams.read_field`` against a path -> value dict.

    ``paths`` keys are raw member-path tuples; the mock resolves logical
    names through the layout's field paths so value_to_stream_buffer_object
    and bounds_check read the same data.
    """

    def fake_read_field(_value, struct_layout, field):
        f = struct_layout.fields.get(field)
        return paths.get(f.path) if f is not None else None

    monkeypatch.setattr(streams, "read_field", fake_read_field)


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
    _patch_stream_reads(monkeypatch, {("xLength",): 0, ("pucBuffer",): 0})
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
    _patch_stream_reads(monkeypatch, {("xLength",): 0, ("pucBuffer",): 0})
    obj = streams.value_to_stream_buffer_object(object(), found, layout)
    pairs = stream_buffer_detail(obj, object(), layout)

    pairs_by_key = dict(pairs)
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
    monkeypatch.setattr(streams, "type_size", lambda _name: None)

    # Message buffer with an empty ring: no length prefix stored -> None.
    found, paths = _message_buffer_object()
    _patch_stream_reads(monkeypatch, paths)
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    assert obj.kind == "message"
    assert obj.next_message is None

    # A plain stream buffer never computes NextMsg (N/A, not 0).
    stream_paths = dict(paths)
    stream_paths[("ucFlags",)] = 0
    _patch_stream_reads(monkeypatch, stream_paths)
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )
    assert obj.kind == "stream"
    assert obj.next_message is None


def test_message_length_size_falls_back_to_size_t(monkeypatch):
    """Without the macro typedef, sizeof(size_t) is used and marked assumed."""

    def lookup(name):
        if name == "configMESSAGE_BUFFER_LENGTH_TYPE":
            return None
        if name == "size_t":
            return 8
        return None

    monkeypatch.setattr(streams, "type_size", lookup)
    size, assumed = streams.message_length_size()
    assert (size, assumed) == (8, True)

    def lookup_precise(name):
        if name == "configMESSAGE_BUFFER_LENGTH_TYPE":
            return 2
        return None

    monkeypatch.setattr(streams, "type_size", lookup_precise)
    size, assumed = streams.message_length_size()
    assert (size, assumed) == (2, False)


def test_notification_index_absent_before_11_1():
    """A kernel without uxNotificationIndex renders the gate, not a fade."""
    layout = build_layout(FreeRtosConfig(), (10, 3, 1))  # gate off
    obj = FreeRtosStreamBufferObject(
        name="g", address=0x10, kind="message", flags=1, source="symbol"
    )
    pairs = stream_buffer_detail(obj, object(), layout)
    pairs_by_key = dict(pairs)
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
    monkeypatch.setattr(streams, "type_size", lambda _name: None)
    _patch_stream_reads(monkeypatch, paths)
    obj = streams.value_to_stream_buffer_object(object(), found, layout)

    assert obj.notification_index == 7
    pairs = stream_buffer_detail(obj, object(), layout)
    pairs_by_key = dict(pairs)
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
    monkeypatch.setattr(streams, "type_size", lambda _name: None)
    _patch_stream_reads(monkeypatch, paths)
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

    monkeypatch.setattr(streams, "type_size", lambda _name: None)
    _patch_stream_reads(monkeypatch, paths)
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

    monkeypatch.setattr(streams, "type_size", lambda _name: None)
    _patch_stream_reads(monkeypatch, paths)
    monkeypatch.setattr(streams, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(streams, "get_arch_info", lambda: None)
    obj = streams.value_to_stream_buffer_object(
        object(), found, build_layout(FreeRtosConfig(), (10, 3, 1))
    )

    # 8 bytes stored (head wrapped to 2): bytes > 4 so the prefix is read.
    assert obj.bytes_used == 8
    assert reads == [(0x101F, 2), (0x1000, 2)], reads
