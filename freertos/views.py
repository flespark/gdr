"""FreeRTOS table contracts, vertical details and merged detail tables.

Column shapes, per-kind detail builders and the typed merged tables share one
module so a table and the columns it advertises cannot disagree; object
decode lives in :mod:`freertos.objects`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from core.adapter import Cell, ColumnSpec, merged_detail_table, symbol_cell
from core.formatting import (
    format_address,
    format_optional_bytes,
    format_optional_int,
    waiter_cell,
)
from core.gdb_bridge import (
    lookup_symbol,
    lookup_symbol_at,
    read_int,
    source_definition_at,
    source_location_at,
)
from core.layout import read_field
from freertos.layout import FreeRtosLayout, queue_type_label
from freertos.navigation import (
    iter_queue_items,
    source_label,
    system_value,
    task_priority_at,
)

if TYPE_CHECKING:
    from freertos.adapter import FreeRtosTask, FreeRtosTimerObject
    from freertos.objects import (
        FreeRtosEventGroupObject,
        FreeRtosStreamBufferObject,
    )


def task_column_flags(layout: FreeRtosLayout) -> dict[str, bool]:
    """Which optional task columns this build can populate.

    ``high_water`` is deliberately absent: it depends on the runtime data (a
    stack actually carrying the watermark), so the table decides it per
    snapshot while the capability claim describes the gating config.
    """
    fields = layout.structs["struct tskTaskControlBlock"].fields
    return {
        "base_priority": "base_priority" in fields,
        "stack": "stack_end" in fields,
        "runtime": "runtime_counter" in fields,
        "smp": layout.config.smp,
        "affinity": "core_affinity" in fields,
    }


def task_specs(
    *,
    base_priority: bool,
    stack: bool,
    high_water: bool,
    runtime: bool,
    smp: bool,
    affinity: bool,
) -> tuple[ColumnSpec, ...]:
    """The task table contract; optional columns follow the same order."""
    specs = [
        ColumnSpec("Name", elastic=True, doc="task name; '*' marks the running one"),
        ColumnSpec("State", doc="scheduler state derived from the list it is on"),
        ColumnSpec("Prio", kind="int", align="right"),
    ]
    if base_priority:
        specs.append(ColumnSpec("BasePrio", kind="int", align="right"))
    specs.append(
        ColumnSpec(
            "PC",
            kind="pc",
            doc="last PC: the return address on the saved switch frame; N/A when unproven",
        )
    )
    specs.append(ColumnSpec("SP", kind="addr", doc="top of stack"))
    if stack:
        specs += [
            ColumnSpec("Stack", kind="int", align="right", doc="stack size in bytes"),
            ColumnSpec("Used", kind="int", align="right", doc="stack bytes in use"),
        ]
    if high_water:
        specs.append(
            ColumnSpec(
                "HighWater",
                kind="int",
                align="right",
                doc="historical maximum stack bytes used, from fill watermark; includes initial context writes",
            )
        )
    if runtime:
        specs.append(ColumnSpec("Runtime", kind="int", align="right"))
    if smp:
        specs.append(ColumnSpec("CPU", kind="int", align="right"))
    if affinity:
        specs.append(ColumnSpec("Affinity", kind="hex"))
    specs.append(ColumnSpec("Addr", kind="addr", doc="task control block address"))
    return tuple(specs)


SUMMARY_SPECS = (
    ColumnSpec("Kind", doc="semantic object kind"),
    ColumnSpec("Count", kind="int", align="right"),
    ColumnSpec("Sources", elastic=True, doc="discovery channels that found it"),
)

QUEUE_SPECS_HEAD = (
    ColumnSpec("Name", elastic=True, doc="queue name from the registry"),
    ColumnSpec("Type", kind="enum", doc="ucQueueType, or inferred from pcHead"),
    ColumnSpec("Items", kind="int", align="right", doc="queued items"),
    ColumnSpec("Length", kind="int", align="right", doc="capacity"),
    ColumnSpec("ItemSize", kind="int", align="right", doc="bytes per item"),
    ColumnSpec("Free", kind="int", align="right"),
    ColumnSpec("SendWait", kind="waiters", elastic=True),
    ColumnSpec("RecvWait", kind="waiters", elastic=True),
    ColumnSpec("Locks", doc="rx/tx lock counters from a critical section"),
)

QUEUE_SPECS_TAIL = (
    ColumnSpec("Src", doc="discovery channels"),
    ColumnSpec("Addr", kind="addr"),
)


def queue_specs(queue_sets: bool) -> tuple[ColumnSpec, ...]:
    """The queue table contract; ``Set`` exists only with queue sets."""
    specs = list(QUEUE_SPECS_HEAD)
    if queue_sets:
        specs.append(
            ColumnSpec("Set", kind="addr", doc="owning queue set, '-' when none")
        )
    return (*specs, *QUEUE_SPECS_TAIL)


SEMAPHORE_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", kind="enum"),
    ColumnSpec("Count", kind="int", align="right", doc="available count"),
    ColumnSpec("Max", kind="int", align="right", doc="upper bound"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

MUTEX_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", kind="enum"),
    ColumnSpec("Held", kind="enum", doc="'yes' when taken; value is the count"),
    ColumnSpec("Owner", kind="symbol", doc="holder task address, '-' when free"),
    ColumnSpec("Recursive", kind="int", align="right", doc="nested take count"),
    ColumnSpec("Waiters", kind="waiters", elastic=True),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

TIMER_SPECS = (
    ColumnSpec("Name", elastic=True, doc="pcTimerName"),
    ColumnSpec("State", doc="list-derived state; '?' marks a transition"),
    ColumnSpec("Mode", doc="auto-reload vs one-shot"),
    ColumnSpec("Period", kind="int", align="right", doc="ticks"),
    ColumnSpec("Expiry", kind="int", align="right", doc="absolute tick"),
    ColumnSpec("ExpiresIn", kind="int", align="right", doc="wrap-safe ticks left"),
    ColumnSpec("Callback", kind="symbol", elastic=True),
    ColumnSpec("ID", kind="symbol", doc="pvTimerID, '-' when NULL"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

EVENT_GROUP_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Bits", kind="hex", doc="current event bits"),
    ColumnSpec("Waiters", kind="int", align="right"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)

HEAP_LINEAR_SPECS = (
    ColumnSpec("Address", kind="addr"),
    ColumnSpec("Size", kind="int", align="right", doc="block size in bytes"),
    ColumnSpec("State", kind="enum", doc="allocated vs free"),
)

HEAP_FREE_LIST_SPECS = (
    ColumnSpec("Address", kind="addr"),
    ColumnSpec("Size", kind="int", align="right", doc="block size in bytes"),
    ColumnSpec("Next", kind="addr", elastic=True, doc="next free block, '-' at end"),
)


STREAM_BUFFER_SPECS = (
    ColumnSpec("Name", elastic=True),
    ColumnSpec("Type", doc="byte stream vs message buffer"),
    ColumnSpec("Bytes", kind="int", align="right", doc="bytes stored"),
    ColumnSpec("Space", kind="int", align="right", doc="free bytes"),
    ColumnSpec("Capacity", kind="int", align="right", doc="buffer size in bytes"),
    ColumnSpec("Trigger", kind="int", align="right", doc="trigger level in bytes"),
    ColumnSpec("NextMsg", kind="hex", doc="message length prefix, 'N/A' for streams"),
    ColumnSpec("RecvWait", doc="single receiving task handle"),
    ColumnSpec("SendWait", doc="single sending task handle"),
    ColumnSpec("Src"),
    ColumnSpec("Addr", kind="addr"),
)


# ---------------------------------------------------------------------------
# Vertical detail builders.
# ---------------------------------------------------------------------------
try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]


if TYPE_CHECKING:
    from freertos.adapter import FreeRtosTask, FreeRtosTimerObject
    from freertos.objects import (
        FreeRtosEventGroupObject,
        FreeRtosStreamBufferObject,
    )

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

    V11 declares ``ulTotalRunTime[configNUMBER_OF_CORES]`` on every build,
    including uniprocessor (``[1]``); both shapes are summed so Runtime%
    is never silently hidden.
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
    total_sym = lookup_symbol(layout.symbols["total_runtime"])
    if total_sym is None:
        return None
    total = _sum_runtime_counter(total_sym, layout)
    if total is None or total <= 0:
        return None
    return f"{100.0 * task.runtime_counter / total:.1f}%"


def task_detail(
    task: FreeRtosTask,
    layout: FreeRtosLayout,
    tcb_value=None,
) -> list[tuple[str, str]]:
    """Build vertical ``(key, value)`` pairs for ``frt task <name>``.

    Key order is a stable output contract; config-conditional fields appear
    only when the corresponding TCB member exists in DWARF. ``WakeTick`` is
    only meaningful while blocked, and ``BlockedOn`` is still a placeholder
    (resolving the waiter host needs the object discovery channels).  The
    consistency checks (``Checks:`` rows) need the raw TCB value; without it
    (model-only callers) the section is omitted.
    """
    from freertos.diagnostics import task_checks

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
    pairs.append(("StackSize", format_optional_bytes(task.stack_size)))
    pairs.append(("Used", format_optional_bytes(task.stack_used)))
    if task.stack_size is not None or task.high_water_mark is not None:
        # NOTE: keep an unavailable watermark visible when capacity is
        # known but stack prefill cannot be established.
        pairs.append(
            (
                "HighWater",
                format_optional_bytes(task.high_water_mark),
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
    if tcb_value is not None:
        pairs.extend(checks_pairs(task_checks(tcb_value, task.address, layout)))
    return pairs


# ---------------------------------------------------------------------------
# queue-family shared cells and detail builders
# ---------------------------------------------------------------------------


def waiter_summary(names: list[str] | None) -> str:
    """Render ``count@names`` with the count first so truncation keeps it.

    Mirrors the IPC waiter contract of the other adapter package: on a
    narrow terminal the elastic columns shrink from the right, so a
    names-first format would lose the diagnostics count exactly when it
    matters most.  ``None`` (unreadable) renders ``N/A``; an empty list
    renders ``0``.  The cell format lives in
    :func:`core.formatting.waiter_cell`; this name is kept as the adapter-facing
    seam its callers and unit tests use.
    """
    return waiter_cell(names)


def locks_cell(rx_lock: int | None, tx_lock: int | None) -> str:
    """Render a queue's lock counters: ``-`` when both are ``queueUNLOCKED``.

    ``queueUNLOCKED == (int8_t) -1`` (queue.c) and is normalized from an
    unsigned 255 read before it reaches here; any other value is a lock
    count from a queue locked inside a critical section.
    """
    if rx_lock is None or tx_lock is None:
        return "N/A"
    if rx_lock == -1 and tx_lock == -1:
        return "-"
    return f"rx={rx_lock} tx={tx_lock}"


def held_cell(obj) -> str:
    """Render the mutex ``Held`` cell: ``yes`` when taken, ``no`` when free.

    A mutex is *taken* when its message count is 0 (a take empties it, a
    give refills it, queue.c xQueueSemaphoreTake/give); the Owner column
    carries who.  The count is the objective state -- a take issued before
    the scheduler starts records no holder (pxCurrentTCB is NULL), so the
    holder pointer alone would mislabel a genuinely taken mutex as free.
    """
    if obj.count is None:
        return "N/A"
    return "yes" if obj.count == 0 else "no"


def checks_pairs(checks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Show actionable failures and incomplete reads, not success counters.

    Full ok/skipped coverage is available on demand through ``frt checks``.
    The verdict prefix is retained so machine clients can classify each
    Check[name] column instead of treating it as text.
    """
    return [
        (f"Check[{name}]", status)
        for name, status in checks
        if status.startswith("fail:")
        or any(
            word in status
            for word in (
                "unreadable",
                "bound reached",
                "unavailable",
                "corrupt",
                "truncated",
                "partial",
            )
        )
    ]


def queue_detail(obj, value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt queue <name>``.

    Key order is a stable output contract; ``Set`` appears only when the
    build has queue sets, and FIFO ``Item[i]`` storage addresses trail the checks.
    """
    from freertos.diagnostics import queue_checks

    ql = layout.structs["struct QueueDefinition"]
    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
        ("Type", queue_type_label(obj.kind, obj.type_code, obj.inferred_kind)),
        ("Items", format_optional_int(obj.count)),
        ("Length", format_optional_int(obj.length)),
        ("ItemSize", format_optional_int(obj.item_size)),
        ("Free", format_optional_int(obj.free)),
        ("Head", format_address(read_int(read_field(value, ql, "head")))),
        ("Tail", format_address(read_int(read_field(value, ql, "tail")))),
        ("WriteTo", format_address(read_int(read_field(value, ql, "write_to")))),
        (
            "ReadFrom",
            format_address(read_int(read_field(value, ql, "read_from"))),
        ),
        ("Locks", locks_cell(obj.rx_lock, obj.tx_lock)),
    ]
    if layout.config.queue_sets:
        pairs.append(
            ("Set", format_address(obj.set_container) if obj.set_container else "-")
        )
    pairs.append(("SendWait", waiter_summary(obj.send_waiters)))
    pairs.append(("RecvWait", waiter_summary(obj.recv_waiters)))
    pairs.append(("Src", source_label(obj.source, obj.extra_sources)))
    pairs.extend(checks_pairs(queue_checks(value, obj.kind, layout)))
    # NOTE: payload inspection belongs to GDB/the memory viewer. Timer
    # command decoding still requests bytes through the same ring iterator.
    for index, address, _payload in iter_queue_items(value, layout, max_payload=0):
        pairs.append((f"Item[{index}]", format_address(address)))
    return pairs


def semaphore_detail(obj, value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt semaphore <name>``.

    Deliberately reads no ``u.xSemaphore`` union arm: only mutexes
    (``pcHead == NULL``) decode that, and reading it on a semaphore -- whose
    ``u`` member is QueuePointers_t -- would fabricate a holder from
    pcTail/pcReadFrom bytes (queue.c).
    """
    from freertos.diagnostics import queue_checks

    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
        ("Type", queue_type_label(obj.kind, obj.type_code, obj.inferred_kind)),
        ("Count", format_optional_int(obj.count)),
        ("Max", format_optional_int(obj.length)),
        ("Waiters", waiter_summary(obj.recv_waiters)),
        ("Locks", locks_cell(obj.rx_lock, obj.tx_lock)),
        ("Src", source_label(obj.source, obj.extra_sources)),
    ]
    pairs.extend(checks_pairs(queue_checks(value, obj.kind, layout)))
    return pairs


def mutex_detail(obj, value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt mutex <name>``.

    Holder priority fields come from the holder TCB (``uxPriority``,
    ``uxBasePriority`` when present), gated on the same capability as the
    task table's BasePrio column.
    """
    from freertos.diagnostics import queue_checks

    sl = layout.structs["struct tskTaskControlBlock"]
    priority = task_priority_at(obj.holder_address, layout, "current_priority")
    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
        ("Type", queue_type_label(obj.kind, obj.type_code, obj.inferred_kind)),
        ("Held", held_cell(obj)),
        ("Owner", obj.holder or "-"),
        ("OwnerPriority", format_optional_int(priority)),
    ]
    if "base_priority" in sl.fields:
        base = task_priority_at(obj.holder_address, layout, "base_priority")
        pairs.append(("OwnerBasePriority", format_optional_int(base)))
    pairs.append(("RecursiveCallCount", format_optional_int(obj.recursive_count)))
    pairs.append(("Waiters", waiter_summary(obj.recv_waiters)))
    pairs.append(("Locks", locks_cell(obj.rx_lock, obj.tx_lock)))
    pairs.append(("Src", source_label(obj.source, obj.extra_sources)))
    pairs.extend(checks_pairs(queue_checks(value, obj.kind, layout)))
    return pairs


# ---------------------------------------------------------------------------
# timer detail builder
# ---------------------------------------------------------------------------


def timer_detail(
    obj: FreeRtosTimerObject,
    value,
    layout: FreeRtosLayout,
) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt timer <name>``.

    ``List`` resolves the epoch the item sits on (``current``/``overflow``
    with the underlying list symbol) and ``OwnerCheck`` verifies
    ``pvOwner`` only when the item is actually linked; a dormant timer
    renders ``N/A`` expiry cells so a stale list-item value is never read
    as a live deadline.  The pending-command section trails the checks.
    """
    from freertos.diagnostics import timer_checks
    from freertos.objects import (
        DAEMON_CURRENT_MESSAGE,
        callback_cell,
        commands_cell,
        daemon_is_current,
        id_cell,
        iter_timer_commands,
        mode_cell,
        owner_check,
        state_cell,
        timer_epoch,
        timer_expires_in,
        timer_is_on_lists,
        timer_list_label,
    )

    dormant = not timer_is_on_lists(obj.source, obj.extra_sources)
    tick = system_value("tick", layout)
    mask = (1 << layout.config.tick_bits) - 1
    in_overflow = timer_epoch(obj.container) == "overflow"
    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
        ("State", state_cell(obj.source, obj.status, obj.extra_sources)),
        ("Mode", mode_cell(obj.status)),
        ("Period", format_optional_int(obj.period)),
        ("Expiry", "N/A" if dormant else format_optional_int(obj.expiry)),
        (
            "ExpiresIn",
            "N/A" if dormant else timer_expires_in(obj.expiry, tick, mask, in_overflow),
        ),
        ("Callback", callback_cell(obj.callback)),
        ("ID", id_cell(obj.id)),
        ("List", timer_list_label(obj.container)),
        ("OwnerCheck", owner_check(obj.container, obj.owner, obj.address)),
        ("Src", source_label(obj.source, obj.extra_sources)),
    ]
    pairs.extend(checks_pairs(timer_checks(value, layout)))
    messages, commands = iter_timer_commands(layout)
    cell = commands_cell(messages, commands, layout)
    if daemon_is_current(layout):
        # NOTE: the daemon is the only writer of the queue, so while it is
        # on a core the section is an intermediate state; the warning leads
        # the section instead of replacing a decodable command table.
        cell = f"{DAEMON_CURRENT_MESSAGE}\n{cell}"
    pairs.append(("Commands", cell))
    return pairs


# ---------------------------------------------------------------------------
# event group / stream buffer detail builders
# ---------------------------------------------------------------------------


def event_group_detail(
    obj: FreeRtosEventGroupObject,
    value,
    layout: FreeRtosLayout,
) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt eventgroup <name>``.

    Every waiter renders its own ``wants/mode/clearOnExit/missing`` line; a
    waiter whose wanted bits are already set but is still on the list is the
    transient state where ``xEventGroupSetBits`` has not run yet, marked
    ``(satisfied — mid-unblock)`` instead of silently reporting "blocked".
    The ``EventWaiterSatisfied`` consistency check surfaces the same
    transient in the ``Checks:`` section.
    """
    from freertos.diagnostics import event_checks
    from freertos.objects import bits_cell, format_waiter_line

    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
        ("Bits", bits_cell(obj.bits)),
        ("Waiters", str(len(obj.waiters))),
    ]
    if obj.statically_allocated is not None:
        pairs.append(
            ("StaticallyAllocated", "yes" if obj.statically_allocated else "no")
        )
    for index, waiter in enumerate(obj.waiters):
        prefix = f"{waiter.task} " if waiter.task and waiter.task != "-" else ""
        line = prefix + format_waiter_line(waiter)
        pairs.append((f"Waiter[{index}]", line))
    pairs.append(("Src", source_label(obj.source, obj.extra_sources)))
    pairs.extend(checks_pairs(event_checks(value, layout)))
    return pairs


def stream_buffer_detail(
    obj: FreeRtosStreamBufferObject,
    value,
    layout: FreeRtosLayout,
) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt streambuffer <name>``.

    A deleted buffer (``xLength == 0 && pucBuffer == NULL``) short-circuits:
    computing bytes/space/capacity would divide by the zero length, so the
    detail explains the deletion instead of fabricating geometry.  ``NextMsg``
    is N/A for non-message buffers (a bare 0 would read as a real 0-length
    message), ``MsgLenBytes`` declares whether the length-prefix width is an
    assumed ``size_t`` fallback, and ``NotificationIndex`` states the kernel
    version gate instead of silently dropping the key.
    """
    from freertos.objects import bounds_check, stream_label

    pairs: list[tuple[str, str]] = [
        ("Name", obj.name),
        ("Address", format_address(obj.address)),
    ]
    if obj.deleted:
        pairs.append(("Type", "deleted"))
        pairs.append(
            (
                "Note",
                "xLength==0 and pucBuffer==NULL: vStreamBufferDeleteStatic "
                "memset the buffer; bytes/space/capacity are not computed",
            )
        )
        pairs.append(("Src", source_label(obj.source, obj.extra_sources)))
        return pairs
    pairs.append(("Type", stream_label(obj.flags)))
    pairs.append(("Capacity", format_optional_int(obj.capacity)))
    pairs.append(("Bytes", format_optional_int(obj.bytes_used)))
    pairs.append(("Space", format_optional_int(obj.space)))
    pairs.append(("Trigger", format_optional_int(obj.trigger)))
    if obj.trigger_met is None:
        pairs.append(("TriggerMet", "N/A"))
    else:
        pairs.append(("TriggerMet", "yes" if obj.trigger_met else "no"))
    if obj.kind == "message":
        pairs.append(
            (
                "NextMsg",
                "-" if obj.next_message is None else f"0x{obj.next_message:x}",
            )
        )
    else:
        pairs.append(("NextMsg", "N/A"))
    assumed = " (assumed size_t)" if obj.message_length_assumed else ""
    pairs.append(("MsgLenBytes", f"{obj.message_length_bytes}{assumed}"))
    pairs.append(("RecvWait", obj.recv_waiter or "-"))
    pairs.append(("SendWait", obj.send_waiter or "-"))
    if layout.config.stream_buffer_notification_index:
        pairs.append(
            (
                "NotificationIndex",
                "-" if obj.notification_index is None else str(obj.notification_index),
            )
        )
    else:
        pairs.append(("NotificationIndex", "N/A (kernel < 11.1.0)"))
    pairs.append(("BoundsCheck", bounds_check(obj, value, layout)))
    pairs.append(("Src", source_label(obj.source, obj.extra_sources)))
    return pairs


# ---------------------------------------------------------------------------
# Merged detail tables.
# ---------------------------------------------------------------------------
DETAIL_SPECS = (
    ColumnSpec("Callback", kind="symbol"),
    ColumnSpec("ID", kind="symbol"),
    ColumnSpec("Waiters", kind="waiters"),
    ColumnSpec("RecvWait", kind="waiters"),
    ColumnSpec("SendWait", kind="waiters"),
    ColumnSpec("Item", kind="addresses"),
    *(
        ColumnSpec(key, kind="symbol")
        for key in ("Head", "Tail", "WriteTo", "ReadFrom", "Set")
    ),
    ColumnSpec("Owner", kind="symbol"),
)


def waiter_records(names, adapter, cache):
    """Typed waiter navigation: proven saved PC/source, otherwise the TCB.

    FreeRTOS retains no task entry field. Never infer one from a task name;
    duplicate names stay non-actionable instead of selecting the wrong task.
    """
    from freertos.adapter import iter_converted_tasks

    if names is None:
        return None
    if names and not cache:
        for task in iter_converted_tasks(adapter.layout):
            cache.setdefault(task.name, []).append(task)
    records = []
    for name in names:
        tasks = cache.get(name, [])
        task = tasks[0] if len(tasks) == 1 else None
        pc = adapter._pc_cell(task) if task is not None else None
        source = pc.source if pc is not None else None
        records.append(
            {
                "name": name,
                "address": pc.value
                if pc is not None and pc.value
                else task.address
                if task
                else None,
                "source": {
                    "file": source.fullname,
                    "name": source.name,
                    "line": source.line,
                }
                if source
                else None,
            }
        )
    return records


def detail_table(adapter, kind: str):
    """Build one table from the same bounded detail builders used by the CLI."""
    from freertos.adapter import (
        _cast_object,
        value_to_queue_object,
        value_to_timer_object,
    )
    from freertos.navigation import discover
    from freertos.objects import (
        value_to_event_group_object,
        value_to_stream_buffer_object,
    )

    routes = {
        "timer": (value_to_timer_object, timer_detail),
        "queue": (value_to_queue_object, queue_detail),
        "semaphore": (value_to_queue_object, semaphore_detail),
        "mutex": (value_to_queue_object, mutex_detail),
        "eventgroup": (value_to_event_group_object, event_group_detail),
        "streambuffer": (value_to_stream_buffer_object, stream_buffer_detail),
    }
    if kind not in routes:
        return None
    converter, builder = routes[kind]
    rows = []
    overrides = []
    waiter_cache = {}
    for found in discover(kind, adapter.layout):
        value = _cast_object(found.address, kind, adapter.layout)
        obj = converter(value, found, adapter.layout)
        pairs = builder(obj, value, adapter.layout)
        cells: dict[str, Cell] = {}
        if kind == "queue":
            ql = adapter.layout.structs["struct QueueDefinition"]
            for key, field in (
                ("Head", "head"),
                ("Tail", "tail"),
                ("WriteTo", "write_to"),
                ("ReadFrom", "read_from"),
                ("Set", "set_container"),
            ):
                if field not in ql.fields:
                    continue
                address = read_int(read_field(value, ql, field))
                cells[key] = (
                    symbol_cell(address, lookup_symbol_at(address))
                    if address
                    else Cell("-" if address == 0 else "N/A", None)
                )
        elif kind == "mutex" and obj.holder_address:
            cells["Owner"] = Cell(obj.holder or "-", obj.holder_address)
        if kind == "timer":
            for key, address in (("Callback", obj.callback), ("ID", obj.id)):
                if address:
                    cells[key] = symbol_cell(
                        address,
                        lookup_symbol_at(address),
                        source_location_at(address)
                        if key == "Callback"
                        else source_definition_at(address),
                    )
        elif kind == "eventgroup":
            names = [waiter.task for waiter in obj.waiters]
            cells["Waiters"] = Cell(
                str(len(names)), waiter_records(names, adapter, waiter_cache)
            )
        elif kind == "streambuffer":
            for key, name in (
                ("RecvWait", obj.recv_waiter),
                ("SendWait", obj.send_waiter),
            ):
                names = [name] if name and name != "-" else []
                cells[key] = Cell(
                    name or "-", waiter_records(names, adapter, waiter_cache)
                )
        else:
            channels = (
                (("SendWait", obj.send_waiters), ("RecvWait", obj.recv_waiters))
                if kind == "queue"
                else (("Waiters", obj.recv_waiters),)
            )
            for key, names in channels:
                cells[key] = Cell(
                    waiter_summary(names), waiter_records(names, adapter, waiter_cache)
                )
            items = []
            for key, text in pairs:
                if key.startswith("Item["):
                    address_text = text
                    try:
                        address = int(address_text, 16)
                    except ValueError:
                        address = None
                    items.append({"text": f"{key}: {address_text}", "address": address})
            if items:
                cells["Item"] = Cell("; ".join(item["text"] for item in items), items)
        # NOTE: queue/semaphore/mutex subtypes still carry diagnostic value;
        # unlike RT-Thread's class label, do not erase them from the table.
        rows.append(
            [("Subtype" if key == "Type" else key, text) for key, text in pairs]
        )
        overrides.append(cells)
    return merged_detail_table(
        kind, rows, cell_overrides=overrides, column_specs=DETAIL_SPECS
    )
