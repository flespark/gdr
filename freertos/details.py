"""Vertical detail builders for `frt task <name>` and later object kinds."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from gdr.formatting import format_address, format_optional_int
from gdr.gdb_bridge import lookup_symbol, read_int

if TYPE_CHECKING:
    from freertos.adapter import FreeRtosTask

# ucNotifyState values (tasks.c). Used to label notification slots in detail.
_NOTIFY_STATE_NAMES = {
    0: "not-waiting",
    1: "waiting",
    2: "received",
}


def _notify_state_name(state: int) -> str:
    return _NOTIFY_STATE_NAMES.get(state, str(state))


def _sum_runtime_counter(value, layout: FreeRtosLayout) -> int | None:
    """Sum ``ulTotalRunTime``, which is a scalar on V10 and an array on V11.

    Reason: V11 declares ``ulTotalRunTime[configNUMBER_OF_CORES]`` on every
    build, including uniprocessor (``[1]``). ``int()`` on an array gdb.Value
    raises TypeError, so a scalar-only read would silently hide Runtime%.
    """
    try:
        typ = value.type.strip_typedefs()
        if gdb is not None and typ.code == gdb.TYPE_CODE_ARRAY:
            total = 0
            any_value = False
            for index in range(max(layout.config.number_of_cores, 1)):
                item = read_int(value[index])
                if item is not None:
                    any_value = True
                    total += item
            return total if any_value else None
    except (TypeError, ValueError, AttributeError, IndexError):
        pass
    return read_int(value)


def _runtime_percent(task: FreeRtosTask, layout: FreeRtosLayout) -> str | None:
    """Return the task's share of total runtime, or ``None`` when unknown."""
    if task.runtime_counter is None:
        return None
    total_sym = lookup_symbol("ulTotalRunTime")
    if total_sym is None:
        return None
    total = _sum_runtime_counter(total_sym, layout)
    if total is None or total <= 0:
        return None
    return f"{100.0 * task.runtime_counter / total:.1f}%"


def task_detail(task: FreeRtosTask, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build vertical ``(key, value)`` pairs for ``frt task <name>``.

    Key order is a stable output contract; config-conditional fields appear
    only when the corresponding TCB member exists in DWARF. ``WakeTick`` is
    only meaningful while blocked, and ``BlockedOn`` is still a placeholder
    (resolving the waiter host needs the object discovery channels).
    """
    tcb_layout = layout.structs["struct tskTaskControlBlock"]
    fields = tcb_layout.fields
    pairs: list[tuple[str, str]] = [
        ("Name", task.name),
        ("Address", format_address(task.address)),
        ("Type", "Idle" if task.is_idle else "Normal"),
        ("State", task.state),
        ("Priority", str(task.current_priority)),
    ]
    if "base_priority" in fields:
        pairs.append(("BasePriority", format_optional_int(task.base_priority)))
    pairs.append(("SP", format_address(task.top_of_stack)))
    pairs.append(("Stack", format_address(task.stack_base)))
    pairs.append(("StackSize", format_optional_int(task.stack_size)))
    pairs.append(("Used", format_optional_int(task.stack_used)))
    if task.stack_size is not None or task.high_water_mark is not None:
        # Reason: HighWater does not depend on the stack size -- the default
        # build has no pxEndOfStack, so StackSize/Used stay N/A while the 0xa5
        # watermark window is still scannable. Gating on stack_size alone hid
        # a value that ``frt tasks`` shows, making the two commands disagree.
        pairs.append(
            (
                "HighWater",
                str(task.high_water_mark)
                if task.high_water_mark is not None
                else "unavailable",
            )
        )
    if "mutexes_held" in fields:
        pairs.append(("MutexesHeld", format_optional_int(task.mutexes_held)))
    if task.state == "Blocked":
        pairs.append(("WakeTick", format_optional_int(task.wake_tick)))
    pairs.append(("BlockedOn", task.blocked_on or "N/A"))
    if task.notify is not None:
        for index, (value, state) in enumerate(task.notify):
            pairs.append((f"Notify[{index}]", f"{value}/{_notify_state_name(state)}"))
    if "runtime_counter" in fields:
        pairs.append(("Runtime", format_optional_int(task.runtime_counter)))
        percent = _runtime_percent(task, layout)
        if percent is not None:
            pairs.append(("Runtime%", percent))
    if "core_affinity" in fields:
        pairs.append(("CoreAffinity", format_optional_int(task.core_affinity)))
    if "run_state" in fields:
        pairs.append(("RunState", format_optional_int(task.run_state)))
    if "preemption_disable" in fields:
        pairs.append(
            ("PreemptionDisable", format_optional_int(task.preemption_disable))
        )
    if "critical_nesting" in fields:
        pairs.append(("CriticalNesting", format_optional_int(task.critical_nesting)))
    if "errno" in fields:
        pairs.append(("Errno", format_optional_int(task.errno)))
    if "delay_aborted" in fields:
        pairs.append(("DelayAborted", format_optional_int(task.delay_aborted)))
    if "statically_allocated" in fields:
        pairs.append(
            ("StaticallyAllocated", format_optional_int(task.statically_allocated))
        )
    if "tls" in fields:
        pairs.append(("TLS", "present" if task.tls_present else "absent"))
    return pairs
