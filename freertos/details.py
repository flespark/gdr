"""Vertical detail builders for `frt task <name>` and later object kinds."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.diagnostics import event_checks, queue_checks, task_checks, timer_checks
from freertos.events import bits_cell, format_waiter_line
from freertos.layout import FreeRtosLayout, queue_type_label
from freertos.navigation import (
    iter_queue_items,
    source_label,
    system_value,
    task_priority_at,
)
from freertos.streams import bounds_check, stream_label
from freertos.timers import (
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
from gdr.derive import waiter_cell
from gdr.formatting import format_address, format_optional_int
from gdr.gdb_bridge import (
    lookup_symbol,
    read_int,
)
from gdr.layout import read_field

if TYPE_CHECKING:
    from freertos.adapter import FreeRtosTask, FreeRtosTimerObject
    from freertos.events import FreeRtosEventGroupObject
    from freertos.streams import FreeRtosStreamBufferObject

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
                else "N/A",
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

# Item payloads longer than this are truncated in the Item[i] dump.
GDR_QUEUE_ITEM_DUMP_BYTES = 64


def waiter_summary(names: list[str] | None) -> str:
    """Render ``count@names`` with the count first so truncation keeps it.

    Mirrors the IPC waiter contract of the other adapter package: on a
    narrow terminal the elastic columns shrink from the right, so a
    names-first format would lose the diagnostics count exactly when it
    matters most.  ``None`` (unreadable) renders ``N/A``; an empty list
    renders ``0``.  The cell format lives in
    :func:`gdr.derive.waiter_cell`; this name is kept as the adapter-facing
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
    """Render consistency-check results as a verdict row plus problem rows.

    The checks themselves must stay exhaustive -- an inapplicable check is
    reported so "not checked" is never read as "verified" -- but enumerating
    every ``skipped: not a data queue`` inline produced a 150-column value
    that buried the one line a reader acts on.  So the rendering splits by
    what the reader can do about each outcome, mirroring RT-Thread's
    named-verdict style (rtthread/diagnostics.py):

    * inapplicable by structure (a mutex has no storage pointers) is
      *counted* in the verdict as ``n/a``, never spelled out;
    * an unreadable field means the check could not run -- a degradation the
      reader may need to explain -- so it gets its own ``Check[<name>]`` row;
    * a failure gets its own row, because that is the actionable output.
    """
    verified = [name for name, status in checks if status == "ok"]
    problems = [
        (name, status)
        for name, status in checks
        if status.startswith("fail") or "unreadable" in status
    ]
    inapplicable = [
        name
        for name, status in checks
        if status.startswith("skipped") and "unreadable" not in status
    ]

    counts = [f"{len(verified)} verified"]
    if inapplicable:
        counts.append(f"{len(inapplicable)} n/a")
    failures = [name for name, status in problems if status.startswith("fail")]
    verdict = f"{len(failures)} failed" if failures else "ok"
    pairs = [("Checks", f"{verdict} ({', '.join(counts)})")]
    for name, status in problems:
        pairs.append((f"Check[{name}]", status.removeprefix("fail: ").strip()))
    return pairs


def queue_detail(obj, value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt queue <name>``.

    Key order is a stable output contract; ``Set`` appears only when the
    build has queue sets, and the FIFO ``Item[i]`` dump trails the checks.
    """
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
    item_size = obj.item_size
    for index, address, payload in iter_queue_items(value, layout):
        if payload is None:
            pairs.append((f"Item[{index}]", f"@0x{address:x}: unreadable"))
        elif item_size is not None and item_size > GDR_QUEUE_ITEM_DUMP_BYTES:
            pairs.append(
                (
                    f"Item[{index}]",
                    f"@0x{address:x}: {payload.hex(' ')}"
                    f"…(+{item_size - len(payload)} bytes)",
                )
            )
        else:
            pairs.append((f"Item[{index}]", f"@0x{address:x}: {payload.hex(' ')}"))
    return pairs


def semaphore_detail(obj, value, layout: FreeRtosLayout) -> list[tuple[str, str]]:
    """Build the vertical pairs for ``frt semaphore <name>``.

    Deliberately reads no ``u.xSemaphore`` union arm: only mutexes
    (``pcHead == NULL``) decode that, and reading it on a semaphore -- whose
    ``u`` member is QueuePointers_t -- would fabricate a holder from
    pcTail/pcReadFrom bytes (queue.c).
    """
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
        # Reason: the daemon is the only writer of the queue, so while it is
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
