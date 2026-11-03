"""DWARF path layouts for FreeRTOS kernel structures."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

import re

from core.gdb_bridge import (
    array_bound,
    get_arch_info,
    loaded_object_path,
    lookup_symbol,
    lookup_type,
    read_int,
    read_macro_int,
    read_macro_text,
    read_macro_text_in_source,
    symbol_exists,
    type_field_names,
    type_size,
    warn,
)
from core.layout import StructField, StructLayout
from core.version import (
    ProbeCandidate,
    TargetVerdict,
    Version,
    VersionRange,
    decode_version,
    format_version,
    parse_version,
    version_in_ranges,
)

# DWARF probe failures we degrade from during config detection.  Module scope
# keeps the tuple computable outside GDB (where ``gdb`` is ``None``); anything
# outside this set bubbles to the ``gdr init`` command guard for a diagnostic.
if gdb is not None:
    _PROBE_ERRORS = (gdb.error, TypeError, ValueError, AttributeError)
else:
    _PROBE_ERRORS = (TypeError, ValueError, AttributeError)

# NOTE: ``gdr probe`` reports presence before any layout exists, so the marker
# names stay here with the rest of the kernel symbol table instead of being
# re-spelled by a consumer (the boundary test enforces one lookup table per
# domain).
PROBE_MARKERS: tuple[str, ...] = (
    "pxCurrentTCB",
    "pxCurrentTCBs",
    "uxCurrentNumberOfTasks",
    "xSchedulerRunning",
)

# The SMP-era TCB field and the multicore handle array that narrow the layout
# era when the kernel's version macros are absent from the debug info.
SMP_ERA_FIELD = "xTaskRunState"
SMP_ERA_SYMBOL = "pxCurrentTCBs"

# The port the image was built for, read from its symbols (see freertos/stackpc.py).
# RISC-V's port has xPortStartFirstTask; Arm M-profile ports have PendSV_Handler,
# which Armv6-M, Armv7-M, and Armv8-M Mainline share, so ELF build attributes
# tell them apart.
PORT_RISCV_SYMBOL = "xPortStartFirstTask"
# Runtime symbol retained in the RISC-V port image even when portASM.S has no
# DWARF; it is the fallback port evidence for the 32-bit lane.
PORT_RISCV_TIMER_SYMBOL = "ullNextTime"
PORT_PENDSV_SYMBOL = "PendSV_Handler"


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
    queue_support: bool = False
    stack_word_bytes: int = 4
    task_attributes: bool = False
    preemption_disable: bool = False
    critical_nesting_in_tcb: bool = False
    posix_errno: bool = False
    delay_abort: bool = False
    mpu_object_pool: bool = False
    mpu_context: bool = False
    heap_kind: int | None = None
    heap_protector: bool = False
    tcb_fields: frozenset[str] = frozenset()


def _fields(type_name: str) -> set[str]:
    return set(type_field_names(type_name))


def _macro_int(name: str) -> int | None:
    return read_macro_int(name)


def _array_bound(name: str) -> int | None:
    """Return the element count of an array symbol, or ``None``.

    GDB's ``Type.range()`` yields an inclusive upper bound, so the array
    length is ``range()[1] + 1``; ``None`` when the symbol is missing or its
    type is not a decodable array (including an unbounded ``extern T
    sym[]`` declaration, whose ``(0, -1)`` range means unknown, not a real
    zero-length array).  The bounds decode lives in the bridge
    (:func:`core.gdb_bridge.array_bound`); this thin name is kept because
    config probing reads symbols by name.
    """
    value = lookup_symbol(name)
    if value is None:
        return None
    return array_bound(value)


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
            end_fields = type_field_names(member.type)
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
    # NOTE: heap_2's xEnd is a BlockLink_t *value*; heap_4/5 use pxEnd as a
    # pointer and must not also match heap_2 when a test fixture (or a
    # corrupted symbol table) happens to expose both names.
    if has_x_start and has_x_end and has_block_link and not has_px_end:
        matches.append(2)
    # NOTE: heap_5.c reuses heap_4's xStart/pxEnd plus vPortDefineHeapRegions,
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
        # NOTE: pxCurrentTCBs[TASK_CORE_ID] bound is authoritative for the
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
                count = array_bound(notification)
                if count is not None:
                    cfg.notification_count = count
        except _PROBE_ERRORS:
            pass
    tick = lookup_type("TickType_t")
    runtime = lookup_type("configRUN_TIME_COUNTER_TYPE")
    tick_size = type_size(tick) if tick is not None else None
    runtime_size = type_size(runtime) if runtime is not None else None
    if tick_size is not None:
        cfg.tick_bits = tick_size * 8
    if runtime_size is not None:
        cfg.runtime_counter_bits = runtime_size * 8

    cfg.mini_list = _mini_list_detected()
    cfg.tls_field = next(
        (name for name in ("xTLSBlock", "xNewLib_reent") if name in fields),
        None,
    )
    # NOTE: configENABLE_BACKWARD_COMPATIBILITY defaults to 1, which
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

    # --- queue family (type / sets / support) -----------------------
    # NOTE: trace facility (queue type classification) is proven by the
    # ucQueueType member. Some GDB/DWARF spellings expose the struct tag
    # (``struct QueueDefinition``) while others only expose the typedef
    # (``xQUEUE``); ``_fields`` already strip_typedefs() so either works.
    queue_fields = _fields("struct QueueDefinition") or _fields("xQUEUE")
    cfg.trace_facility = "ucQueueType" in queue_fields
    cfg.queue_sets = "pxQueueSetContainer" in queue_fields
    # NOTE: the queue family is only offered when the kernel actually
    # compiles Queue_t (queue.c); some GDB/DWARF spellings expose the struct
    # tagged ``QueueDefinition`` while others only the typedef ``xQUEUE``,
    # and ``_fields`` already strip_typedefs so either spelling counts.
    cfg.queue_support = (
        lookup_type("struct QueueDefinition") is not None
        or lookup_type("xQUEUE") is not None
    )
    # --- stack width -------------------------------------------------
    # NOTE: StackType_t is a port typedef (uint32_t on ARMv7-M, uint64_t on
    # RV64); the watermark scan needs the stack word width, which comes from
    # DWARF or falls back to the target pointer width.
    stack_type = lookup_type("StackType_t")
    if stack_type is not None:
        stack_bytes = type_size(stack_type)
        cfg.stack_word_bytes = stack_bytes if stack_bytes is not None else 4
    else:
        # NOTE: get_arch_info raises outside GDB (unit tests mock it); on
        # a live target an unreadable probe bubbles to the guard.  The
        # fallback keeps the previously probed width (default 4).
        stack_arch = get_arch_info()
        if stack_arch is not None and stack_arch.ptrsize in (4, 8):
            cfg.stack_word_bytes = stack_arch.ptrsize
    # --- registry / timers / allocation -----------------------------
    cfg.queue_registry = lookup_symbol("xQueueRegistry") is not None
    cfg.queue_registry_size = _array_bound("xQueueRegistry") or 0
    cfg.timers = (
        lookup_symbol("xTimerTaskHandle") is not None
        or lookup_symbol("xTimerQueue") is not None
    )
    # NOTE: tskSTATIC_AND_DYNAMIC_ALLOCATION_POSSIBLE (FreeRTOS.h) is what
    # actually emits ucStaticallyAllocated; that is only true when *both*
    # static and dynamic allocation are possible. static-only builds still
    # export xTaskCreateStatic but have no TCB marker, so the two facts are
    # probed separately.
    cfg.static_allocation = symbol_exists("xTaskCreateStatic")
    cfg.static_and_dynamic = "ucStaticallyAllocated" in fields

    # --- heap & object kinds ----------------------------------------
    cfg.mpu_object_pool = symbol_exists("xKernelObjectPool")
    cfg.mpu_context = cfg.mpu_object_pool and "xMPUSettings" in fields
    cfg.heap_kind = _detect_heap_kind()
    cfg.heap_protector = symbol_exists("xHeapCanary")

    cfg.event_groups = lookup_type("struct EventGroupDef_t") is not None
    stream_fields = _fields("struct StreamBufferDef_t")
    cfg.stream_buffers = lookup_type("struct StreamBufferDef_t") is not None
    cfg.stream_buffer_notification_index = "uxNotificationIndex" in stream_fields

    # --- list integrity / task attributes ---------------------------
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
    # Internal kernel structs used only for field reads (daemon-queue
    # message, queue-registry item, MPU pool slot).  Kept out of ``structs``
    # so the pretty-printer map (``SupportsStructs.structs``) stays exactly
    # the user-visible surface: these types never carry summary fields and
    # are never part of a folded display.
    internal_structs: dict[str, StructLayout] = field(default_factory=dict)
    lists: dict[str, str] = field(default_factory=dict)
    # Kernel global scalars/handles referenced across the adapter, keyed by
    # logical name (``layout.symbols["tick"]`` -> "xTickCount").  One place
    # to review when the kernel renames a global; consumers never spell raw
    # target symbols.
    symbols: dict[str, str] = field(default_factory=dict)
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


# Semantic kind -> layout struct key used to cast a discovered address back
# to a native gdb.Value.  Semaphores and mutexes are QueueDefinition structs;
# owned by layout.py because it maps semantic kind to the ABI struct.
STRUCT_BY_KIND: dict[str, str] = {
    "task": "struct tskTaskControlBlock",
    "queue": "struct QueueDefinition",
    "semaphore": "struct QueueDefinition",
    "mutex": "struct QueueDefinition",
    "timer": "struct tmrTimerControl",
    "eventgroup": "struct EventGroupDef_t",
    "streambuffer": "struct StreamBufferDef_t",
}


# Kinds in display order for the object summary.
OBJECT_KIND_ORDER: tuple[str, ...] = (
    "task",
    "queue",
    "semaphore",
    "mutex",
    "timer",
    "eventgroup",
    "streambuffer",
)


# Static-buffer typedef names (include/FreeRTOS.h) -> semantic kind.  GDB
# keeps the declared typedef spelling in the symbol's type name, so a
# ``static StaticSemaphore_t`` variable stays distinguishable from
# ``StaticQueue_t`` even though both strip to the same struct.
KIND_BY_STATIC_TYPE: dict[str, str] = {
    "StaticTask_t": "task",
    "StaticQueue_t": "queue",
    "StaticSemaphore_t": "semaphore",
    "StaticEventGroup_t": "eventgroup",
    "StaticTimer_t": "timer",
    "StaticStreamBuffer_t": "streambuffer",
    "StaticMessageBuffer_t": "streambuffer",
}


# Handle typedef names (task.h/queue.h/semphr.h/timers.h/event_groups.h/
# stream_buffer.h/message_buffer.h) -> semantic kind.  Queue-set handles are
# queues themselves and count as ``queue``.
KIND_BY_HANDLE_TYPE: dict[str, str] = {
    "TaskHandle_t": "task",
    "QueueHandle_t": "queue",
    "QueueSetHandle_t": "queue",
    "QueueSetMemberHandle_t": "queue",
    "SemaphoreHandle_t": "semaphore",
    "TimerHandle_t": "timer",
    "EventGroupHandle_t": "eventgroup",
    "StreamBufferHandle_t": "streambuffer",
    "MessageBufferHandle_t": "streambuffer",
}


# ulKernelObjectType values (portable/Common/mpu_wrappers_v2.c).
MPU_KIND_BY_TYPE: dict[int, str] = {
    1: "queue",  # also covers semaphore/mutex/queue-set/set-member
    2: "task",
    3: "streambuffer",
    4: "eventgroup",
    5: "timer",
}


def build_layout(
    cfg: FreeRtosConfig, version: tuple[int, int, int] | None = None
) -> FreeRtosLayout:
    # --- TCB fields (config-gated) ---------------------------------
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
    if cfg.mpu_context:
        tcb_fields["mpu_context"] = StructField(
            "mpu_context", ("xMPUSettings", "ulContext")
        )
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
    # --- Queue fields (config-gated) -------------------------------
    queue_fields: dict[str, StructField] = {
        "length": StructField("length", ("uxLength",), summary=True),
        "count": StructField("count", ("uxMessagesWaiting",), summary=True),
        # Detail-only fields for the queue family.  The storage-window and
        # lock fields feed ``frt queue/semaphore/mutex <name>`` and the
        # consistency checks; the ``u`` union arms are read through DWARF so
        # a semaphore's QueuePointers_t ``u`` is never misread as
        # SemaphoreData_t (queue.c) -- the adapter still gates the mutex
        # arms on the discriminated kind.
        "item_size": StructField("item_size", ("uxItemSize",)),
        "head": StructField("head", ("pcHead",), kind="ptr"),
        "write_to": StructField("write_to", ("pcWriteTo",), kind="ptr"),
        "tail": StructField("tail", ("u", "xQueue", "pcTail"), kind="ptr"),
        "read_from": StructField(
            "read_from", ("u", "xQueue", "pcReadFrom"), kind="ptr"
        ),
        "rx_lock": StructField("rx_lock", ("cRxLock",)),
        "tx_lock": StructField("tx_lock", ("cTxLock",)),
        "send_waiters": StructField("send_waiters", ("xTasksWaitingToSend",)),
        "recv_waiters": StructField("recv_waiters", ("xTasksWaitingToReceive",)),
        "mutex_holder": StructField(
            "mutex_holder", ("u", "xSemaphore", "xMutexHolder"), kind="ptr"
        ),
        "recursive_count": StructField(
            "recursive_count", ("u", "xSemaphore", "uxRecursiveCallCount")
        ),
    }
    if cfg.queue_sets:
        # NOTE: pxQueueSetContainer only exists under
        # configUSE_QUEUE_SETS (queue.h); describing it unconditionally
        # would fabricate a Set column on builds without queue sets.
        queue_fields["set_container"] = StructField(
            "set_container", ("pxQueueSetContainer",), kind="ptr"
        )
    if cfg.trace_facility:
        # NOTE: ucQueueType only exists under configUSE_TRACE_FACILITY == 1
        # (queue.c). configUSE_TRACE_FACILITY defaults to 0, so describing the
        # field unconditionally would fold every queue as ``type=N/A``.
        queue_fields["type"] = StructField(
            "type",
            ("ucQueueType",),
            kind="enum",
            summary=True,
            enum_map=_QUEUE_TYPE_NAMES,
        )
    # --- struct map (pretty-printer surface) -----------------------
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
                # NOTE: member name follows the probed spelling (pvContainer
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
                # The list-item membership fields are declared as nested
                # paths here (composing through the ListItem layout): the
                # container member name follows the probed spelling
                # (pvContainer under the default backward-compat build,
                # pxContainer otherwise), so the probe lives only here.
                "expiry": StructField("expiry", ("xTimerListItem", "xItemValue")),
                "owner": StructField(
                    "owner",
                    ("xTimerListItem", "pvOwner"),
                    kind="ptr",
                ),
                "container": StructField(
                    "container",
                    ("xTimerListItem", cfg.list_item_container_field or "pxContainer"),
                ),
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
    # Kernel global scalars/handles (the scheduler lists live in ``lists``).
    symbols = {
        "tick": "xTickCount",
        "scheduler_running": "xSchedulerRunning",
        "task_count": "uxCurrentNumberOfTasks",
        "scheduler_suspended": "uxSchedulerSuspended",
        "next_unblock_time": "xNextTaskUnblockTime",
        "current_tcb": "pxCurrentTCB",
        "current_tcbs": "pxCurrentTCBs",
        "idle_handle": "xIdleTaskHandle",
        "idle_handles": "xIdleTaskHandles",
        "total_runtime": "ulTotalRunTime",
        "timer_list_current": "pxCurrentTimerList",
        "timer_list_overflow": "pxOverflowTimerList",
        "timer_list_1": "xActiveTimerList1",
        "timer_list_2": "xActiveTimerList2",
        "timer_queue": "xTimerQueue",
        "timer_task": "xTimerTaskHandle",
        "queue_registry": "xQueueRegistry",
        "mpu_pool": "xKernelObjectPool",
    }
    # Internal kernel structs consumed for field reads only (never folded by
    # the pretty-printer).  The daemon-message union arms are read through
    # DWARF, so a build without INCLUDE_xTimerPendFunctionCall degrades a
    # callback-arm read to None (same as the current DWARF probe).
    internal_structs: dict[str, StructLayout] = {
        "struct tmrTimerQueueMessage": StructLayout(
            "struct tmrTimerQueueMessage",
            fields={
                "message_id": StructField("message_id", ("xMessageID",)),
                "timer": StructField(
                    "timer", ("u", "xTimerParameters", "pxTimer"), kind="ptr"
                ),
                "message_value": StructField(
                    "message_value", ("u", "xTimerParameters", "xMessageValue")
                ),
                "callback_fn": StructField(
                    "callback_fn",
                    ("u", "xCallbackParameters", "pxCallbackFunction"),
                    kind="ptr",
                ),
                "callback_p1": StructField(
                    "callback_p1",
                    ("u", "xCallbackParameters", "pvParameter1"),
                    kind="ptr",
                ),
                "callback_p2": StructField(
                    "callback_p2", ("u", "xCallbackParameters", "ulParameter2")
                ),
            },
        ),
    }
    # heap_2/4/5 free-list node (typedef BlockLink_t struct A_BLOCK_LINK);
    # part of the heap manager's ABI, described here (not in heap.py) so the
    # heap walks resolve offsets through DWARF like every other kernel struct.
    internal_structs["struct A_BLOCK_LINK"] = StructLayout(
        "struct A_BLOCK_LINK",
        fields={
            "next_free": StructField("next_free", ("pxNextFreeBlock",), kind="ptr"),
            "block_size": StructField("block_size", ("xBlockSize",)),
        },
    )
    if cfg.queue_registry:
        # queue.c: typedef struct QUEUE_REGISTRY_ITEM { const char *
        # pcQueueName; QueueHandle_t xHandle; } xQueueRegistryItem;
        internal_structs["struct QUEUE_REGISTRY_ITEM"] = StructLayout(
            "struct QUEUE_REGISTRY_ITEM",
            fields={
                "name": StructField("name", ("pcQueueName",), kind="string"),
                "handle": StructField("handle", ("xHandle",), kind="ptr"),
            },
        )
    if cfg.mpu_object_pool:
        # mpu_wrappers_v2.c: typedef struct KernelObject { OpaqueObjectHandle_t
        # xInternalObjectHandle; uint32_t ulKernelObjectType; void *
        # pvKernelObjectData; } KernelObject_t;
        internal_structs["struct KernelObject"] = StructLayout(
            "struct KernelObject",
            fields={
                "internal_handle": StructField(
                    "internal_handle", ("xInternalObjectHandle",)
                ),
                "type": StructField("type", ("ulKernelObjectType",)),
                "data": StructField("data", ("pvKernelObjectData",), kind="ptr"),
            },
        )
    if cfg.trace_facility:
        # NOTE: uxTimerNumber only exists under configUSE_TRACE_FACILITY
        # (timers.c) and prvInitialiseNewTimer never writes it, so it is not
        # usable as an identifier (only vTimerSetTimerNumber or a trace tool
        # sets it); gating on the same probe as ucQueueType keeps a trace-off
        # build from fabricating a stable-looking timer id.
        structs["struct tmrTimerControl"].fields["number"] = StructField(
            "number", ("uxTimerNumber",)
        )
        # NOTE: uxEventGroupNumber / uxStreamBufferNumber only exist under
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
        # NOTE: ucStaticallyAllocated only exists when both static and
        # dynamic allocation are possible (FreeRTOS.h tstSTATIC_AND_DYNAMIC_
        # ALLOCATION_POSSIBLE), mirroring the TCB probe in detect_config.
        structs["struct EventGroupDef_t"].fields["static_alloc"] = StructField(
            "static_alloc", ("ucStaticallyAllocated",)
        )
    if cfg.stream_buffer_notification_index:
        # NOTE: uxNotificationIndex only exists from V11.1.0 (stream_buffer.c);
        # declaring it unconditionally would fabricate a silent N/A on older
        # kernels instead of surfacing the version gate.
        structs["struct StreamBufferDef_t"].fields["notification_index"] = StructField(
            "notification_index", ("uxNotificationIndex",)
        )
    return FreeRtosLayout(
        structs=structs,
        internal_structs=internal_structs,
        lists=lists,
        symbols=symbols,
        config=cfg,
        version=version,
    )


# ---------------------------------------------------------------------------
# Version policy and target-version probing.
# ---------------------------------------------------------------------------
# NOTE: V11.3.1 introduced ARMv8-M SMP support (History.txt, V11.3.1), and
# the six kernel header structs are identical between V11.2.0 and V11.3.1, so
# the public range extends through 11.3.x without new layout branches.
SUPPORTED_RANGES: tuple[VersionRange, ...] = (((10, 3, 0), (11, 3, 99)),)

# Matches the tskKERNEL_VERSION_NUMBER string macro, e.g. "V10.3.1" or
# "V11.1.0+": an optional leading V, three decimal components, and an
# optional trailing '+' (dirty builds).
_VERSION_STRING_RE = re.compile(r"^V?(\d+)\.(\d+)\.(\d+)\+?$")

# The integer macro triplet that FreeRTOS-Kernel exports (task.h).
_VERSION_TRIPLET = (
    "tskKERNEL_VERSION_MAJOR",
    "tskKERNEL_VERSION_MINOR",
    "tskKERNEL_VERSION_BUILD",
)

# Every source ``detect_target_version`` consults, in priority order.  The probe
# reports them as the evidence when no version could be read, so the list stays
# here rather than being re-spelled by a consumer.
VERSION_SOURCES: tuple[str, ...] = (
    *_VERSION_TRIPLET,
    "tskKERNEL_VERSION_NUMBER",
    "gdr_freertos_version_num",
)


def _range_text() -> str:
    return ", ".join(
        f"{format_version(lower)}-{format_version(upper)}"
        for lower, upper in SUPPORTED_RANGES
    )


def validate_version(value: str) -> Version | None:
    """Validate the version argument; ``None`` (already warned) on failure.

    Callers abort the init on ``None`` instead of raising ``SystemExit``,
    which would kill the whole GDB session on a typo'd version.
    """
    parsed = parse_version(value)
    if parsed is None:
        warn(f"invalid FreeRTOS version: {value!r}; expected X.Y.Z")
        return None
    if not version_in_ranges(parsed, SUPPORTED_RANGES):
        warn(f"unsupported FreeRTOS version: {value!r}")
        warn(f"supported public ranges: {_range_text()}")
        return None
    return parsed


def _macro_int_in_kernel_cu(name: str) -> int | None:
    """Read an integer macro from the FreeRTOS kernel compilation unit."""
    value = read_macro_int(name)
    if value is not None:
        return value
    text = read_macro_text_in_source(name, "vTaskStartScheduler")
    if text is None:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _macro_triplet() -> Version | None:
    """Read the tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD integer macros.

    The first attempt uses the current CU. If the selected source is outside
    the kernel, the fallback temporarily lists ``vTaskStartScheduler`` to
    select a kernel CU, reads the macro, and restores the user's source view.
    """
    components: list[int] = []
    for name in _VERSION_TRIPLET:
        value = _macro_int_in_kernel_cu(name)
        if value is None:
            return None
        components.append(value)
    return (components[0], components[1], components[2])


def _version_number_string() -> Version | None:
    """Parse the tskKERNEL_VERSION_NUMBER string macro, or ``None``."""
    text = read_macro_text("tskKERNEL_VERSION_NUMBER")
    if text is None:
        text = read_macro_text_in_source(
            "tskKERNEL_VERSION_NUMBER", "vTaskStartScheduler"
        )
    if text is None:
        return None
    match = _VERSION_STRING_RE.match(text.strip())
    if match is None:
        return None
    # NOTE: the regex normally guarantees decimal groups, but keep target
    # macro text untrusted at this GDB boundary.
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def detect_target_version() -> Version | None:
    """Best-effort FreeRTOS version detection from exported constants.

    Priority:
    1. the ``tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD`` integer-macro triplet;
    2. the ``tskKERNEL_VERSION_NUMBER`` string macro (``"V10.3.1"`` / ``+``);
    3. the project-injected ``gdr_freertos_version_num`` symbol
       (decimal or packed-hex encoding).

    The upstream kernel exports no single numeric version symbol, so the
    ``gdr_freertos_version_num`` fallback exists for builds that link a
    helper symbol instead of relying on macro debug info.
    """
    triplet = _macro_triplet()
    if triplet is not None:
        return triplet
    string_version = _version_number_string()
    if string_version is not None:
        return string_version
    numeric = read_int(lookup_symbol("gdr_freertos_version_num"))
    if numeric is None:
        return None
    return decode_version(numeric, ("decimal", "packed-hex"), SUPPORTED_RANGES)


def check_target(value: str, *, allow_unverified: bool = False) -> TargetVerdict:
    """Decide whether *value* may register this adapter on this target.

    The presence markers are ``layout.PROBE_MARKERS``, the same table
    ``gdr probe`` matches, so the probe and this gate cannot disagree about
    what counts as evidence for "this target runs FreeRTOS".

    Refused when the argument is invalid or unsupported, when the target
    exports a different version, or when no marker matched at all -- the last
    one keeps a symbol-less or wrong-target session from registering an adapter
    whose every read would be empty. *allow_unverified* is the escape hatch for
    a deliberately modified kernel, and it records the override as the verdict's
    note rather than hiding it. Markers without an exported version are
    *accepted* with a note: the user asserted the version, which is the flow the
    plugin's detection wizard needs.
    """
    expected = validate_version(value)
    if expected is None:
        return TargetVerdict(
            code="unsupported_version",
            message=f"invalid or unsupported FreeRTOS version: {value!r}",
        )
    markers = tuple(name for name in PROBE_MARKERS if symbol_exists(name))
    actual = detect_target_version()
    if actual is not None and actual != expected:
        message = (
            f"FreeRTOS version mismatch: requested {value}, "
            f"but target exports {format_version(actual)}"
        )
        warn(message)
        return TargetVerdict(code="version_mismatch", message=message)
    if not markers:
        if not allow_unverified:
            # NOTE: same `load`-without-symbols trap as the RT-Thread gate
            # (see rtthread.version.check_target): name `file` only when GDB
            # really has no symbol file loaded.
            if loaded_object_path() is None:
                message = (
                    "no FreeRTOS symbol matched on this target; GDB has no "
                    "symbol file loaded -- run `file <elf>` first (`load` "
                    "only writes the target memory), or pass "
                    "--allow-unverified to register anyway"
                )
            else:
                message = (
                    "no FreeRTOS symbol matched on this target; load the image "
                    "first, or pass --allow-unverified to register anyway"
                )
            warn(message)
            return TargetVerdict(code="no_target_evidence", message=message)
        note = (
            "no FreeRTOS symbol matched; registration was forced with "
            "--allow-unverified"
        )
        warn(note)
        return TargetVerdict(version=expected, note=note, markers=markers)
    if actual is None:
        note = (
            "target FreeRTOS version is not exported; the requested version is "
            "unverified"
        )
        warn(note)
        return TargetVerdict(version=expected, note=note, markers=markers)
    return TargetVerdict(version=expected, markers=markers)


# ---------------------------------------------------------------------------
# Presence and version evidence for ``gdr probe``.
# ---------------------------------------------------------------------------
def probe() -> ProbeCandidate | None:
    """Report whether this target runs FreeRTOS, and which version it exports.

    ``None`` means no marker matched, i.e. this adapter has nothing to claim.
    """
    matched = tuple(name for name in PROBE_MARKERS if symbol_exists(name))
    if not matched:
        return None

    evidence = [f"symbol: {name}" for name in matched]
    version = detect_target_version()
    if version is not None:
        evidence.append(
            f"version: {format_version(version)} from {'/'.join(VERSION_SOURCES)}"
        )
        return ProbeCandidate(
            rtos="freertos",
            confidence="exact",
            version=format_version(version),
            evidence=tuple(evidence),
        )

    evidence.append("version: none of " + ", ".join(VERSION_SOURCES) + " is readable")
    smp_era = symbol_exists(SMP_ERA_SYMBOL) or SMP_ERA_FIELD in type_field_names(
        "struct tskTaskControlBlock"
    )
    if smp_era:
        evidence.append(f"layout: {SMP_ERA_FIELD}/{SMP_ERA_SYMBOL} present")
        era = "the V11 SMP-era layout"
    else:
        evidence.append(f"layout: {SMP_ERA_FIELD} absent")
        era = "the pre-SMP V10.x layout"
    return ProbeCandidate(
        rtos="freertos",
        confidence="range",
        evidence=tuple(evidence),
        detail=(
            f"FreeRTOS is present but exports no version macro; the TCB matches "
            f"{era}, so the exact version must be confirmed by the user. "
            "Building with -g3 makes the version macros available"
        ),
    )
