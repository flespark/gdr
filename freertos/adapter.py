"""FreeRTOS task conversion and GDB convenience functions."""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos import heap as heap_module
from freertos.details import (
    checks_pairs,
    event_group_detail,
    held_cell,
    locks_cell,
    mutex_detail,
    queue_detail,
    semaphore_detail,
    stream_buffer_detail,
    task_detail,
    timer_detail,
    waiter_summary,
)
from freertos.diagnostics import system_checks
from freertos.events import event_group_table, value_to_event_group_object
from freertos.layout import (
    OBJECT_KIND_ORDER,
    STRUCT_BY_KIND,
    FreeRtosLayout,
    queue_type_label,
)
from freertos.navigation import (
    _QUEUE_FAMILY_KINDS,
    DiscoveredObject,
    _iter_list,
    _refine_queue_object,
    discover,
    discover_all,
    is_idle_task,
    iter_tasks,
    list_count,
    resolve_object,
    source_label,
    system_value,
    task_name_at,
    task_state,
)
from freertos.streams import (
    stream_buffer_table,
    value_to_stream_buffer_object,
)
from freertos.timers import (
    DAEMON_CURRENT_MESSAGE,
    callback_cell,
    daemon_is_current,
    id_cell,
    mode_cell,
    state_cell,
    timer_epoch,
    timer_expires_in,
    timer_is_on_lists,
    timer_subsystem_ready,
)
from gdr.adapter_api import (
    HeapReport,
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
)
from gdr.derive import fill_watermark
from gdr.formatting import format_address, format_optional_int
from gdr.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    read_bytes,
    read_cstring,
    read_int,
    value_address,
)
from gdr.layout import read_field, value_at


@dataclass
class FreeRtosTask:
    name: str = ""
    address: int = 0
    state: str = "Unknown"
    current_priority: int = 0
    base_priority: int = 0
    top_of_stack: int = 0
    stack_base: int = 0
    stack_end: int = 0
    stack_size: int | None = None
    stack_used: int | None = None
    # Bytes of untouched 0xa5 fill; the scan itself is word-granular.
    high_water_mark: int | None = None
    runtime_counter: int | None = None
    core: int | None = None
    core_affinity: int | None = None
    # Detail-only fields for ``frt task <name>``; absent members stay None.
    mutexes_held: int | None = None
    wake_tick: int | None = None
    blocked_on: str | None = None
    notify: list[tuple[int, int]] | None = None
    run_state: int | None = None
    preemption_disable: int | None = None
    critical_nesting: int | None = None
    errno: int | None = None
    delay_aborted: int | None = None
    statically_allocated: int | None = None
    tls_present: bool | None = None
    is_idle: bool = False


@dataclass
class FreeRtosQueueObject:
    """Display model for one queue-family object (queue/semaphore/mutex).

    Scalar fields that are unreadable stay ``None`` and render as ``N/A``;
    ``free`` is ``max(length - count, 0)`` and only set when both are known.
    Mutex-only fields (``holder``/``recursive_count``) are decoded strictly
    for ``kind == "mutex"`` -- a semaphore's ``u`` union member holds
    QueuePointers_t and must never be read as SemaphoreData_t.
    """

    name: str = "-"
    address: int = 0
    kind: str = "queue"
    inferred_kind: bool = False
    type_code: int | None = None
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    length: int | None = None
    item_size: int | None = None
    count: int | None = None
    free: int | None = None
    send_waiters: list[str] | None = None
    recv_waiters: list[str] | None = None
    rx_lock: int | None = None
    tx_lock: int | None = None
    set_container: int | None = None
    holder: str | None = None
    holder_address: int | None = None
    recursive_count: int | None = None


def _stack_type_size(layout=None) -> int:
    """Return ``sizeof(StackType_t)`` in bytes for the current target.

    ``StackType_t`` is a port typedef (``uint32_t`` on ARMv7-M, ``uint64_t`` on
    RV64); the width is probed into ``FreeRtosConfig.stack_word_bytes`` by
    :func:`freertos.layout.detect_config` (DWARF, falling back to the target
    pointer width), so the watermark scan never probes the type directly here.
    ``None`` (no layout in scope) keeps the previous probed value.
    """
    if layout is not None and layout.config.stack_word_bytes:
        return layout.config.stack_word_bytes
    return 4


def _high_water_mark(stack: bytes | None, stack_word_bytes: int) -> int | None:
    """Count untouched ``0xa5`` fill bytes at the low end of a stack.

    Stacks are prefilled with ``tskSTACK_FILL_BYTE`` (0xa5) under the
    watermarking macros; the high-water mark is the number of words that were
    never overwritten. Returns ``None`` when the stack was never filled (the
    first byte is not 0xa5) or the raw read failed, which the renderer reports
    as ``unavailable`` rather than a fabricated zero.  The count lives in
    :func:`gdr.derive.fill_watermark`; only grow-down stacks are supported
    here (the sole upstream ``portSTACK_GROWTH=+1`` port is SDCC/Cygnal 8051,
    which has no GCC toolchain and no QEMU machine, so the untouched fill
    always sits at the low end).
    """
    return fill_watermark(stack, from_low=True, word_bytes=stack_word_bytes)


def _read_notifications(
    value, sl, layout: FreeRtosLayout
) -> list[tuple[int, int]] | None:
    """Read ``(ulNotifiedValue, ucNotifyState)`` per notification slot."""
    notify_value = read_field(value, sl, "notify_value")
    notify_state = read_field(value, sl, "notify_state")
    if notify_value is None or notify_state is None:
        return None
    if layout.config.notification_array:
        return [
            (read_int(notify_value[index]) or 0, read_int(notify_state[index]) or 0)
            for index in range(layout.config.notification_count)
        ]
    return [(read_int(notify_value) or 0, read_int(notify_state) or 0)]


def value_to_task(
    value, state: str, core: int | None, layout: FreeRtosLayout
) -> FreeRtosTask:
    sl = layout.structs["struct tskTaskControlBlock"]
    top = read_int(read_field(value, sl, "top_of_stack")) or 0
    base = read_int(read_field(value, sl, "stack_base")) or 0
    end = read_int(read_field(value, sl, "stack_end")) or 0
    size = end - base if end and base and end >= base else None
    # Reason: pxEndOfStack only exists under configRECORD_STACK_HIGH_ADDRESS
    # or grow-up ports (tasks.c:403), and is absent from the default
    # downward-growing build. Watermark counting only needs a window that
    # covers the used region: [pxStack, pxTopOfStack] always contains the
    # untouched 0xa5 run, because the deepest historical use point is <= the
    # currently saved SP. When stack_end is present it is the stricter upper
    # bound; otherwise pxTopOfStack stands in.
    water_base = base
    water_end = end if (end and end >= base) else top
    # Reason: a corrupt TCB can report pxTopOfStack below pxStack; a negative
    # size would reach gdb's read_memory as an unsigned conversion and raise
    # OverflowError (not a MemoryError/gdb.error read_bytes degrades from),
    # aborting the whole table instead of one unavailable cell.
    water_size = (
        water_end - water_base
        if water_end and water_base and water_end > water_base
        else None
    )
    high = None
    word_bytes = _stack_type_size(layout)
    if water_size is not None and water_base:
        # Reason: when the whole window is untouched fill (all 0xa5) the count
        # reports the full window's word count. That is intentionally
        # conservative -- it never overstates free stack beyond what is visible
        # in the startup window -- which is exactly the direction a debugger
        # must prefer over a fabricated larger number.
        raw = read_bytes(water_base, water_size)
        # Reason: the scan is word-granular (the kernel's fill and
        # uxTaskGetStackHighWaterMark both count StackType_t words), but the
        # model carries bytes so Stack/Used/HighWater render in one unit.
        high_words = _high_water_mark(raw, word_bytes)
        high = high_words * word_bytes if high_words is not None else None
    state_item = read_field(value, sl, "state_list_item")
    wake_tick = None
    if state_item is not None:
        item_layout = layout.structs["struct xLIST_ITEM"]
        wake_tick = read_int(read_field(state_item, item_layout, "value"))
    tls = read_field(value, sl, "tls")
    return FreeRtosTask(
        name=read_cstring(read_field(value, sl, "name")) or "",
        address=value_address(value),
        state=state,
        current_priority=read_int(read_field(value, sl, "current_priority")) or 0,
        base_priority=read_int(read_field(value, sl, "base_priority")) or 0,
        top_of_stack=top,
        stack_base=base,
        stack_end=end,
        stack_size=size,
        stack_used=(end - top if end and top and end >= top else None),
        high_water_mark=high,
        runtime_counter=read_int(read_field(value, sl, "runtime_counter")),
        core=core,
        core_affinity=read_int(read_field(value, sl, "core_affinity")),
        mutexes_held=read_int(read_field(value, sl, "mutexes_held")),
        wake_tick=wake_tick,
        notify=_read_notifications(value, sl, layout),
        run_state=read_int(read_field(value, sl, "run_state")),
        preemption_disable=read_int(read_field(value, sl, "preemption_disable")),
        critical_nesting=read_int(read_field(value, sl, "critical_nesting")),
        errno=read_int(read_field(value, sl, "errno")),
        delay_aborted=read_int(read_field(value, sl, "delay_aborted")),
        statically_allocated=read_int(read_field(value, sl, "statically_allocated")),
        # Reason: xTLSBlock/xNewLib_reent is a struct on newlib and a pointer
        # on picolibc, so read_int() cannot decode it. Presence of the member
        # is the fact we report; None means the layout has no TLS member.
        tls_present=(tls is not None) if "tls" in sl.fields else None,
        is_idle=is_idle_task(value, layout),
    )


def iter_converted_tasks(layout: FreeRtosLayout):
    for value, state, core in iter_tasks(layout):
        yield value_to_task(value, state, core, layout)


def iter_task_names(layout: FreeRtosLayout):
    """Yield the name of every known task (for tab completion)."""
    sl = layout.structs["struct tskTaskControlBlock"]
    for value, _state, _core in iter_tasks(layout):
        name = read_cstring(read_field(value, sl, "name")) or ""
        if name:
            yield name


def find_task(name: str, layout: FreeRtosLayout):
    """Return the TCB whose name matches, reading only the name field."""
    sl = layout.structs["struct tskTaskControlBlock"]
    for value, _state, _core in iter_tasks(layout):
        task_name = read_cstring(read_field(value, sl, "name")) or ""
        if task_name == name:
            return value
    return None


def _cast_object(address: int, kind: str, layout: FreeRtosLayout) -> gdb.Value | None:
    """Cast a discovered object address to its native DWARF struct value."""
    if gdb is None:
        return None
    struct_key = STRUCT_BY_KIND.get(kind.strip().lower())
    if struct_key is None:
        return None
    struct_layout = layout.structs.get(struct_key)
    if struct_layout is None:
        return None
    # Cast through the generic helper so the DWARF type name comes from the
    # layout description (never a literal); value_at already degrades to
    # None on any expected access error.
    return value_at(address, struct_layout)


def _queue_type_present(layout: FreeRtosLayout) -> bool:
    """Whether the kernel exposes a QueueDefinition type (queue support).

    Probed into ``FreeRtosConfig.queue_support`` by detect_config (the
    DWARF type presence decides it), so the adapter never re-probes the
    type name directly.
    """
    return layout.config.queue_support


def _kind_enabled(kind: str, layout: FreeRtosLayout) -> bool:
    """Whether objects of *kind* can exist in the current target config.

    ``object_counts`` only reports kinds whose DWARF type is present, so a
    build without event groups or stream buffers never shows a zero row for
    a kind that cannot exist.
    """
    if kind == "task":
        return True
    if kind in ("queue", "semaphore", "mutex"):
        return _queue_type_present(layout)
    if kind == "timer":
        return layout.config.timers
    if kind == "eventgroup":
        return layout.config.event_groups
    if kind == "streambuffer":
        return layout.config.stream_buffers
    return False


# Message attached to the queue-family tables when the build has no
# configUSE_TRACE_FACILITY: the Type cells then only carry the discriminated
# family (with a ``?``) because binary/counting and plain/recursive variants
# leave no runtime trace to tell them apart.
_INFERRED_KIND_MESSAGE = (
    "no configUSE_TRACE_FACILITY: queue-family kinds are inferred from "
    "Queue_t pointers and item size; binary vs counting semaphores and "
    "mutexes vs recursive mutexes are indistinguishable"
)


def _normalize_lock(lock: int | None) -> int | None:
    """Normalize an int8_t lock counter; 255 (unsigned) == -1 (unlocked).

    ``cRxLock``/``cTxLock`` are ``volatile int8_t`` (queue.c) whose unlocked
    sentinel is ``queueUNLOCKED == -1``; a read that comes back as 255 is
    the same byte seen unsigned, so it is normalized before the "-" check.
    """
    if lock is None:
        return None
    return -1 if lock == 255 else lock


def waiter_names(value, layout: FreeRtosLayout, wait_list: str) -> list[str] | None:
    """Return names of tasks blocked on one of the object's wait lists.

    Args:
        wait_list: ``"send"`` for ``xTasksWaitingToSend`` (blocked senders,
            e.g. a full queue) or ``"receive"`` for
            ``xTasksWaitingToReceive`` (blocked takers, e.g. an empty queue,
            a semaphore take or a mutex take).

    Walks the List_t head the same way the scheduler lists are walked; each
    item's owner is the blocked task's TCB (queue.c vListInsertEnd).
    Returns ``None`` when the list head is unreadable.
    """
    field = "send_waiters" if wait_list == "send" else "recv_waiters"
    head = read_field(value, layout.structs["struct QueueDefinition"], field)
    if head is None:
        return None
    sl = layout.structs["struct tskTaskControlBlock"]
    names: list[str] = []
    for task in _iter_list(head, layout):
        name = read_cstring(read_field(task, sl, "name")) or ""
        names.append(name or "-")
    return names


def value_to_queue_object(
    value,
    found: DiscoveredObject,
    layout: FreeRtosLayout,
) -> FreeRtosQueueObject:
    """Convert a discovered queue-family address into the display model.

    The ``u.xSemaphore`` union arm is decoded only for mutexes
    (``pcHead == NULL``); a semaphore's ``u`` member is QueuePointers_t and
    reading the other arm would fabricate a holder from pcTail/pcReadFrom
    bytes (queue.c).
    """
    obj = FreeRtosQueueObject(
        name=found.name or "-",
        address=found.address,
        kind=found.kind,
        inferred_kind=found.inferred_kind,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    ql = layout.structs["struct QueueDefinition"]
    length = read_int(read_field(value, ql, "length"))
    count = read_int(read_field(value, ql, "count"))
    item_size = read_int(read_field(value, ql, "item_size"))
    obj.length = length
    obj.count = count
    obj.item_size = item_size
    if length is not None and count is not None:
        obj.free = max(length - count, 0)
    obj.send_waiters = waiter_names(value, layout, "send")
    obj.recv_waiters = waiter_names(value, layout, "receive")
    obj.rx_lock = _normalize_lock(read_int(read_field(value, ql, "rx_lock")))
    obj.tx_lock = _normalize_lock(read_int(read_field(value, ql, "tx_lock")))
    if layout.config.trace_facility:
        obj.type_code = read_int(read_field(value, ql, "type"))
    if layout.config.queue_sets:
        obj.set_container = read_int(read_field(value, ql, "set_container"))
    if found.kind == "mutex":
        holder = read_int(read_field(value, ql, "mutex_holder"))
        obj.holder_address = holder
        if holder:
            obj.holder = task_name_at(holder, layout)
        obj.recursive_count = read_int(read_field(value, ql, "recursive_count"))
    return obj


@dataclass
class FreeRtosTimerObject:
    """Display model for one software timer (struct tmrTimerControl).

    ``expiry`` is the list item's xItemValue (the absolute expiry tick); it
    is only meaningful while the timer is linked into an active list -- a
    dormant timer keeps a stale value, so the renderer shows ``N/A``.
    ``container``/``owner`` are the list item's membership fields; the owner
    is heap garbage on a never-linked timer, so the OwnerCheck is decided by
    the container (freertos.timers.owner_check) and never by the owner value
    itself.
    """

    name: str = "-"
    address: int = 0
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    period: int | None = None
    status: int | None = None
    expiry: int | None = None
    callback: int | None = None
    id: int | None = None
    timer_number: int | None = None
    owner: int | None = None
    container: int | None = None


def value_to_timer_object(
    value,
    found: DiscoveredObject,
    layout: FreeRtosLayout,
) -> FreeRtosTimerObject:
    """Convert a discovered timer address into the display model.

    ``uxTimerNumber`` is only read under the trace facility -- the member
    does not exist in a trace-off build (timers.c) -- and is not usable as
    an identifier anyway, because prvInitialiseNewTimer never writes it.
    """
    obj = FreeRtosTimerObject(
        name=found.name or "-",
        address=found.address,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    tl = layout.structs["struct tmrTimerControl"]
    obj.period = read_int(read_field(value, tl, "period"))
    obj.status = read_int(read_field(value, tl, "status"))
    # The list-item membership fields are nested struct paths declared in
    # the layout (they compose through the ListItem layout); the container
    # member name follows the probed spelling (pvContainer / pxContainer)
    # inside layout.py, so no re-probe here.
    obj.expiry = read_int(read_field(value, tl, "expiry"))
    obj.callback = read_int(read_field(value, tl, "callback"))
    obj.id = read_int(read_field(value, tl, "id"))
    obj.owner = read_int(read_field(value, tl, "owner"))
    obj.container = read_int(read_field(value, tl, "container"))
    if layout.config.trace_facility:
        obj.timer_number = read_int(read_field(value, tl, "number"))
    return obj


class FreeRtosAdapter(RtosAdapter):
    """Expose FreeRTOS scheduler lists through the shared task contract."""

    def __init__(self, layout: FreeRtosLayout) -> None:
        self.layout = layout

    def find_task(self, name: str) -> gdb.Value | None:
        return find_task(name, self.layout)

    def find_object(self, kind: str, name: str) -> gdb.Value | None:
        """Return the native object value for a name, address or symbol.

        First tries the discovery channels by name (registry names, static
        symbol names, active timer names), then falls back to resolving the
        argument as an explicit ``0x`` address, decimal address or symbol
        name.  Returns the object struct value, or ``None``.
        """
        layout = self.layout
        requested = kind.strip().lower()
        # Reason: tasks stay on the scheduler-list lookup so
        # $gdr_object("task", "IDLE") agrees with the task table instead of
        # requiring a static symbol for the task.
        if requested == "task":
            task = find_task(name, self.layout)
            if task is not None:
                return task
        for found in discover(requested, layout):
            if found.name == name:
                return _cast_object(found.address, requested, layout)
        resolved = resolve_object(requested, name, layout)
        if resolved is None:
            return None
        return _cast_object(resolved.address, requested, layout)

    def object_counts(self) -> dict[str, int]:
        """Return per-kind object counts for reliably enumerable kinds."""
        return {kind: count for kind, count, _sources in self.object_summary_rows()}

    def object_summary_rows(self) -> list[tuple[str, int, str]]:
        """Return ``(kind, count, "source=count ...")`` rows for each kind.

        The task count comes from the scheduler-list snapshot (authoritative:
        it covers dynamically created tasks too); other kinds count the
        discovery channels' deduplicated results from one shared scan, so
        the waiter channel runs once for the whole summary instead of once
        per kind.
        """
        tasks = list(iter_converted_tasks(self.layout))
        rows: list[tuple[str, int, str]] = [
            ("task", len(tasks), f"scheduler={len(tasks)}")
        ]
        enabled_kinds = [
            kind for kind in OBJECT_KIND_ORDER[1:] if _kind_enabled(kind, self.layout)
        ]
        # Reason: the shared channel scan walks every task list, so it only
        # runs when at least one non-task kind can actually exist; a build
        # with no such kind (or a test layout) never triggers it.
        all_found = discover_all(self.layout) if enabled_kinds else {}
        for kind in enabled_kinds:
            found = all_found.get(kind, [])
            sources: dict[str, int] = {}
            for obj in found:
                sources[obj.source] = sources.get(obj.source, 0) + 1
            source_text = " ".join(
                f"{name}={count}" for name, count in sorted(sources.items())
            )
            rows.append((kind, len(found), source_text))
        return rows

    def object_summary_table(self) -> ObjectTable:
        """Build the ``frt objects`` provenance summary table.

        The messages (rendered above the table by the core) state the
        enumeration limitation and any disabled channel, so a count is never
        mistaken for a complete object inventory.
        """
        messages = [
            "object discovery covers registered, static-symbol and "
            "scheduler-reachable objects; unregistered dynamic objects with "
            "no global handle are not enumerable"
        ]
        if not self.layout.config.queue_registry:
            messages.append(
                "queue registry: configQUEUE_REGISTRY_SIZE is 0, so the "
                "registry channel is disabled"
            )
        rows = [
            [kind, str(count), sources]
            for kind, count, sources in self.object_summary_rows()
        ]
        return ObjectTable(
            headers=["Kind", "Count", "Sources"],
            rows=rows,
            messages=messages,
            elastic=("Sources",),
        )

    def object_table(self, kind: str) -> ObjectTable | None:
        """Return the list table for one object kind, or ``None``.

        Semaphores and mutexes are Queue_t objects refined by the discovery
        layer, so the queue-family entry point serves ``frt queues``, ``frt
        semaphores`` and ``frt mutexes`` with their own column contracts;
        timers have their own model and table.
        """
        kind = kind.strip().lower()
        if kind == "timer":
            return self._timer_table()
        if kind == "eventgroup":
            return event_group_table(
                [
                    value_to_event_group_object(
                        _cast_object(found.address, found.kind, self.layout),
                        found,
                        self.layout,
                    )
                    for found in discover(kind, self.layout)
                ],
                self.layout,
            )
        if kind == "streambuffer":
            return stream_buffer_table(
                [
                    value_to_stream_buffer_object(
                        _cast_object(found.address, found.kind, self.layout),
                        found,
                        self.layout,
                    )
                    for found in discover(kind, self.layout)
                ],
                self.layout,
            )
        if kind not in _QUEUE_FAMILY_KINDS:
            return None
        objects = [
            value_to_queue_object(
                _cast_object(found.address, found.kind, self.layout),
                found,
                self.layout,
            )
            for found in discover(kind, self.layout)
        ]
        if kind == "queue":
            return self._queue_table(objects)
        if kind == "semaphore":
            return self._semaphore_table(objects)
        return self._mutex_table(objects)

    def _table_messages(self) -> list[str]:
        """Capability messages for the queue-family tables."""
        messages: list[str] = []
        if not self.layout.config.trace_facility:
            messages.append(_INFERRED_KIND_MESSAGE)
        return messages

    def _queue_table(self, objects: list[FreeRtosQueueObject]) -> ObjectTable:
        """Build the ``frt queues`` table.

        The ``Set`` column exists only when the build has queue sets
        (``configUSE_QUEUE_SETS``): the member struct only exists then, so an
        unconditional column would render a whole ``N/A`` column that looks
        like "not in a set" instead of "no sets in this build".
        """
        layout = self.layout
        headers = [
            "Name",
            "Type",
            "Items",
            "Length",
            "ItemSize",
            "Free",
            "SendWait",
            "RecvWait",
            "Locks",
        ]
        if layout.config.queue_sets:
            headers.append("Set")
        headers += ["Src", "Addr"]
        rows = []
        for obj in objects:
            row = [
                obj.name,
                queue_type_label(obj.kind, obj.type_code, obj.inferred_kind),
                format_optional_int(obj.count),
                format_optional_int(obj.length),
                format_optional_int(obj.item_size),
                format_optional_int(obj.free),
                waiter_summary(obj.send_waiters),
                waiter_summary(obj.recv_waiters),
                locks_cell(obj.rx_lock, obj.tx_lock),
            ]
            if layout.config.queue_sets:
                row.append(
                    format_address(obj.set_container) if obj.set_container else "-"
                )
            row += [
                source_label(obj.source, obj.extra_sources),
                format_address(obj.address),
            ]
            rows.append(row)
        return ObjectTable(
            headers=headers,
            rows=rows,
            messages=self._table_messages(),
            elastic=("SendWait", "RecvWait", "Name"),
        )

    def _semaphore_table(self, objects: list[FreeRtosQueueObject]) -> ObjectTable:
        """Build the ``frt semaphores`` table.

        ``Count`` is the semaphore's available count (uxMessagesWaiting),
        ``Max`` its upper bound (uxLength); waiters are the tasks blocked on
        take (xTasksWaitingToReceive).
        """
        rows = [
            [
                obj.name,
                queue_type_label(obj.kind, obj.type_code, obj.inferred_kind),
                format_optional_int(obj.count),
                format_optional_int(obj.length),
                waiter_summary(obj.recv_waiters),
                source_label(obj.source, obj.extra_sources),
                format_address(obj.address),
            ]
            for obj in objects
        ]
        return ObjectTable(
            headers=["Name", "Type", "Count", "Max", "Waiters", "Src", "Addr"],
            rows=rows,
            messages=self._table_messages(),
            elastic=("Waiters", "Name"),
        )

    def _timer_table(self) -> ObjectTable:
        """Build the ``frt timers`` table.

        Rows are partitioned so the reliably named active timers (``source
        == "active"``, found on the daemon's lists) come first and the
        dormant ones after them; a dormant timer's ``Expiry``/``ExpiresIn``
        must be ``N/A`` because its list-item value is a stale leftover that
        would look like a real deadline.
        """
        layout = self.layout
        headers = [
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
        if not layout.config.timers:
            return ObjectTable(
                headers=headers,
                rows=[],
                messages=["no software timers in this build (configUSE_TIMERS=0)"],
            )
        if not timer_subsystem_ready():
            return ObjectTable(
                headers=headers,
                rows=[],
                messages=[
                    "timer subsystem not initialised (pxCurrentTimerList == NULL)"
                ],
            )
        objects = [
            value_to_timer_object(
                _cast_object(found.address, "timer", layout), found, layout
            )
            for found in discover("timer", layout)
        ]
        # Reason: partition keeps the active (list-reachable) timers first
        # and the dormant ones (static buffers / global handles) after them,
        # so a reader never has to guess which rows the kernel references.
        # List membership is the active channel's signal, which on a pool-
        # based build arrives as an extra source (see timer_is_on_lists).
        objects.sort(
            key=lambda obj: 0 if timer_is_on_lists(obj.source, obj.extra_sources) else 1
        )
        tick = system_value("tick", self.layout)
        mask = (1 << layout.config.tick_bits) - 1
        rows = []
        transition = False
        for obj in objects:
            listed = timer_is_on_lists(obj.source, obj.extra_sources)
            dormant = not listed
            state = state_cell(obj.source, obj.status, obj.extra_sources)
            transition = transition or state.endswith("?")
            in_overflow = timer_epoch(obj.container) == "overflow"
            rows.append(
                [
                    obj.name,
                    state,
                    mode_cell(obj.status),
                    format_optional_int(obj.period),
                    "N/A" if dormant else format_optional_int(obj.expiry),
                    "N/A"
                    if dormant
                    else timer_expires_in(obj.expiry, tick, mask, in_overflow),
                    callback_cell(obj.callback),
                    id_cell(obj.id),
                    source_label(obj.source, obj.extra_sources),
                    format_address(obj.address),
                ]
            )
        messages = [f"Kernel tick: {tick if tick is not None else 'N/A'}"]
        sources: dict[str, int] = {}
        for obj in objects:
            sources[obj.source] = sources.get(obj.source, 0) + 1
        if sources:
            messages.append(
                "provenance: "
                + " ".join(f"{name}={count}" for name, count in sorted(sources.items()))
            )
        messages.append(
            "stopped, expired one-shot and never-started timers are not "
            "referenced by any kernel global; they only appear when a static "
            "buffer or a global handle keeps them in the symbol table"
        )
        if transition:
            messages.append(
                "some timers show 'active?'/'dormant?': ucStatus and the timer "
                "lists disagree, which a queued start/stop command explains"
            )
        if daemon_is_current(layout):
            messages.append(DAEMON_CURRENT_MESSAGE)
        return ObjectTable(
            headers=headers,
            rows=rows,
            messages=messages,
            elastic=("Callback", "Name"),
        )

    def _mutex_table(self, objects: list[FreeRtosQueueObject]) -> ObjectTable:
        """Build the ``frt mutexes`` table.

        ``Held``/``Owner``/``Recursive`` come from the ``u.xSemaphore`` arm,
        which is only decodable for mutexes (``pcHead == NULL``).
        """
        rows = [
            [
                obj.name,
                queue_type_label(obj.kind, obj.type_code, obj.inferred_kind),
                held_cell(obj),
                obj.holder or "-",
                format_optional_int(obj.recursive_count),
                waiter_summary(obj.recv_waiters),
                source_label(obj.source, obj.extra_sources),
                format_address(obj.address),
            ]
            for obj in objects
        ]
        return ObjectTable(
            headers=[
                "Name",
                "Type",
                "Held",
                "Owner",
                "Recursive",
                "Waiters",
                "Src",
                "Addr",
            ],
            rows=rows,
            messages=self._table_messages(),
            elastic=("Waiters", "Name"),
        )

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:
        """Return one object's vertical detail (task, timer, event group,
        stream buffer or queue family)."""
        kind = kind.strip().lower()
        if kind == "task":
            value = find_task(name, self.layout)
            if value is None:
                return ObjectDetail(found=False)
            task = value_to_task(value, *task_state(value, self.layout), self.layout)
            return ObjectDetail(pairs=task_detail(task, self.layout, tcb_value=value))
        if kind == "timer":
            found, value = self._find_named_object(kind, name)
            if found is None or value is None:
                return ObjectDetail(found=False)
            obj = value_to_timer_object(value, found, self.layout)
            return ObjectDetail(pairs=timer_detail(obj, value, self.layout))
        if kind == "eventgroup":
            found, value = self._find_named_object(kind, name)
            if found is None or value is None:
                return ObjectDetail(found=False)
            obj = value_to_event_group_object(value, found, self.layout)
            return ObjectDetail(pairs=event_group_detail(obj, value, self.layout))
        if kind == "streambuffer":
            found, value = self._find_named_object(kind, name)
            if found is None or value is None:
                return ObjectDetail(found=False)
            obj = value_to_stream_buffer_object(value, found, self.layout)
            return ObjectDetail(pairs=stream_buffer_detail(obj, value, self.layout))
        if kind not in _QUEUE_FAMILY_KINDS:
            return None
        found, value = self._find_named_object(kind, name)
        if found is None or value is None:
            return ObjectDetail(found=False)
        # Reason: the address/symbol fallback stamps the *requested* kind on
        # whatever it resolved, so `frt semaphore <a mutex>` used to render a
        # semaphore block whose every consistency check failed. Refine the
        # kind and redirect instead; an inferred kind (no trace facility)
        # cannot justify a refusal, so it renders with the usual `?` marker.
        actual = _refine_queue_object(found, self.layout)
        if actual.kind != kind and not actual.inferred_kind:
            return ObjectDetail(
                found=False,
                message=(
                    f"{name!r} is a {actual.kind}, not a {kind}; "
                    f"try `freertos {actual.kind} {name}`"
                ),
            )
        obj = value_to_queue_object(value, found, self.layout)
        if kind == "queue":
            pairs = queue_detail(obj, value, self.layout)
        elif kind == "semaphore":
            pairs = semaphore_detail(obj, value, self.layout)
        else:
            pairs = mutex_detail(obj, value, self.layout)
        return ObjectDetail(pairs=pairs)

    def _find_named_object(self, kind: str, name: str):
        """Return ``(DiscoveredObject, native value)`` for a kind's name.

        Mirrors :meth:`find_object`: discovery names first, then the explicit
        address/symbol/decimal fallback -- but keeps the found object so the
        detail builder can render its provenance and inferred kind.  Kind-
        agnostic: timers and the queue family both resolve through it.
        """
        for found in discover(kind, self.layout):
            if found.name == name:
                return found, _cast_object(found.address, found.kind, self.layout)
        resolved = resolve_object(kind, name, self.layout)
        if resolved is None:
            return None, None
        return resolved, _cast_object(resolved.address, resolved.kind, self.layout)

    def iter_tasks(self):
        for value, _state, _core in iter_tasks(self.layout):
            yield value

    def task_table(self) -> ObjectTable:
        fields = self.layout.structs["struct tskTaskControlBlock"].fields
        show_base_priority = "base_priority" in fields
        show_stack = "stack_end" in fields
        show_runtime = "runtime_counter" in fields
        show_smp = self.layout.config.smp
        show_affinity = "core_affinity" in fields

        headers = ["Name", "State", "Prio"]
        if show_base_priority:
            headers.append("BasePrio")
        headers.append("SP")
        if show_stack:
            headers += ["Stack", "Used"]
        if show_runtime:
            headers.append("Runtime")
        if show_smp:
            headers.append("CPU")
        if show_affinity:
            headers.append("Affinity")
        headers.append("Addr")

        tasks = list(iter_converted_tasks(self.layout))
        # Reason: HighWater is a capability column -- it appears only when at
        # least one task's stack was actually filled with the watermark byte;
        # an all-unavailable column would be pure noise.
        show_high_water = any(task.high_water_mark is not None for task in tasks)
        if show_high_water:
            # Reason: the row appends the HighWater cell right after the
            # optional Stack/Used pair, i.e. after SP when stack bounds are
            # unavailable (pxEndOfStack absent). The header has to be inserted
            # at that exact position, otherwise every following column
            # (Runtime/CPU/Affinity/Addr) renders under the wrong heading.
            anchor = "Used" if "Used" in headers else "SP"
            headers.insert(headers.index(anchor) + 1, "HighWater")

        rows = []
        for task in tasks:
            row = [
                task.name + (" *" if task.core is not None else ""),
                task.state,
                str(task.current_priority),
            ]
            if show_base_priority:
                row.append(str(task.base_priority))
            row.append(format_address(task.top_of_stack))
            if show_stack:
                row += [
                    format_optional_int(task.stack_size),
                    format_optional_int(task.stack_used),
                ]
            if show_high_water:
                row.append(
                    str(task.high_water_mark)
                    if task.high_water_mark is not None
                    else "N/A"
                )
            if show_runtime:
                row.append(format_optional_int(task.runtime_counter))
            if show_smp:
                row.append(format_optional_int(task.core))
            if show_affinity:
                row.append(format_optional_int(task.core_affinity))
            row.append(format_address(task.address))
            rows.append(row)
        return ObjectTable(headers=headers, rows=rows, elastic=("Name",))

    def heap_report(self) -> HeapReport:
        """One collected heap snapshot formatted for ``frt heap``.

        The snapshot is collected once and both the pairs and the block
        table derive from it, so ``frt heap`` never walks the heap twice.
        """
        snap = heap_module.heap_snapshot(self.layout)
        table = heap_module.heap_block_table(snap)
        algorithm = snap.geometry.algorithm
        messages: list[str] = []
        if algorithm == "heap_3":
            messages.append(
                "heap_3 wraps the C library malloc; the libc heap is not inspectable"
            )
        elif algorithm == "none":
            messages.append("no FreeRTOS heap allocator is linked (no pvPortMalloc)")
        return HeapReport(
            pairs=heap_module.heap_pairs(snap),
            table=table,
            messages=messages,
        )

    def system_summary(self) -> SystemSummary:
        tasks = list(iter_converted_tasks(self.layout))
        current = next((task.name for task in tasks if task.core is not None), None)
        delayed = [list_count(key, self.layout) for key in ("delayed_1", "delayed_2")]
        counts = {
            "Ready": list_count("ready", self.layout),
            "Delayed": (
                sum(value for value in delayed if value is not None)
                if any(value is not None for value in delayed)
                else None
            ),
            "Pending": list_count("pending", self.layout),
            "Suspended": list_count("suspended", self.layout),
            "Termination": list_count("termination", self.layout),
        }
        scheduler = system_value("scheduler_running", self.layout)
        total = system_value("task_count", self.layout)
        # Reason: the heap snapshot is best-effort -- outside GDB the symbol
        # probes raise RuntimeError, and a broken target heap must not take
        # down ``frt system`` -- so a snapshot failure degrades to the
        # layout-only allocator label instead of aborting the summary.
        try:
            snap = heap_module.heap_snapshot(self.layout)
        except TARGET_ACCESS_ERRORS:
            snap = None
        if snap is not None:
            # Reason: pre-init the counters hold their static initializers
            # (heap_4's xFreeBytesRemaining is 0), so total - free would
            # report the whole heap as used; used bytes are only meaningful
            # once the heap manager has initialised.
            heap_used = (
                snap.total - snap.free
                if (
                    snap.initialised
                    and snap.total is not None
                    and snap.free is not None
                )
                else None
            )
            heap_status = heap_module.heap_status(snap)
            if snap.geometry.kind is not None:
                heap_allocator = f"heap_{snap.geometry.kind}"
            elif snap.geometry.algorithm == "heap_3":
                heap_allocator = "heap_3"
            else:
                heap_allocator = None
        else:
            heap_used = None
            heap_status = None
            heap_allocator = (
                f"heap_{self.layout.config.heap_kind}"
                if self.layout.config.heap_kind is not None
                else None
            )
        # Reason: the system checks are best-effort -- a broken heap or an
        # unreadable scheduler list must not take down ``frt system``, so a
        # checks failure degrades to an empty section (the summary rows are
        # still rendered from whatever symbols did read).
        try:
            checks = checks_pairs(system_checks(self.layout))
        except TARGET_ACCESS_ERRORS:
            checks = []
        return SystemSummary(
            kernel_version=(
                ".".join(map(str, self.layout.version))
                if self.layout.version is not None
                else "unknown"
            ),
            current_task=current,
            task_count=total if total is not None else len(tasks),
            # Reason: name the evidence source -- the kernel counter and the
            # scheduler-list walk are independent counts that agree on a
            # healthy kernel and diverge on a corrupt one (the TaskCount
            # check compares them), so the rows must not read as duplicates.
            task_count_label=(
                "Task count (kernel)" if total is not None else "Task count (walked)"
            ),
            tick_count=system_value("tick", self.layout),
            scheduler_state=(
                "running"
                if scheduler
                else "not-running"
                if scheduler is not None
                else "N/A"
            ),
            state_counts={
                name: value for name, value in counts.items() if value is not None
            },
            object_counts={"task (walked)": len(tasks)},
            heap_allocator=heap_allocator,
            heap_used=heap_used,
            heap_total=(snap.total if snap is not None else None),
            heap_status=heap_status,
            extra_pairs=checks,
        )
