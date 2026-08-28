"""DWARF path layouts for FreeRTOS kernel structures."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import (
    lookup_symbol,
    lookup_type,
    read_macro_int,
    symbol_exists,
)
from gdr.layout import StructField, StructLayout

# TODO: replace by exception guard
# DWARF probe failures we degrade from during config detection.  Module scope
# keeps the tuple computable outside GDB (where ``gdb`` is ``None``); anything
# outside this set bubbles to the ``gdr init`` command guard for a diagnostic.
if gdb is not None:
    _PROBE_ERRORS = (gdb.error, TypeError, ValueError, AttributeError)
else:
    _PROBE_ERRORS = (TypeError, ValueError, AttributeError)


@dataclass
class FreeRtosConfig:
    smp: bool = False
    number_of_cores: int = 1
    notifications: bool = True
    notification_array: bool = False
    notification_count: int = 1
    tick_bits: int = 32
    runtime_counter_bits: int | None = 32
    mini_list: bool | None = None
    tls_field: str | None = None
    list_item_container_field: str | None = None
    stack_end_field: str | None = None
    trace_facility: bool = False
    queue_registry: bool = False
    queue_registry_size: int = 0
    timers: bool = False
    static_allocation: bool = False
    static_and_dynamic: bool = False
    max_priorities: int | None = None
    list_integrity_check: bool = False
    event_groups: bool = False
    stream_buffers: bool = False
    stream_buffer_notification_index: bool = False
    queue_sets: bool = False
    task_attributes: bool = False
    preemption_disable: bool = False
    critical_nesting_in_tcb: bool = False
    posix_errno: bool = False
    delay_abort: bool = False
    mpu_object_pool: bool = False
    heap_kind: int | None = None
    heap_protector: bool = False
    tcb_fields: frozenset[str] = frozenset()


def _fields(type_name: str) -> set[str]:
    typ = lookup_type(type_name)
    if typ is None:
        return set()
    try:
        return {field.name for field in typ.strip_typedefs().fields() if field.name}
    except _PROBE_ERRORS:
        return set()


def _macro_int(name: str) -> int | None:
    return read_macro_int(name)


def _array_bound(name: str) -> int | None:
    """Return the element count of an array symbol, or ``None``.

    GDB's ``Type.range()`` yields an inclusive upper bound, so the array
    length is ``range()[1] + 1``.  ``None`` when the symbol is missing or its
    type is not a decodable array.
    """
    value = lookup_symbol(name)
    if value is None:
        return None
    try:
        count = value.type.strip_typedefs().range()[1] + 1
    except _PROBE_ERRORS:
        return None
    # Reason: an unbounded declaration (``extern T sym[]``) reports range()
    # as (0, -1), i.e. count 0. Report that as unknown rather than as a real
    # length: callers treat 0 as a hard fact (core loops would iterate zero
    # times and silently report no running task).
    return count if count > 0 else None


def _mini_list_detected() -> bool | None:
    """Return whether mini list items are in use, or ``None`` when unknown.

    `configUSE_MINI_LIST_ITEM` is on by default in FreeRTOS (FreeRTOS.h). When
    on, ``xMINI_LIST_ITEM`` (used for ``List_t::xListEnd``) carries only
    ``xItemValue``/``pxNext``/``pxPrevious``; when off, ``MiniListItem_t``
    aliases the full ``xLIST_ITEM`` which *does* carry ``pvOwner``.  So the
    absence of ``pvOwner`` in ``xListEnd``'s type proves mini list items.
    """
    typ = lookup_type("struct xLIST")
    if typ is None:
        return None
    try:
        for member in typ.fields():
            if member.name != "xListEnd":
                continue
            end_fields = {
                f.name for f in member.type.strip_typedefs().fields() if f.name
            }
            return "pvOwner" not in end_fields
    except _PROBE_ERRORS:
        pass
    return None


def _block_link_type_present() -> bool:
    """Return whether DWARF exposes ``BlockLink_t`` / ``struct A_BLOCK_LINK``.

    heap_2/4/5 all define that typedef in their ``.c`` file; heap_1 and heap_3
    do not. Missing type info is a failed check, not a pass.
    """
    return (
        lookup_type("BlockLink_t") is not None
        or lookup_type("struct A_BLOCK_LINK") is not None
    )


def _detect_heap_kind() -> int | None:
    """Identify the heap manager (heap_1..heap_5) by symbols and types.

    Signatures (heap_5 always also carries heap_4's ``pxEnd``, so 5 supersedes
    4; any other overlapping match is reported as unknown rather than picking
    a priority winner):
    * heap_5 -> ``vPortDefineHeapRegions`` plus ``BlockLink_t``
    * heap_4 -> ``pxEnd`` plus ``BlockLink_t``, without the regions API
    * heap_1 -> ``xNextFreeByte`` (sole BlockLink-free allocator)
    * heap_2 -> ``xStart`` and ``xEnd`` plus ``BlockLink_t``
    * else ``None`` (heap_3 wraps ``malloc`` and exports no kernel heap symbol)
    """
    has_regions = symbol_exists("vPortDefineHeapRegions")
    has_px_end = symbol_exists("pxEnd")
    has_next_free = symbol_exists("xNextFreeByte")
    has_x_start = symbol_exists("xStart")
    has_x_end = symbol_exists("xEnd")
    has_block_link = _block_link_type_present()
    matches: list[int] = []
    if has_regions and has_block_link:
        matches.append(5)
    if has_px_end and has_block_link:
        matches.append(4)
    if has_next_free:
        matches.append(1)
    # Reason: heap_2's xEnd is a BlockLink_t *value*; heap_4/5 use pxEnd as a
    # pointer and must not also match heap_2 when a test fixture (or a
    # corrupted symbol table) happens to expose both names.
    if has_x_start and has_x_end and has_block_link and not has_px_end:
        matches.append(2)
    # Reason: heap_5.c reuses heap_4's xStart/pxEnd plus vPortDefineHeapRegions,
    # so a real heap_5 always matches 4 as well. Drop the dominated 4; any
    # leftover overlap (heap_1 symbols next to a BlockLink heap, etc.) is
    # ambiguous and must not silently pick the priority-chain winner.
    if 5 in matches and 4 in matches:
        matches.remove(4)
    if len(matches) != 1:
        return None
    return matches[0]


def detect_config() -> FreeRtosConfig:
    """Probe FreeRTOS configuration from symbols, macros and DWARF fields."""
    fields = _fields("struct tskTaskControlBlock")
    cfg = FreeRtosConfig()
    cfg.tcb_fields = frozenset(fields)

    cfg.smp = lookup_symbol("pxCurrentTCBs") is not None
    if cfg.smp:
        # Reason: pxCurrentTCBs[TASK_CORE_ID] bound is authoritative for the
        # core count; the configNUMBER_OF_CORES macro is only a fallback.
        bound = _array_bound("pxCurrentTCBs")
        if bound is not None:
            cfg.number_of_cores = bound
        else:
            value = _macro_int("configNUMBER_OF_CORES")
            cfg.number_of_cores = value if value and value > 0 else 2

    cfg.notifications = "ulNotifiedValue" in fields
    typ = lookup_type("struct tskTaskControlBlock")
    if typ is not None and gdb is not None:
        try:
            notification = next(
                (
                    field.type.strip_typedefs()
                    for field in typ.fields()
                    if field.name == "ulNotifiedValue"
                ),
                None,
            )
            if notification is not None and notification.code == gdb.TYPE_CODE_ARRAY:
                cfg.notification_array = True
                cfg.notification_count = notification.range()[1] + 1
        except _PROBE_ERRORS:
            pass
    tick = lookup_type("TickType_t")
    runtime = lookup_type("configRUN_TIME_COUNTER_TYPE")
    if tick is not None:
        cfg.tick_bits = tick.sizeof * 8
    if runtime is not None:
        cfg.runtime_counter_bits = runtime.sizeof * 8

    cfg.mini_list = _mini_list_detected()
    cfg.tls_field = next(
        (name for name in ("xTLSBlock", "xNewLib_reent") if name in fields),
        None,
    )
    # Reason: configENABLE_BACKWARD_COMPATIBILITY defaults to 1, which
    # ``#define pxContainer pvContainer`` (FreeRTOS.h). The default build's
    # DWARF member is therefore named ``pvContainer``; a non-compatible build
    # (or a kernel that dropped the macro) uses ``pxContainer``. Access has to
    # follow whichever spelling actually exists, or list membership reads
    # would always fail and every non-running task would be mislabelled.
    cfg.list_item_container_field = next(
        (
            name
            for name in ("pvContainer", "pxContainer")
            if name in _fields("struct xLIST_ITEM")
        ),
        None,
    )
    cfg.stack_end_field = next(
        (name for name in ("pxEndOfStack", "pxStackEnd") if name in fields),
        None,
    )

    # Reason: trace facility (queue type classification) is proven by the
    # ucQueueType member. Some GDB/DWARF spellings expose the struct tag
    # (``struct QueueDefinition``) while others only expose the typedef
    # (``xQUEUE``); ``_fields`` already strip_typedefs() so either works.
    queue_fields = _fields("struct QueueDefinition") or _fields("xQUEUE")
    cfg.trace_facility = "ucQueueType" in queue_fields
    cfg.queue_sets = "pxQueueSetContainer" in queue_fields
    cfg.queue_registry = lookup_symbol("xQueueRegistry") is not None
    cfg.queue_registry_size = _array_bound("xQueueRegistry") or 0
    cfg.timers = (
        lookup_symbol("xTimerTaskHandle") is not None
        or lookup_symbol("xTimerQueue") is not None
    )
    # Reason: tskSTATIC_AND_DYNAMIC_ALLOCATION_POSSIBLE (FreeRTOS.h) is what
    # actually emits ucStaticallyAllocated; that is only true when *both*
    # static and dynamic allocation are possible. static-only builds still
    # export xTaskCreateStatic but have no TCB marker, so the two facts are
    # probed separately.
    cfg.static_allocation = symbol_exists("xTaskCreateStatic")
    cfg.static_and_dynamic = "ucStaticallyAllocated" in fields

    cfg.mpu_object_pool = symbol_exists("xKernelObjectPool")
    cfg.heap_kind = _detect_heap_kind()
    cfg.heap_protector = symbol_exists("xHeapCanary")

    cfg.event_groups = lookup_type("struct EventGroupDef_t") is not None
    stream_fields = _fields("struct StreamBufferDef_t")
    cfg.stream_buffers = lookup_type("struct StreamBufferDef_t") is not None
    cfg.stream_buffer_notification_index = "uxNotificationIndex" in stream_fields

    cfg.list_integrity_check = "xListItemIntegrityValue1" in _fields(
        "struct xLIST_ITEM"
    )
    cfg.max_priorities = _array_bound("pxReadyTasksLists")

    cfg.task_attributes = "uxTaskAttributes" in fields
    cfg.preemption_disable = "xPreemptionDisable" in fields
    cfg.critical_nesting_in_tcb = "uxCriticalNesting" in fields
    cfg.posix_errno = "iTaskErrno" in fields
    cfg.delay_abort = "ucDelayAborted" in fields
    return cfg


@dataclass
class FreeRtosLayout:
    structs: dict[str, StructLayout] = field(default_factory=dict)
    lists: dict[str, str] = field(default_factory=dict)
    config: FreeRtosConfig = field(default_factory=FreeRtosConfig)
    version: tuple[int, int, int] | None = None


# FreeRTOS ucQueueType values (queue.h).  Used to summarize a queue's kind in
# the pretty-printed fold and to render the ``Type`` cell of the queue-family
# tables/details.
_QUEUE_TYPE_NAMES: dict[int, str] = {
    0: "queue",
    1: "mutex",
    2: "counting-sem",
    3: "binary-sem",
    4: "recursive-mutex",
    5: "queue-set",
}


def queue_type_label(kind: str, type_code: int | None, inferred: bool) -> str:
    """Render the ``Type`` cell for one queue-family object.

    With ``ucQueueType`` the exact label comes from the type code (queue.h:
    BASE=0 ... SET=5).  Without it (``configUSE_TRACE_FACILITY`` off) only
    the discriminated family is known -- binary vs counting semaphores and
    plain vs recursive mutexes are indistinguishable -- so the label falls
    back to the kind and carries a ``?`` suffix to say the precise variant
    is guessed.
    """
    if type_code is not None:
        base = _QUEUE_TYPE_NAMES.get(type_code, str(type_code))
    else:
        base = {"queue": "queue", "semaphore": "semaphore", "mutex": "mutex"}.get(
            kind, kind
        )
    return f"{base}?" if inferred else base


def build_layout(
    cfg: FreeRtosConfig, version: tuple[int, int, int] | None = None
) -> FreeRtosLayout:
    tcb_fields: dict[str, StructField] = {
        "top_of_stack": StructField("top_of_stack", ("pxTopOfStack",)),
        "state_list_item": StructField("state_list_item", ("xStateListItem",)),
        "event_list_item": StructField("event_list_item", ("xEventListItem",)),
        "current_priority": StructField(
            "current_priority", ("uxPriority",), summary=True
        ),
        "stack_base": StructField("stack_base", ("pxStack",)),
        "name": StructField("name", ("pcTaskName",), kind="string", summary=True),
    }
    if "uxBasePriority" in cfg.tcb_fields:
        tcb_fields["base_priority"] = StructField(
            "base_priority", ("uxBasePriority",), summary=True
        )
    if "ulRunTimeCounter" in cfg.tcb_fields:
        tcb_fields["runtime_counter"] = StructField(
            "runtime_counter", ("ulRunTimeCounter",)
        )
    if cfg.stack_end_field:
        tcb_fields["stack_end"] = StructField("stack_end", (cfg.stack_end_field,))
    if cfg.tls_field:
        tcb_fields["tls"] = StructField("tls", (cfg.tls_field,))
    if cfg.smp:
        if "xTaskRunState" in cfg.tcb_fields:
            tcb_fields["run_state"] = StructField(
                "run_state", ("xTaskRunState",), summary=True
            )
        if "uxCoreAffinityMask" in cfg.tcb_fields:
            tcb_fields["core_affinity"] = StructField(
                "core_affinity", ("uxCoreAffinityMask",)
            )
    # Detail-only fields for ``frt task <name>``.  Each is gated on the actual
    # DWARF member so absent members never render a fabricated column/pair.
    if "uxMutexesHeld" in cfg.tcb_fields:
        tcb_fields["mutexes_held"] = StructField("mutexes_held", ("uxMutexesHeld",))
    if cfg.notifications:
        tcb_fields["notify_value"] = StructField("notify_value", ("ulNotifiedValue",))
        tcb_fields["notify_state"] = StructField("notify_state", ("ucNotifyState",))
    if "ucStaticallyAllocated" in cfg.tcb_fields:
        tcb_fields["statically_allocated"] = StructField(
            "statically_allocated", ("ucStaticallyAllocated",)
        )
    if "ucDelayAborted" in cfg.tcb_fields:
        tcb_fields["delay_aborted"] = StructField("delay_aborted", ("ucDelayAborted",))
    if "iTaskErrno" in cfg.tcb_fields:
        tcb_fields["errno"] = StructField("errno", ("iTaskErrno",))
    if cfg.critical_nesting_in_tcb:
        tcb_fields["critical_nesting"] = StructField(
            "critical_nesting", ("uxCriticalNesting",)
        )
    if cfg.preemption_disable:
        tcb_fields["preemption_disable"] = StructField(
            "preemption_disable", ("xPreemptionDisable",)
        )
    if cfg.task_attributes:
        tcb_fields["task_attributes"] = StructField(
            "task_attributes", ("uxTaskAttributes",)
        )
    queue_fields: dict[str, StructField] = {
        "length": StructField("length", ("uxLength",), summary=True),
        "count": StructField("count", ("uxMessagesWaiting",), summary=True),
    }
    if cfg.trace_facility:
        # Reason: ucQueueType only exists under configUSE_TRACE_FACILITY == 1
        # (queue.c). configUSE_TRACE_FACILITY defaults to 0, so describing the
        # field unconditionally would fold every queue as ``type=N/A``.
        queue_fields["type"] = StructField(
            "type",
            ("ucQueueType",),
            kind="enum",
            summary=True,
            enum_map=_QUEUE_TYPE_NAMES,
        )
    structs = {
        "struct tskTaskControlBlock": StructLayout(
            "struct tskTaskControlBlock", fields=tcb_fields, display_name="Task"
        ),
        "struct xLIST": StructLayout(
            "struct xLIST",
            fields={
                "count": StructField("count", ("uxNumberOfItems",), summary=True),
                "index": StructField("index", ("pxIndex",)),
                "end": StructField("end", ("xListEnd",)),
            },
            display_name="List",
        ),
        "struct xLIST_ITEM": StructLayout(
            "struct xLIST_ITEM",
            fields={
                "value": StructField("value", ("xItemValue",), summary=True),
                "next": StructField("next", ("pxNext",)),
                "previous": StructField("previous", ("pxPrevious",)),
                "owner": StructField("owner", ("pvOwner",), kind="ptr", summary=True),
                # Reason: member name follows the probed spelling (pvContainer
                # under the default backward-compat build, pxContainer
                # otherwise); falling back to pxContainer when unknown.
                "container": StructField(
                    "container", (cfg.list_item_container_field or "pxContainer",)
                ),
            },
            display_name="ListItem",
        ),
        "struct xMINI_LIST_ITEM": StructLayout(
            "struct xMINI_LIST_ITEM",
            fields={
                "value": StructField("value", ("xItemValue",), summary=True),
                "next": StructField("next", ("pxNext",)),
                "previous": StructField("previous", ("pxPrevious",)),
            },
            display_name="MiniListItem",
        ),
        "struct QueueDefinition": StructLayout(
            "struct QueueDefinition",
            fields=queue_fields,
            display_name="Queue",
        ),
        "struct tmrTimerControl": StructLayout(
            "struct tmrTimerControl",
            fields={
                "name": StructField(
                    "name", ("pcTimerName",), kind="string", summary=True
                ),
                "period": StructField("period", ("xTimerPeriodInTicks",), summary=True),
                # Detail-only fields for ``frt timer <name>`` (plus the
                # ``number`` member gated below on configUSE_TRACE_FACILITY).
                "id": StructField("id", ("pvTimerID",), kind="ptr"),
                "callback": StructField(
                    "callback", ("pxCallbackFunction",), kind="ptr", summary=True
                ),
                "status": StructField("status", ("ucStatus",)),
                "list_item": StructField("list_item", ("xTimerListItem",)),
            },
            display_name="Timer",
        ),
        "struct EventGroupDef_t": StructLayout(
            "struct EventGroupDef_t",
            fields={
                "value": StructField("value", ("uxEventBits",), summary=True),
                # Detail-only fields for ``frt eventgroup <name>``.  The
                # waiter list is walked the same way the scheduler lists are;
                # ``number``/``static_alloc`` are injected below on their
                # config gates.
                "waiting": StructField("waiting", ("xTasksWaitingForBits",)),
            },
            display_name="EventGroup",
        ),
        "struct StreamBufferDef_t": StructLayout(
            "struct StreamBufferDef_t",
            fields={
                "size": StructField("size", ("xLength",), summary=True),
                # Detail-only geometry for ``frt streambuffer <name>``.
                # Upstream StreamBufferDef_t (stream_buffer.c) carries no
                # byte-count member: bytes/space are derived from the
                # head/tail/trigger offsets by freertos.streams (never via
                # inferior arithmetic).  Waiter fields are a single
                # TaskHandle_t, not a list.
                "head": StructField("head", ("xHead",)),
                "tail": StructField("tail", ("xTail",)),
                "trigger": StructField("trigger", ("xTriggerLevelBytes",)),
                "flags": StructField("flags", ("ucFlags",)),
                "buffer": StructField("buffer", ("pucBuffer",)),
                "recv_waiter": StructField("recv_waiter", ("xTaskWaitingToReceive",)),
                "send_waiter": StructField("send_waiter", ("xTaskWaitingToSend",)),
            },
            display_name="StreamBuffer",
        ),
    }
    lists = {
        "ready": "pxReadyTasksLists",
        "delayed_1": "xDelayedTaskList1",
        "delayed_2": "xDelayedTaskList2",
        "delayed_current": "pxDelayedTaskList",
        "delayed_overflow": "pxOverflowDelayedTaskList",
        "pending": "xPendingReadyList",
        "suspended": "xSuspendedTaskList",
        "termination": "xTasksWaitingTermination",
    }
    if cfg.trace_facility:
        # Reason: uxTimerNumber only exists under configUSE_TRACE_FACILITY
        # (timers.c) and prvInitialiseNewTimer never writes it, so it is not
        # usable as an identifier (only vTimerSetTimerNumber or a trace tool
        # sets it); gating on the same probe as ucQueueType keeps a trace-off
        # build from fabricating a stable-looking timer id.
        structs["struct tmrTimerControl"].fields["number"] = StructField(
            "number", ("uxTimerNumber",)
        )
        # Reason: uxEventGroupNumber / uxStreamBufferNumber only exist under
        # configUSE_TRACE_FACILITY and, like uxTimerNumber, are never written
        # by prvInitialiseNewStreamBuffer / prvINITIALISE_EVENT_GROUP, so they
        # are not usable as identifiers; gating keeps a trace-off build from
        # fabricating a stable-looking object id (timers.c / stream_buffer.c /
        # event_groups.c).
        structs["struct EventGroupDef_t"].fields["number"] = StructField(
            "number", ("uxEventGroupNumber",)
        )
        structs["struct StreamBufferDef_t"].fields["number"] = StructField(
            "number", ("uxStreamBufferNumber",)
        )
    if cfg.static_and_dynamic:
        # Reason: ucStaticallyAllocated only exists when both static and
        # dynamic allocation are possible (FreeRTOS.h tstSTATIC_AND_DYNAMIC_
        # ALLOCATION_POSSIBLE), mirroring the TCB probe in detect_config.
        structs["struct EventGroupDef_t"].fields["static_alloc"] = StructField(
            "static_alloc", ("ucStaticallyAllocated",)
        )
    if cfg.stream_buffer_notification_index:
        # Reason: uxNotificationIndex only exists from V11.1.0 (stream_buffer.c);
        # declaring it unconditionally would fabricate a silent N/A on older
        # kernels instead of surfacing the version gate.
        structs["struct StreamBufferDef_t"].fields["notification_index"] = StructField(
            "notification_index", ("uxNotificationIndex",)
        )
    return FreeRtosLayout(structs=structs, lists=lists, config=cfg, version=version)
