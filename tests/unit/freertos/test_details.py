"""Unit tests for the `frt task <name>` vertical detail contract."""

from __future__ import annotations

import freertos.details as details_module
from freertos.adapter import FreeRtosTask
from freertos.layout import FreeRtosConfig, build_layout

# Documented ``frt task <name>`` detail key order. Conditional keys appear only
# when the matching TCB member exists in DWARF.
_FULL_ORDER = [
    "Name",
    "Address",
    "Type",
    "State",
    "Priority",
    "BasePriority",
    "SP",
    "Stack",
    "StackSize",
    "Used",
    "HighWater",
    "MutexesHeld",
    "WakeTick",
    "BlockedOn",
    "Notify[0]",
    "Runtime",
    "CoreAffinity",
    "RunState",
    "PreemptionDisable",
    "CriticalNesting",
    "Errno",
    "DelayAborted",
    "StaticallyAllocated",
    "TLS",
]


def _full_layout():
    return build_layout(
        FreeRtosConfig(
            smp=True,
            number_of_cores=2,
            notifications=True,
            tls_field="xTLSBlock",
            stack_end_field="pxEndOfStack",
            task_attributes=True,
            preemption_disable=True,
            critical_nesting_in_tcb=True,
            tcb_fields=frozenset(
                {
                    "uxBasePriority",
                    "uxMutexesHeld",
                    "ulRunTimeCounter",
                    "uxCoreAffinityMask",
                    "xTaskRunState",
                    "ucStaticallyAllocated",
                    "ucDelayAborted",
                    "iTaskErrno",
                    "xPreemptionDisable",
                    "uxCriticalNesting",
                }
            ),
        ),
        (11, 1, 0),
    )


def _full_task():
    return FreeRtosTask(
        name="worker",
        address=0x2000,
        state="Blocked",
        current_priority=4,
        base_priority=3,
        top_of_stack=0x1180,
        stack_base=0x1000,
        stack_end=0x1200,
        stack_size=0x200,
        stack_used=0x80,
        high_water_mark=12,
        runtime_counter=500,
        core=None,
        core_affinity=3,
        mutexes_held=1,
        wake_tick=4242,
        notify=[(7, 1)],
        run_state=-1,
        preemption_disable=0,
        critical_nesting=0,
        errno=0,
        delay_aborted=0,
        statically_allocated=1,
        tls_present=True,
    )


def test_task_detail_follows_the_documented_key_order(monkeypatch):
    """Every available key appears exactly once, in the documented order."""
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: None)

    pairs = details_module.task_detail(_full_task(), _full_layout())

    assert [key for key, _value in pairs] == _FULL_ORDER


def test_task_detail_reports_runtime_percent_against_total(monkeypatch):
    """Runtime% follows Runtime and divides by ulTotalRunTime."""
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: 2000)
    monkeypatch.setattr(details_module, "read_int", lambda value: value)

    pairs = dict(details_module.task_detail(_full_task(), _full_layout()))
    keys = [
        key for key, _value in details_module.task_detail(_full_task(), _full_layout())
    ]

    assert pairs["Runtime%"] == "25.0%"
    assert keys.index("Runtime%") == keys.index("Runtime") + 1


def test_runtime_percent_sums_v11_array_total(monkeypatch):
    """V11 ulTotalRunTime is an array even on a uniprocessor build."""
    array_code = object()

    class _FakeType:
        code = array_code

        def strip_typedefs(self):
            return self

    class _Array:
        def __init__(self, values):
            self._values = values
            self.type = _FakeType()

        def __getitem__(self, index):
            return self._values[index]

    fake = _Array([400, 1600])
    monkeypatch.setattr(
        details_module, "gdb", type("G", (), {"TYPE_CODE_ARRAY": array_code})
    )
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: fake)
    monkeypatch.setattr(details_module, "read_int", lambda value: value)

    percent = details_module._runtime_percent(_full_task(), _full_layout())

    assert percent == "25.0%"


def test_task_detail_omits_absent_members_and_never_shows_entry():
    """A minimal V10.3 config yields only unconditional keys."""
    layout = build_layout(FreeRtosConfig(notifications=False), (10, 3, 0))
    task = FreeRtosTask(name="idle", address=0x10, state="Running", is_idle=True)

    pairs = details_module.task_detail(task, layout)

    assert [key for key, _value in pairs] == [
        "Name",
        "Address",
        "Type",
        "State",
        "Priority",
        "SP",
        "Stack",
        "StackSize",
        "Used",
        "BlockedOn",
    ]
    assert dict(pairs)["Type"] == "Idle"
    assert "Entry" not in dict(pairs)


def test_task_detail_wake_tick_only_when_blocked(monkeypatch):
    """WakeTick is meaningful only while the task sits on a delayed list."""
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: None)
    layout = _full_layout()
    blocked = _full_task()
    ready = _full_task()
    ready.state = "Ready"

    assert "WakeTick" in dict(details_module.task_detail(blocked, layout))
    assert "WakeTick" not in dict(details_module.task_detail(ready, layout))


def test_task_detail_renders_unfilled_high_water_as_unavailable(monkeypatch):
    """A readable stack that was never watermarked reports unavailable."""
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: None)
    task = _full_task()
    task.high_water_mark = None

    pairs = dict(details_module.task_detail(task, _full_layout()))

    assert pairs["HighWater"] == "N/A"


def test_task_detail_reports_unreadable_optional_members_as_na(monkeypatch):
    """A present-but-unreadable member is N/A, never the string 'None'."""
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: None)
    task = _full_task()
    task.mutexes_held = None
    task.errno = None
    task.statically_allocated = None

    pairs = dict(details_module.task_detail(task, _full_layout()))

    assert pairs["MutexesHeld"] == "N/A"
    assert pairs["Errno"] == "N/A"
    assert pairs["StaticallyAllocated"] == "N/A"
    assert "None" not in pairs.values()


def test_task_detail_shows_high_water_without_a_known_stack_size(monkeypatch):
    """HighWater must appear whenever it was computed, size known or not.

    The default build has no pxEndOfStack, so StackSize/Used stay N/A while the
    watermark window is still scannable. ``frt tasks`` shows the HighWater
    column in exactly that configuration, so the detail view must not hide it.
    """
    monkeypatch.setattr(details_module, "lookup_symbol", lambda _name: None)
    task = _full_task()
    task.stack_size = None
    task.stack_used = None
    task.high_water_mark = 105

    pairs = dict(details_module.task_detail(task, _full_layout()))

    assert pairs["StackSize"] == "N/A"
    assert pairs["HighWater"] == "105"
