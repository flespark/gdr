"""FreeRTOS task conversion and GDB convenience functions."""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.details import task_detail
from freertos.layout import FreeRtosLayout
from freertos.navigation import (
    is_idle_task,
    iter_tasks,
    list_count,
    system_value,
    task_state,
)
from gdr.adapter_api import (
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
)
from gdr.formatting import format_address, format_optional_int
from gdr.gdb_bridge import (
    get_arch_info,
    read_bytes,
    read_cstring,
    read_int,
    value_address,
)
from gdr.layout import read_field


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
    high_water_mark: int | None = None
    runtime_counter: int | None = None
    core: int | None = None
    core_affinity: int | None = None
    # Phase 1 detail-only fields (frt task <name>); absent members stay None.
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


def _ptr(value) -> int:
    return read_int(value) or 0


def _stack_type_size() -> int:
    """Return ``sizeof(StackType_t)`` in bytes for the current target.

    ``StackType_t`` is a port typedef (``uint32_t`` on ARMv7-M, ``uint64_t`` on
    RV64), so the value must come from DWARF. When the typedef is absent the
    fallback is the *target's* pointer width -- every upstream port defines
    ``portSTACK_TYPE`` at the machine word width -- never a host-side guess.
    """
    if gdb is None:
        return 4
    try:
        typ = gdb.lookup_type("StackType_t")
        if typ is not None:
            return int(typ.sizeof)
    except Exception:
        pass
    arch = get_arch_info()
    return arch.ptrsize if arch is not None else 4


def _count_fill(stack: bytes, from_high_end: bool) -> int:
    """Count leading (or trailing) ``tskSTACK_FILL_BYTE`` bytes."""
    order = reversed(stack) if from_high_end else stack
    count = 0
    for byte in order:
        if byte != 0xA5:
            break
        count += 1
    return count


def _high_water_mark(stack: bytes | None, layout: FreeRtosLayout) -> int | None:
    """Count untouched ``0xa5`` fill bytes at the low end of a stack.

    Stacks are prefilled with ``tskSTACK_FILL_BYTE`` (0xa5) under the
    watermarking macros; the high-water mark is the number of words that were
    never overwritten. Returns ``None`` when the stack was never filled (the
    first byte is not 0xa5) or the raw read failed, which the renderer reports
    as ``unavailable`` rather than a fabricated zero.
    """
    if stack is None or not stack:
        return None
    # Reason: on grow-up stacks the untouched fill sits at the high end.
    from_high_end = bool(layout.config.stack_grows_up)
    edge = stack[-1] if from_high_end else stack[0]
    if edge != 0xA5:
        return None
    return _count_fill(stack, from_high_end) // _stack_type_size()


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
    top = _ptr(read_field(value, sl, "top_of_stack"))
    base = _ptr(read_field(value, sl, "stack_base"))
    end = _ptr(read_field(value, sl, "stack_end"))
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
    if water_size is not None and water_base:
        # Reason: when the whole window is untouched fill (all 0xa5) the count
        # reports the full window's word count. That is intentionally
        # conservative -- it never overstates free stack beyond what is visible
        # in the startup window -- which is exactly the direction a debugger
        # must prefer over a fabricated larger number.
        raw = read_bytes(water_base, water_size)
        high = _high_water_mark(raw, layout)
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


class FreeRtosAdapter(RtosAdapter):
    """Expose FreeRTOS scheduler lists through the shared task contract."""

    def __init__(self, layout: FreeRtosLayout) -> None:
        self.layout = layout

    def find_task(self, name: str) -> gdb.Value | None:
        return find_task(name, self.layout)

    def find_object(self, kind: str, name: str) -> gdb.Value | None:  # noqa: ARG002
        # Queue Registry and active-timer traversal are not implemented.
        return None

    def object_counts(self) -> dict[str, int]:
        # Reason: count through the same converted-task snapshot used by the
        # system summary so both routes share one scheduler traversal contract.
        return {"task": len(list(iter_converted_tasks(self.layout)))}

    def object_table(self, kind: str) -> ObjectTable | None:  # noqa: ARG002
        return None

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:
        """Return one object's detail; only ``task`` is enumerable in Phase 1."""
        if kind.strip().lower() == "task":
            value = find_task(name, self.layout)
            if value is None:
                return ObjectDetail(found=False)
            task = value_to_task(value, *task_state(value, self.layout), self.layout)
            return ObjectDetail(pairs=task_detail(task, self.layout))
        return None  # queue/timer/etc detail arrives with later phases

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
                    else "unavailable"
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
        scheduler = system_value("xSchedulerRunning")
        total = system_value("uxCurrentNumberOfTasks")
        return SystemSummary(
            kernel_version=(
                ".".join(map(str, self.layout.version))
                if self.layout.version is not None
                else "unknown"
            ),
            current_task=current,
            task_count=total if total is not None else len(tasks),
            tick_count=system_value("xTickCount"),
            scheduler_state=(
                "running"
                if scheduler
                else "not-running"
                if scheduler is not None
                else "unavailable"
            ),
            state_counts={
                name: value for name, value in counts.items() if value is not None
            },
            object_counts={"task": len(tasks)},
            heap_allocator=None,
        )
