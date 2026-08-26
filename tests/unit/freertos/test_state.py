"""Table-driven tests for the per-TCB FreeRTOS state algorithm (tasks.c)."""

from __future__ import annotations

import freertos.navigation as navigation
from freertos.layout import FreeRtosConfig, build_layout

# Global scheduler-list addresses used by the fake symbol table.
PENDING = 0x0100
DELAYED = 0x0200
OVERFLOW = 0x0300
SUSPENDED = 0x0400
TERMINATION = 0x0500
CURRENT_TCB = 0x0600


class _Addr:
    def __init__(self, value: int):
        self.value = value

    def __int__(self) -> int:
        return self.value


class _Target:
    """A list/TCB value: ``.address`` feeds value_address, ``int()`` the pointer."""

    def __init__(self, value: int):
        self._value = value
        self.address = _Addr(value)

    def __int__(self) -> int:
        return self._value


class _Ptr:
    """A pointer to a _Target; dereference() yields the pointee."""

    def __init__(self, target: _Target):
        self.target = target

    def __int__(self) -> int:
        return int(self.target)

    def dereference(self) -> _Target:
        return self.target


class _Array:
    """Indexable fake for pxCurrentTCBs (list of _Ptr)."""

    def __init__(self, pointers: list[_Ptr]):
        self.pointers = pointers

    def __getitem__(self, index: int) -> _Ptr:
        return self.pointers[index]


class _Slots:
    """Indexable fake for the ucNotifyState array member."""

    def __init__(self, values: list[int]):
        self.values = values

    def __getitem__(self, index: int) -> int:
        return self.values[index]


def _task_state(
    monkeypatch,
    *,
    smp: bool = False,
    cores: int = 2,
    tcb_addr: int = CURRENT_TCB,
    current: int | None = None,
    state_container: int | None = None,
    event_container: int | None = None,
    run_state: int | None = None,
    notify_state: _Slots | int | None = None,
    notification_array: bool = False,
    current_tcbs: _Array | None = None,
):
    """Drive task_state() against a controlled TCB/list world."""
    config = FreeRtosConfig(
        smp=smp,
        number_of_cores=cores,
        notifications=True,
        # Reason: the notification member is a scalar before V10.4.0 and an
        # array from V10.4.0 on; the fake must carry the shape the config
        # declares or the scalar branch never gets exercised.
        notification_array=notification_array,
        notification_count=(
            len(notify_state.values)
            if notification_array and isinstance(notify_state, _Slots)
            else 1
        ),
        tcb_fields=frozenset({"xTaskRunState"} if smp else ()),
    )
    layout = build_layout(config, (10, 5, 1))
    tcb = _Target(tcb_addr)
    state_item = object()
    event_item = object()

    def read_field(value, _struct_layout, field_name):
        if value is tcb:
            return {
                "state_list_item": state_item,
                "event_list_item": event_item,
                "run_state": run_state,
                "notify_state": notify_state,
            }.get(field_name)
        if value is state_item and field_name == "container":
            return state_container
        if value is event_item and field_name == "container":
            return event_container
        return None

    def lookup_symbol(name):
        plain = {
            "xPendingReadyList": PENDING,
            "xSuspendedTaskList": SUSPENDED,
            "xTasksWaitingTermination": TERMINATION,
        }
        if name in plain:
            return _Target(plain[name])
        if name == "pxDelayedTaskList":
            return _Ptr(_Target(DELAYED))
        if name == "pxOverflowDelayedTaskList":
            return _Ptr(_Target(OVERFLOW))
        if name == "pxCurrentTCB":
            return _Ptr(_Target(current)) if current is not None else None
        if name == "pxCurrentTCBs":
            return current_tcbs
        return None

    monkeypatch.setattr(navigation, "read_field", read_field)
    monkeypatch.setattr(navigation, "lookup_symbol", lookup_symbol)
    return navigation.task_state(tcb, layout)


# --- the six main branches ---------------------------------------------------


def test_single_core_current_tcb_is_running(monkeypatch):
    """The task behind pxCurrentTCB reports Running on core 0."""
    assert _task_state(monkeypatch, current=CURRENT_TCB) == ("Running", 0)


def test_event_list_on_pending_ready_is_ready(monkeypatch):
    """xEventListItem on xPendingReadyList wins over any state-list position."""
    assert _task_state(monkeypatch, event_container=PENDING) == ("Ready", None)


def test_state_list_on_delayed_is_blocked(monkeypatch):
    """The active delayed list classifies the task as Blocked."""
    assert _task_state(monkeypatch, state_container=DELAYED) == ("Blocked", None)


def test_state_list_on_overflow_delayed_is_blocked(monkeypatch):
    """The overflow delayed list also classifies the task as Blocked."""
    assert _task_state(monkeypatch, state_container=OVERFLOW) == ("Blocked", None)


def test_termination_list_is_deleted(monkeypatch):
    """xTasksWaitingTermination yields Deleted."""
    assert _task_state(monkeypatch, state_container=TERMINATION) == (
        "Deleted",
        None,
    )


def test_state_list_null_is_deleted(monkeypatch):
    """A NULL state-list container means the task object is gone (eDeleted)."""
    # Reason: a real NULL container is a pointer value of 0, distinct from an
    # unreadable container (None). The fake maps container=0 to safe_int=0.
    assert _task_state(monkeypatch, state_container=0) == ("Deleted", None)


def test_unreadable_container_is_not_reported_deleted(monkeypatch):
    """An unreadable container is unknown, never a definite Deleted.

    If the DWARF spelling of the container member is wrong (e.g. the layout
    probes pxContainer while the build emits pvContainer), reads return None.
    Rendering that as Deleted would label every non-running task as deleted;
    it must instead fall through to the default state instead of inventing a
    confident wrong answer.
    """
    # container=None models the field being absent/unreadable from the value.
    assert _task_state(monkeypatch, state_container=None) == ("Ready", None)


# --- suspended-list three-way split ------------------------------------------


def test_suspended_with_event_container_is_blocked(monkeypatch):
    """On xSuspendedTaskList with an event item => Blocked (IPC wait)."""
    assert _task_state(
        monkeypatch, state_container=SUSPENDED, event_container=0x10
    ) == ("Blocked", None)


def test_suspended_no_event_notify_waiting_is_blocked(monkeypatch):
    """Suspended with a pending notification wait is Blocked, not Suspended."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=_Slots([0, 1]),
        notification_array=True,
    ) == ("Blocked", None)


def test_suspended_scalar_notify_waiting_is_blocked(monkeypatch):
    """Pre-V10.4.0 kernels keep ucNotifyState as a scalar member.

    The B-L475E-IOT01A V10.3.1 fixture is exactly this shape, so subscripting
    the member would raise and silently downgrade Blocked to Suspended.
    """
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=1,
    ) == ("Blocked", None)


def test_suspended_scalar_notify_idle_is_suspended(monkeypatch):
    """A scalar ucNotifyState of NOT_WAITING leaves the task Suspended."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=0,
    ) == ("Suspended", None)


def test_suspended_no_event_no_notify_is_suspended(monkeypatch):
    """Fully suspended: no event item and nothing waiting on a notification."""
    assert _task_state(
        monkeypatch,
        state_container=SUSPENDED,
        event_container=None,
        notify_state=_Slots([0, 0]),
        notification_array=True,
    ) == ("Suspended", None)


# --- SMP run state -----------------------------------------------------------


def test_smp_run_state_0_is_running(monkeypatch):
    """A valid core index in xTaskRunState means Running on that core."""
    assert _task_state(monkeypatch, smp=True, run_state=0) == ("Running", 0)


def test_smp_run_state_minus2_is_running_yielding(monkeypatch):
    """xTaskRunState == -2 (scheduled to yield) still occupies core 1."""
    tcbs = _Array([_Ptr(_Target(0x100)), _Ptr(_Target(CURRENT_TCB))])
    assert _task_state(monkeypatch, smp=True, run_state=-2, current_tcbs=tcbs) == (
        "Running(yielding)",
        1,
    )


def test_smp_run_state_minus1_on_ready_list_is_ready(monkeypatch):
    """xTaskRunState == -1 with no other match falls through to Ready."""
    assert _task_state(monkeypatch, smp=True, run_state=-1, state_container=0x777) == (
        "Ready",
        None,
    )


def test_smp_run_state_minus2_without_core_falls_through_to_blocked(monkeypatch):
    """-2 with no pxCurrentTCBs match must not invent Ready.

    The task still occupies a delayed list, so Step 3 reports Blocked.
    """
    assert _task_state(
        monkeypatch,
        smp=True,
        run_state=-2,
        current_tcbs=_Array([]),
        state_container=DELAYED,
    ) == ("Blocked", None)


def test_smp_run_state_minus2_without_core_defaults_to_ready(monkeypatch):
    """-2 with no core and no other list match falls through to Ready."""
    assert _task_state(
        monkeypatch,
        smp=True,
        run_state=-2,
        current_tcbs=_Array([]),
        state_container=0x777,
    ) == ("Ready", None)


# --- core_of back-reference --------------------------------------------------


def test_core_of_yielding_task_scans_px_current_tcbs(monkeypatch):
    """core_of() resolves a -2 run state against the per-core pointers."""
    tcbs = _Array([_Ptr(_Target(0x100)), _Ptr(_Target(CURRENT_TCB))])
    config = FreeRtosConfig(
        smp=True,
        number_of_cores=2,
        tcb_fields=frozenset({"xTaskRunState"}),
    )
    layout = build_layout(config, (10, 5, 1))
    tcb = _Target(CURRENT_TCB)

    def lookup_symbol(name):
        if name == "pxCurrentTCBs":
            return tcbs
        return None

    monkeypatch.setattr(navigation, "lookup_symbol", lookup_symbol)
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda value, _sl, field: (
            (-2 if field == "run_state" else None) if value is tcb else None
        ),
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.core_of(tcb, layout) == 1


def test_core_of_returns_none_outside_smp():
    """Single-core targets never report a core index."""
    config = FreeRtosConfig(number_of_cores=1)
    layout = build_layout(config, (10, 5, 1))
    assert navigation.core_of(_Target(CURRENT_TCB), layout) is None
