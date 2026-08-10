"""DWARF path layouts for FreeRTOS kernel structures."""

from __future__ import annotations

from dataclasses import dataclass, field

from freertos.config import FreeRtosConfig
from gdr.layout import KernelLayout, StructField, StructLayout


@dataclass
class FreeRtosLayout:
    structs: dict[str, StructLayout] = field(default_factory=dict)
    lists: dict[str, str] = field(default_factory=dict)
    config: FreeRtosConfig = field(default_factory=FreeRtosConfig)
    version: tuple[int, int, int] | None = None

    def as_kernel_layout(self) -> KernelLayout:
        return KernelLayout(structs=self.structs, list_hooks={})


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
        "base_priority": ("uxBasePriority",),
        "runtime_counter": ("ulRunTimeCounter",),
    }
    if cfg.entry_field:
        tcb_fields["entry"] = (cfg.entry_field,)
    if cfg.stack_end_field:
        tcb_fields["stack_end"] = (cfg.stack_end_field,)
    if cfg.tls_field:
        tcb_fields["tls"] = (cfg.tls_field,)
    if cfg.smp:
        tcb_fields.update(
            {"run_state": ("xTaskRunState",), "core_affinity": ("uxCoreAffinityMask",)}
        )
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
