"""DWARF path layouts for FreeRTOS kernel structures."""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import (
    eval_safe,
    lookup_symbol,
    lookup_type,
    macro_defined,
    read_int,
)
from gdr.layout import StructField, StructLayout


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
    entry_field: str | None = None
    trace_facility: bool = False
    queue_registry: bool = False
    timers: bool = False
    static_allocation: bool = False
    mpu_wrapper_v2: bool = False
    tcb_fields: frozenset[str] = frozenset()


def _fields(type_name: str) -> set[str]:
    typ = lookup_type(type_name)
    if typ is None:
        return set()
    try:
        return {field.name for field in typ.strip_typedefs().fields() if field.name}
    except Exception:
        return set()


def _macro_int(name: str) -> int | None:
    return read_int(eval_safe(name))


def detect_config() -> FreeRtosConfig:
    """Probe FreeRTOS configuration from symbols, macros and DWARF fields."""
    fields = _fields("struct tskTaskControlBlock")
    cfg = FreeRtosConfig()
    cfg.tcb_fields = frozenset(fields)
    cfg.smp = lookup_symbol("pxCurrentTCBs") is not None
    if cfg.smp:
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
        except Exception:
            pass
    tick = lookup_type("TickType_t")
    runtime = lookup_type("configRUN_TIME_COUNTER_TYPE")
    if tick is not None:
        cfg.tick_bits = tick.sizeof * 8
    if runtime is not None:
        cfg.runtime_counter_bits = runtime.sizeof * 8
    cfg.mini_list = (
        "xListEnd" in _fields("struct xLIST")
        and lookup_type("struct xMINI_LIST_ITEM") is not None
    )
    cfg.tls_field = next(
        (name for name in ("xTLSBlock", "xNewLib_reent") if name in fields),
        None,
    )
    cfg.stack_end_field = next(
        (name for name in ("pxEndOfStack", "pxStackEnd") if name in fields),
        None,
    )
    cfg.entry_field = next(
        (name for name in ("pxTaskTag", "pxTaskEntry", "pvTaskTag") if name in fields),
        None,
    )
    cfg.trace_facility = lookup_symbol(
        "uxTaskGetSystemState"
    ) is not None or macro_defined("configUSE_TRACE_FACILITY")
    cfg.queue_registry = lookup_symbol("xQueueRegistry") is not None
    cfg.timers = (
        lookup_symbol("xTimerTaskHandle") is not None
        or lookup_symbol("xTimerQueue") is not None
    )
    cfg.static_allocation = (
        macro_defined("configSUPPORT_STATIC_ALLOCATION")
        or lookup_symbol("xIdleTaskTCB") is not None
    )
    cfg.mpu_wrapper_v2 = macro_defined("configUSE_MPU_WRAPPERS_V1")
    return cfg


@dataclass
class FreeRtosLayout:
    structs: dict[str, StructLayout] = field(default_factory=dict)
    lists: dict[str, str] = field(default_factory=dict)
    config: FreeRtosConfig = field(default_factory=FreeRtosConfig)
    version: tuple[int, int, int] | None = None


def _struct(
    name: str, fields: dict[str, tuple[str | int, ...]], display: str
) -> StructLayout:
    result = StructLayout(name, display_name=display)
    for key, path in fields.items():
        result.fields[key] = StructField(key, path)
    return result


def build_layout(
    cfg: FreeRtosConfig, version: tuple[int, int, int] | None = None
) -> FreeRtosLayout:
    tcb_fields = {
        "top_of_stack": ("pxTopOfStack",),
        "state_list_item": ("xStateListItem",),
        "event_list_item": ("xEventListItem",),
        "current_priority": ("uxPriority",),
        "stack_base": ("pxStack",),
        "name": ("pcTaskName",),
    }
    if "uxBasePriority" in cfg.tcb_fields:
        tcb_fields["base_priority"] = ("uxBasePriority",)
    if "ulRunTimeCounter" in cfg.tcb_fields:
        tcb_fields["runtime_counter"] = ("ulRunTimeCounter",)
    if cfg.entry_field:
        tcb_fields["entry"] = (cfg.entry_field,)
    if cfg.stack_end_field:
        tcb_fields["stack_end"] = (cfg.stack_end_field,)
    if cfg.tls_field:
        tcb_fields["tls"] = (cfg.tls_field,)
    if cfg.smp:
        if "xTaskRunState" in cfg.tcb_fields:
            tcb_fields["run_state"] = ("xTaskRunState",)
        if "uxCoreAffinityMask" in cfg.tcb_fields:
            tcb_fields["core_affinity"] = ("uxCoreAffinityMask",)
    structs = {
        "struct tskTaskControlBlock": _struct(
            "struct tskTaskControlBlock", tcb_fields, "Task"
        ),
        "struct xLIST": _struct(
            "struct xLIST",
            {
                "count": ("uxNumberOfItems",),
                "index": ("pxIndex",),
                "end": ("xListEnd",),
            },
            "List",
        ),
        "struct xLIST_ITEM": _struct(
            "struct xLIST_ITEM",
            {
                "value": ("xItemValue",),
                "next": ("pxNext",),
                "previous": ("pxPrevious",),
                "owner": ("pvOwner",),
                "container": ("pxContainer",),
            },
            "ListItem",
        ),
        "struct xMINI_LIST_ITEM": _struct(
            "struct xMINI_LIST_ITEM",
            {
                "value": ("xItemValue",),
                "next": ("pxNext",),
                "previous": ("pxPrevious",),
            },
            "MiniListItem",
        ),
    }
    status = _struct(
        "struct xTASK_STATUS",
        {
            "handle": ("xHandle",),
            "name": ("pcTaskName",),
            "state": ("eCurrentState",),
            "current_priority": ("uxCurrentPriority",),
            "base_priority": ("uxBasePriority",),
            "runtime_counter": ("ulRunTimeCounter",),
            "stack_base": ("pxStackBase",),
            "high_water_mark": ("usStackHighWaterMark",),
        },
        "TaskStatus",
    )
    structs["struct xTASK_STATUS"] = status
    for name, display in (
        ("struct QueueDefinition", "Queue"),
        ("struct tmrTimerControl", "Timer"),
        ("struct EventGroupDef_t", "EventGroup"),
        ("struct StreamBufferDef_t", "StreamBuffer"),
    ):
        structs[name] = StructLayout(name, display_name=display)
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
