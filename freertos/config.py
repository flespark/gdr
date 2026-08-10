"""Best-effort FreeRTOS configuration probes based on GDB debug data."""

from __future__ import annotations

from dataclasses import dataclass

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


def _fields(type_name: str) -> set[str]:
    typ = lookup_type(type_name)
    if typ is None:
        return set()
    try:
        return {f.name for f in typ.strip_typedefs().fields() if f.name}
    except Exception:
        return set()


def _sizeof(type_name: str) -> int | None:
    typ = lookup_type(type_name)
    try:
        return typ.sizeof * 8 if typ is not None else None
    except Exception:
        return None


def _macro_int(name: str) -> int | None:
    # Macro values are not consistently retained by all compilers.  A symbol
    # with the same name is supported for fixture/CI builds.
    value = read_int(eval_safe(name))
    return value if value is not None else None


def detect_config() -> FreeRtosConfig:
    fields = _fields("struct tskTaskControlBlock")
    cfg = FreeRtosConfig()
    cfg.smp = lookup_symbol("pxCurrentTCBs") is not None
    cfg.number_of_cores = 1
    if cfg.smp:
        value = _macro_int("configNUMBER_OF_CORES")
        cfg.number_of_cores = value if value and value > 0 else 2
    cfg.notifications = "ulNotifiedValue" in fields
    ntype = None
    typ = lookup_type("struct tskTaskControlBlock")
    if typ is not None:
        try:
            ntype = next(
                (
                    f.type.strip_typedefs()
                    for f in typ.fields()
                    if f.name == "ulNotifiedValue"
                ),
                None,
            )
        except Exception:
            ntype = None
    if ntype is not None and gdb is not None and ntype.code == gdb.TYPE_CODE_ARRAY:
        cfg.notification_array = True
        cfg.notification_count = ntype.range()[1] + 1
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
        (x for x in ("xTLSBlock", "xNewLib_reent") if x in fields), None
    )
    cfg.stack_end_field = next(
        (x for x in ("pxEndOfStack", "pxStackEnd") if x in fields), None
    )
    cfg.entry_field = next(
        (x for x in ("pxTaskTag", "pxTaskEntry", "pvTaskTag") if x in fields), None
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
