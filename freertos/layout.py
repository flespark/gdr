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
    stack_grows_up: bool | None = False
    stack_end_field: str | None = None
    trace_facility: bool = False
    queue_registry: bool = False
    queue_registry_size: int = 0
    timers: bool = False
    static_allocation: bool = False
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


def _detect_heap_kind() -> int | None:
    """Identify the heap manager (heap_1..heap_5) by its unique symbols.

    Priority chain (each heap has a distinguishing kernel symbol):
    * heap_5 -> ``vPortDefineHeapRegions`` (heap_4's symbols plus regions API)
    * heap_4 -> ``pxEnd`` (a ``BlockLink_t *`` pointer; heap_4/5 only)
    * heap_1 -> ``xNextFreeByte`` (sole BlockLink-free allocator)
    * heap_2 -> ``xStart`` and ``xEnd`` both as ``BlockLink_t`` values
    * else ``None`` (heap_3 wraps ``malloc`` and exports no kernel heap symbol)
    """
    if symbol_exists("vPortDefineHeapRegions"):
        return 5
    if symbol_exists("pxEnd"):
        return 4
    if symbol_exists("xNextFreeByte"):
        return 1
    if symbol_exists("xStart") and symbol_exists("xEnd"):
        return 2
    return None


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
    cfg.stack_end_field = next(
        (name for name in ("pxEndOfStack", "pxStackEnd") if name in fields),
        None,
    )

    # Reason: trace facility (queue type classification) is proven by the
    # ucQueueType member of struct QueueDefinition, not by a macro guard.
    queue_fields = _fields("struct QueueDefinition")
    cfg.trace_facility = "ucQueueType" in queue_fields
    cfg.queue_sets = "pxQueueSetContainer" in queue_fields
    cfg.queue_registry = lookup_symbol("xQueueRegistry") is not None
    cfg.queue_registry_size = _array_bound("xQueueRegistry") or 0
    cfg.timers = (
        lookup_symbol("xTimerTaskHandle") is not None
        or lookup_symbol("xTimerQueue") is not None
    )
    # Reason: ucStaticallyAllocated (tskSTATIC_AND_DYNAMIC_ALLOCATION_POSSIBLE)
    # is the reliable probe; xIdleTaskTCB is function-local and often absent.
    cfg.static_allocation = (
        "ucStaticallyAllocated" in fields or lookup_symbol("xIdleTaskTCB") is not None
    )

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
# the pretty-printed fold.
_QUEUE_TYPE_NAMES: dict[int, str] = {
    0: "queue",
    1: "mutex",
    2: "counting-sem",
    3: "binary-sem",
    4: "recursive-mutex",
}


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
                "container": StructField("container", ("pxContainer",)),
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
            },
            display_name="Timer",
        ),
        "struct EventGroupDef_t": StructLayout(
            "struct EventGroupDef_t",
            fields={
                "value": StructField("value", ("uxEventBits",), summary=True),
            },
            display_name="EventGroup",
        ),
        "struct StreamBufferDef_t": StructLayout(
            "struct StreamBufferDef_t",
            fields={
                "size": StructField("size", ("xLength",), summary=True),
                # Reason: upstream StreamBufferDef_t (stream_buffer.c) has no
                # item/byte-count field; xHead/xTail are byte offsets whose
                # difference is not a DWARF field (computing it would need
                # inferior arithmetic, which is forbidden). Keep only the
                # buffer length as the one-line summary.
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
    return FreeRtosLayout(structs=structs, lists=lists, config=cfg, version=version)
