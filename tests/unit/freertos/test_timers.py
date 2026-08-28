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
import freertos.details as details_module
import freertos.diagnostics as diagnostics
import freertos.timers as timers
from freertos.adapter import FreeRtosTimerObject
from freertos.layout import FreeRtosConfig, build_layout
from freertos.navigation import DiscoveredObject
from freertos.timers import TimerCommand

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
    """Wire *module*'s read_path/read_int against a path -> value dict."""
    monkeypatch.setattr(module, "read_path", lambda _value, path: paths.get(path))
    monkeypatch.setattr(module, "read_int", lambda value: value)


def _patch_value_reads(monkeypatch, module, by_value: dict):
    """Wire read_path per ``id(value)`` so two timers can differ."""
    monkeypatch.setattr(
        module, "read_path", lambda value, path: by_value.get(id(value), {}).get(path)
    )
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
    monkeypatch.setattr(adapter_module, "system_value", lambda _name: tick)
    monkeypatch.setattr(adapter_module, "daemon_is_current", lambda _l: daemon)
    monkeypatch.setattr(timers, "lookup_symbol_at", lambda _addr: "gdr_timer_callback")
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
    _patch_reads(monkeypatch, details_module, paths)
    monkeypatch.setattr(
        details_module, "read_bytes", lambda _address, size: b"\x00" * size
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
        TimerCommand(seq=2, message_id=-1, callback_arm=True),
        TimerCommand(seq=3, message_id=None),
    ]
    cell = timers.commands_cell([], commands, layout)
    assert "start" in cell and "gdr_active" in cell
    assert "pended callback" in cell
    assert "unreadable" in cell
    assert "Seq" in cell and "Command" in cell and "Timer" in cell and "Value" in cell

    assert timers.commands_cell(["no pending timer commands"], [], layout) == (
        "no pending timer commands"
    )


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
    monkeypatch.setattr(details_module, "system_value", lambda _name: 1000)
    monkeypatch.setattr(
        details_module,
        "iter_timer_commands",
        lambda _layout: (["no pending timer commands"], []),
    )
    monkeypatch.setattr(details_module, "daemon_is_current", lambda _layout: False)
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
    assert "Checks" in keys
    assert "Commands" in keys
    pairs_by_key = dict(pairs)
    assert pairs_by_key["State"] == "active"
    assert pairs_by_key["List"] == "current(xActiveTimerList1)"
    assert pairs_by_key["OwnerCheck"] == "ok"
    assert pairs_by_key["ExpiresIn"] == "200"
    assert pairs_by_key["Commands"] == "no pending timer commands"

    # While the daemon runs the warning leads the section, but a decodable
    # command table must survive beneath it.
    monkeypatch.setattr(details_module, "daemon_is_current", lambda _layout: True)
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
