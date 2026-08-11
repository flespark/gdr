"""RT-Thread adapter: gdb.Value conversion and semantic capability provider.

Converts raw ``gdb.Value`` objects into lightweight dataclasses for generic
command tables and implements the RTOS-neutral task/object contract. The core
registers the public GDB functions, which return raw target values for native
GDB expression drilling.

Design follows the Asterinas principle:
- **Navigation belongs to helpers** — convenience functions locate objects
  and return raw ``gdb.Value`` so users can inspect any field with native
  GDB expressions.
- **Display belongs to GDB** — pretty-printers (registered separately) fold
  wrapper types; the dataclasses here are only for command table output.
"""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.abstractions import (
    Event,
    Mailbox,
    MemoryPool,
    Mutex,
    Semaphore,
    Thread,
    Timer,
)
from gdr.adapter_api import ObjectTable, RtosAdapter, SystemSummary, TaskSummary
from gdr.gdb_bridge import (
    eval_safe,
    lookup_symbol_at,
    read_bytes,
    read_cstring,
    read_int,
)
from gdr.layout import KernelLayout, read_field
from rtthread.layout import (
    RT_THREAD_STACK_FILL,
    RT_TIMER_FLAG_ACTIVATED,
    RT_TIMER_FLAG_PERIODIC,
    RT_TIMER_FLAG_SOFT_TIMER,
    ThreadState,
    resolve_object_type_code,
)
from rtthread.navigation import (
    find_object as find_rt_object,
)
from rtthread.navigation import (
    find_thread,
    get_current_thread,
    get_tick,
    iter_objects,
    iter_threads,
    iter_timers,
)


def _type_code(layout: KernelLayout, name: str) -> int:
    """Return an active target's numeric code for a semantic object name."""
    return layout.object_codes[name]


# ---------------------------------------------------------------------------
# Value → dataclass converters
# ---------------------------------------------------------------------------


def _get_addr(val: gdb.Value) -> int:
    """Get the address of a gdb.Value as int, or 0 if not addressable."""
    try:
        addr = val.address
        return int(addr) if addr is not None else 0
    except (gdb.error, TypeError):
        return 0


def _infer_stack_grows_up(stack: bytes) -> bool | None:
    """Infer RT-Thread stack direction from its initialized boundary sentinels."""
    if not stack:
        return None
    edge_size = min(len(stack), 16)
    fill = bytes([RT_THREAD_STACK_FILL]) * edge_size
    low_untouched = stack[:edge_size] == fill
    high_untouched = stack[-edge_size:] == fill
    if low_untouched == high_untouched:
        return None
    return high_untouched


def _max_stack_used(stack: bytes, stack_grows_up: bool | None) -> int | None:
    """Return RT-Thread's fill-pattern high-water mark for a known direction."""
    if stack_grows_up is None:
        return None
    fill = bytes([RT_THREAD_STACK_FILL])
    return len(stack.rstrip(fill) if stack_grows_up else stack.lstrip(fill))


def value_to_thread(val: gdb.Value, layout: KernelLayout) -> Thread:
    """Convert a ``struct rt_thread`` gdb.Value to a ``Thread`` dataclass."""
    sl = layout.structs["struct rt_thread"]
    name = read_cstring(read_field(val, sl, "name")) or ""
    stat_raw = read_int(read_field(val, sl, "stat")) or 0
    state = ThreadState.from_raw(stat_raw)
    stack_addr = read_int(read_field(val, sl, "stack_addr")) or 0
    stack_size = read_int(read_field(val, sl, "stack_size")) or 0
    stack = read_bytes(stack_addr, stack_size) if stack_addr and stack_size else None
    stack_grows_up = layout.stack_grows_up
    if stack_grows_up is None and stack is not None:
        stack_grows_up = _infer_stack_grows_up(stack)
    max_stack_used = _max_stack_used(stack, stack_grows_up) if stack else None

    return Thread(
        name=name,
        address=_get_addr(val),
        type_code=_type_code(layout, "thread"),
        state=int(state),
        current_priority=read_int(read_field(val, sl, "current_priority")) or 0,
        init_priority=read_int(read_field(val, sl, "init_priority")) or 0,
        sp=read_int(read_field(val, sl, "sp")) or 0,
        stack_addr=stack_addr,
        stack_size=stack_size,
        stack_grows_up=stack_grows_up,
        max_stack_used=max_stack_used,
        entry=read_int(read_field(val, sl, "entry")) or 0,
        error=read_int(read_field(val, sl, "error")) or 0,
        remaining_tick=read_int(read_field(val, sl, "remaining_tick")) or 0,
        bind_cpu=read_int(read_field(val, sl, "bind_cpu")) or -1,
        oncpu=read_int(read_field(val, sl, "oncpu")) or -1,
    )


def value_to_semaphore(val: gdb.Value, layout: KernelLayout) -> Semaphore:
    """Convert a ``struct rt_semaphore`` gdb.Value to ``Semaphore``."""
    sl = layout.structs["struct rt_semaphore"]
    return Semaphore(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "semaphore"),
        value=read_int(read_field(val, sl, "value")) or 0,
    )


def value_to_mutex(val: gdb.Value, layout: KernelLayout) -> Mutex:
    """Convert a ``struct rt_mutex`` gdb.Value to ``Mutex``."""
    sl = layout.structs["struct rt_mutex"]
    owner_val = read_field(val, sl, "owner")
    owner_name = ""
    if owner_val is not None and int(owner_val) != 0:
        try:
            owner_name = read_cstring(owner_val.dereference()["name"]) or ""
        except (gdb.error, gdb.MemoryError):
            owner_name = "<invalid>"

    return Mutex(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "mutex"),
        value=read_int(read_field(val, sl, "value")) or 0,
        hold=read_int(read_field(val, sl, "hold")) or 0,
        owner=owner_name,
        original_priority=read_int(read_field(val, sl, "original_priority")) or 0,
    )


def value_to_timer(val: gdb.Value, layout: KernelLayout) -> Timer:
    """Convert a ``struct rt_timer`` gdb.Value to ``Timer``."""
    sl = layout.structs["struct rt_timer"]
    flag = read_int(read_field(val, sl, "flag")) or 0

    return Timer(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "timer"),
        active=bool(flag & RT_TIMER_FLAG_ACTIVATED),
        periodic=bool(flag & RT_TIMER_FLAG_PERIODIC),
        soft_timer=bool(flag & RT_TIMER_FLAG_SOFT_TIMER),
        init_tick=read_int(read_field(val, sl, "init_tick")) or 0,
        timeout_tick=read_int(read_field(val, sl, "timeout_tick")) or 0,
        callback=read_int(read_field(val, sl, "timeout_func")) or 0,
    )


def value_to_event(val: gdb.Value, layout: KernelLayout) -> Event:
    """Convert a ``struct rt_event`` gdb.Value to ``Event``."""
    sl = layout.structs["struct rt_event"]
    return Event(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "event"),
        set=read_int(read_field(val, sl, "set")) or 0,
    )


def value_to_mailbox(val: gdb.Value, layout: KernelLayout) -> Mailbox:
    """Convert a ``struct rt_mailbox`` gdb.Value to ``Mailbox``."""
    sl = layout.structs["struct rt_mailbox"]
    return Mailbox(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "mailbox"),
        size=read_int(read_field(val, sl, "size")) or 0,
        entry=read_int(read_field(val, sl, "entry")) or 0,
        in_offset=read_int(read_field(val, sl, "in_offset")) or 0,
        out_offset=read_int(read_field(val, sl, "out_offset")) or 0,
    )


def value_to_messagequeue(val: gdb.Value, layout: KernelLayout) -> MemoryPool:
    """Convert a ``struct rt_messagequeue`` gdb.Value to a dataclass."""
    sl = layout.structs["struct rt_messagequeue"]
    from gdr.abstractions import MessageQueue

    return MessageQueue(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "msgqueue"),
        msg_size=read_int(read_field(val, sl, "msg_size")) or 0,
        max_msgs=read_int(read_field(val, sl, "max_msgs")) or 0,
        entry=read_int(read_field(val, sl, "entry")) or 0,
    )


def value_to_mempool(val: gdb.Value, layout: KernelLayout) -> MemoryPool:
    """Convert a ``struct rt_mempool`` gdb.Value to ``MemoryPool``."""
    sl = layout.structs["struct rt_mempool"]
    return MemoryPool(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=_type_code(layout, "mempool"),
        block_size=read_int(read_field(val, sl, "block_size")) or 0,
        block_total_count=read_int(read_field(val, sl, "block_total_count")) or 0,
        block_free_count=read_int(read_field(val, sl, "block_free_count")) or 0,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class RtThreadAdapter(RtosAdapter):
    """Expose RT-Thread navigation through GDR's semantic adapter contract."""

    def __init__(self, layout: KernelLayout) -> None:
        self.layout = layout

    def find_task(self, name: str) -> gdb.Value | None:
        return find_thread(name, self.layout)

    def find_object(self, kind: str, name: str) -> gdb.Value | None:
        if kind.strip().lower() == "task":
            kind = "thread"
        type_code = resolve_object_type_code(kind, self.layout)
        return (
            find_rt_object(type_code, name, self.layout)
            if type_code is not None
            else None
        )

    def object_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for type_code, info in self.layout.object_types.items():
            if not info.enabled:
                continue
            kind = "task" if info.name == "thread" else info.name
            counts[kind] = sum(1 for _ in iter_objects(type_code, self.layout))
        return counts

    def object_table(self, kind: str) -> ObjectTable | None:
        if kind == "semaphore":
            if "struct rt_semaphore" not in self.layout.structs:
                return None
            rows = []
            for value in iter_objects(
                self.layout.object_codes["semaphore"], self.layout
            ):
                semaphore = value_to_semaphore(value, self.layout)
                rows.append(
                    [semaphore.name, str(semaphore.value), hex(semaphore.address)]
                )
            return ObjectTable(["Name", "Value", "Addr"], rows)
        if kind == "mutex":
            if "struct rt_mutex" not in self.layout.structs:
                return None
            rows = []
            for value in iter_objects(self.layout.object_codes["mutex"], self.layout):
                mutex = value_to_mutex(value, self.layout)
                rows.append(
                    [
                        mutex.name,
                        str(mutex.value),
                        str(mutex.hold),
                        mutex.owner or "N/A",
                        hex(mutex.address),
                    ]
                )
            return ObjectTable(["Name", "Value", "Hold", "Owner", "Addr"], rows)
        if kind == "timer":
            rows = []
            for value in iter_timers(self.layout):
                timer = value_to_timer(value, self.layout)
                callback = lookup_symbol_at(timer.callback) if timer.callback else None
                rows.append(
                    [
                        timer.name,
                        "active" if timer.active else "inactive",
                        "periodic" if timer.periodic else "one-shot",
                        "soft" if timer.soft_timer else "hard",
                        str(timer.init_tick),
                        str(timer.timeout_tick),
                        f"<{callback}>" if callback else hex(timer.callback),
                    ]
                )
            tick = get_tick()
            return ObjectTable(
                [
                    "Name",
                    "State",
                    "Mode",
                    "Type",
                    "InitTick",
                    "TimeoutTick",
                    "Callback",
                ],
                rows,
                [f"Kernel tick: {tick if tick is not None else 'N/A'}"],
            )
        return None

    def iter_tasks(self):
        yield from iter_threads(self.layout)

    def summarize_task(self, value: gdb.Value) -> TaskSummary:
        thread = value_to_thread(value, self.layout)
        try:
            state = ThreadState(thread.state).name.title()
        except ValueError:
            state = "Unknown"
        current = get_current_thread()
        current_address = _get_addr(current) if current is not None else 0
        return TaskSummary(
            name=thread.name,
            address=thread.address,
            state=state,
            priority=thread.current_priority,
            base_priority=thread.init_priority,
            stack_pointer=thread.sp,
            stack_size=thread.stack_size or None,
            stack_used=thread.stack_used,
            high_water_mark=thread.max_stack_used,
            entry=thread.entry,
            current_core=0
            if current_address == thread.address and thread.address
            else None,
        )

    def system_summary(self) -> SystemSummary:
        values = list(self.iter_tasks())
        tasks = [self.summarize_task(value) for value in values]
        states: dict[str, int] = {}
        for task in tasks:
            states[task.state] = states.get(task.state, 0) + 1
        current = next(
            (task.name for task in tasks if task.current_core is not None), None
        )
        used = read_int(eval_safe("(int)rt_memory_info(0)"))
        return SystemSummary(
            kernel_version="RT-Thread",
            current_task=current,
            task_count=len(tasks),
            tick_count=get_tick(),
            scheduler_state="unavailable",
            state_counts=states,
            heap_summary=f"{used} bytes used" if used is not None else "unavailable",
        )
