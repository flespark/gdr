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

from collections.abc import Callable, Iterator

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.abstractions import (
    Event,
    Mailbox,
    MemoryPool,
    MessageQueue,
    Mutex,
    Semaphore,
    Thread,
    Timer,
)
from gdr.adapter_api import (
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
    TaskSummary,
)
from gdr.gdb_bridge import (
    eval_safe,
    lookup_symbol_at,
    read_bytes,
    read_cstring,
    read_int,
)
from gdr.layout import KernelLayout, read_field
from rtthread import detail as rt_detail
from rtthread.layout import (
    RT_EVENT_FLAG_AND,
    RT_EVENT_FLAG_CLEAR,
    RT_EVENT_FLAG_OR,
    RT_IPC_FLAG_FIFO,
    RT_IPC_FLAG_PRIO,
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
    iter_suspend_threads,
    iter_threads,
    iter_timers,
    suspend_thread_names,
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
        bind_cpu=_cpu_or_none(
            read_int(read_field(val, sl, "bind_cpu")), layout.cpu_count
        ),
        oncpu=_cpu_or_none(read_int(read_field(val, sl, "oncpu")), layout.cpu_count),
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
        parameter=read_int(read_field(val, sl, "parameter")) or 0,
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


def value_to_messagequeue(val: gdb.Value, layout: KernelLayout) -> MessageQueue:
    """Convert a ``struct rt_messagequeue`` gdb.Value to a dataclass."""
    sl = layout.structs["struct rt_messagequeue"]

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


def _cpu_or_none(raw: int | None, cpu_count: int | None) -> int | None:
    """Map a raw SMP CPU field to a CPU index, or ``None`` when meaningless.

    ``None`` covers three cases that must not become a fake ``-1`` or a fake
    valid CPU: the field is absent (UP build), the thread is unbound /
    not-running (sentinel ``RT_CPUS_NR``), or the value is out of range.
    """
    if raw is None:
        return None
    if cpu_count is not None and (raw < 0 or raw >= cpu_count):
        return None
    return raw


def _ipc_policy(flag: int | None) -> str:
    """Decode an RT-Thread IPC object's scheduling policy from its flag."""
    if flag is None:
        return "N/A"
    if flag & RT_IPC_FLAG_PRIO:
        return "PRIO"
    if flag & RT_IPC_FLAG_FIFO or flag == 0:
        return "FIFO"
    return hex(flag)


def _timer_expires_in(timeout_tick: int, current_tick: int | None) -> str | None:
    """Return the wrap-safe remaining ticks for a timer, or ``None``.

    RT-Thread ticks are 32-bit unsigned; ``timeout_tick - current_tick`` is
    valid across wraparound, so no signed overflow handling is needed. ``None``
    means the timer is inactive or the kernel tick is unavailable.
    """
    if current_tick is None:
        return None
    # Reason: ticks are unsigned 32-bit; the subtraction already wraps safely
    # on the target, so re-apply the mask to keep the host value positive.
    return str((timeout_tick - current_tick) & 0xFFFFFFFF)


def _waiter_summary(
    value: gdb.Value,
    layout: KernelLayout,
    struct_name: str,
    field_name: str,
    *,
    available: bool = True,
) -> str:
    """Return a ``count:names`` waiter summary for one object's wait list.

    Args:
        value: The object whose wait list is inspected.
        layout: Active kernel layout.
        struct_name: Struct name of the object.
        field_name: Layout field holding the wait-list head.
        available: Whether the field exists in this target configuration;
            when False the summary is ``N/A`` instead of a fabricated ``0``.

    The count always leads so truncation keeps the diagnostic count.
    """
    if not available:
        return "N/A"
    names = suspend_thread_names(value, layout, struct_name, field_name)
    if not names:
        return "0"
    return f"{len(names)}:{','.join(names)}"


def _event_mode(info: int) -> str:
    """Decode RT-Thread event wait-mode bits for a waiter's event_info."""
    modes = []
    if info & RT_EVENT_FLAG_AND:
        modes.append("AND")
    if info & RT_EVENT_FLAG_OR:
        modes.append("OR")
    if info & RT_EVENT_FLAG_CLEAR:
        modes.append("CLEAR")
    return "|".join(modes) if modes else hex(info)


def _event_detail_with_waiters(
    value: gdb.Value, layout: KernelLayout
) -> list[tuple[str, str]]:
    """Build event detail pairs plus each waiter's wait condition.

    RT-Thread stores a waiter's mask and AND/OR/CLEAR mode in the thread's
    ``event_set`` / ``event_info``, not on the event object itself. Pairing
    them with the waiter explains why the current ``event.set`` did not wake
    it.
    """
    pairs = rt_detail.event_detail(value_to_event(value, layout))
    thread_layout = layout.structs.get("struct rt_thread")
    event_layout = layout.structs.get("struct rt_event")
    if thread_layout is None or event_layout is None:
        return pairs
    head = read_field(value, event_layout, "suspend_thread")
    if head is None:
        return pairs
    for thread in iter_suspend_threads(head, layout):
        name = read_cstring(read_field(thread, thread_layout, "name")) or "<invalid>"
        event_set = read_int(read_field(thread, thread_layout, "event_set")) or 0
        event_info = read_int(read_field(thread, thread_layout, "event_info")) or 0
        pairs.append(
            (
                f"Waiter: {name}",
                f"set=0x{event_set:x} mode={_event_mode(event_info)}",
            )
        )
    return pairs


_DETAIL_BUILDERS: dict[str, tuple[Callable, Callable]] = {
    "semaphore": (value_to_semaphore, rt_detail.semaphore_detail),
    "mutex": (value_to_mutex, rt_detail.mutex_detail),
    "event": (value_to_event, rt_detail.event_detail),
    "mailbox": (value_to_mailbox, rt_detail.mailbox_detail),
    "msgqueue": (value_to_messagequeue, rt_detail.messagequeue_detail),
    "mempool": (value_to_mempool, rt_detail.memorypool_detail),
}


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

    def _registered_objects(
        self, kind: str, struct_name: str
    ) -> Iterator[gdb.Value] | None:
        """Return one enabled registry route, or ``None`` when unavailable."""
        type_code = self.layout.object_codes.get(kind)
        if struct_name not in self.layout.structs or type_code is None:
            return None
        return iter_objects(type_code, self.layout)

    def object_table(self, kind: str) -> ObjectTable | None:
        if kind == "semaphore":
            values = self._registered_objects("semaphore", "struct rt_semaphore")
            if values is None:
                return None
            rows = []
            for value in values:
                semaphore = value_to_semaphore(value, self.layout)
                flag = read_int(
                    read_field(
                        value, self.layout.structs["struct rt_semaphore"], "flag"
                    )
                )
                rows.append(
                    [
                        semaphore.name,
                        str(semaphore.value),
                        _ipc_policy(flag),
                        _waiter_summary(
                            value, self.layout, "struct rt_semaphore", "suspend_thread"
                        ),
                        hex(semaphore.address),
                    ]
                )
            return ObjectTable(
                ["Name", "Value", "Policy", "Waiters", "Addr"],
                rows,
                elastic=("Name", "Waiters"),
            )
        if kind == "mutex":
            values = self._registered_objects("mutex", "struct rt_mutex")
            if values is None:
                return None
            rows = []
            for value in values:
                mutex = value_to_mutex(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_mutex"], "flag")
                )
                rows.append(
                    [
                        mutex.name,
                        str(mutex.value),
                        str(mutex.hold),
                        str(mutex.original_priority),
                        mutex.owner or "N/A",
                        _ipc_policy(flag),
                        _waiter_summary(
                            value, self.layout, "struct rt_mutex", "suspend_thread"
                        ),
                        hex(mutex.address),
                    ]
                )
            return ObjectTable(
                [
                    "Name",
                    "Value",
                    "Hold",
                    "OrigPrio",
                    "Owner",
                    "Policy",
                    "Waiters",
                    "Addr",
                ],
                rows,
                elastic=("Name", "Owner", "Waiters"),
            )
        if kind == "event":
            values = self._registered_objects("event", "struct rt_event")
            if values is None:
                return None
            rows = []
            for value in values:
                event = value_to_event(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_event"], "flag")
                )
                rows.append(
                    [
                        event.name,
                        hex(event.set),
                        _ipc_policy(flag),
                        _waiter_summary(
                            value, self.layout, "struct rt_event", "suspend_thread"
                        ),
                        hex(event.address),
                    ]
                )
            return ObjectTable(
                ["Name", "Set", "Policy", "Waiters", "Addr"],
                rows,
                elastic=("Name", "Waiters"),
            )
        if kind == "mailbox":
            values = self._registered_objects("mailbox", "struct rt_mailbox")
            if values is None:
                return None
            rows = []
            for value in values:
                mailbox = value_to_mailbox(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_mailbox"], "flag")
                )
                rows.append(
                    [
                        mailbox.name,
                        str(mailbox.entry),
                        str(mailbox.size),
                        str(max(mailbox.size - mailbox.entry, 0)),
                        str(mailbox.in_offset),
                        str(mailbox.out_offset),
                        _ipc_policy(flag),
                        _waiter_summary(
                            value, self.layout, "struct rt_mailbox", "suspend_thread"
                        ),
                        _waiter_summary(
                            value,
                            self.layout,
                            "struct rt_mailbox",
                            "suspend_sender_thread",
                        ),
                        hex(mailbox.address),
                    ]
                )
            return ObjectTable(
                [
                    "Name",
                    "Entry",
                    "Size",
                    "Free",
                    "In",
                    "Out",
                    "Policy",
                    "RecvWait",
                    "SendWait",
                    "Addr",
                ],
                rows,
                elastic=("Name", "RecvWait", "SendWait"),
            )
        if kind == "msgqueue":
            values = self._registered_objects("msgqueue", "struct rt_messagequeue")
            if values is None:
                return None
            mq_layout = self.layout.structs.get("struct rt_messagequeue")
            # Reason: the sender wait list is absent on versions that predate
            # v3.1.4 or fall in v4.0.0-v4.0.1; N/A must not become a fake 0.
            sender_available = (
                mq_layout is not None and "suspend_sender_thread" in mq_layout.fields
            )
            rows = []
            for value in values:
                msgqueue = value_to_messagequeue(value, self.layout)
                flag = read_int(
                    read_field(
                        value, self.layout.structs["struct rt_messagequeue"], "flag"
                    )
                )
                rows.append(
                    [
                        msgqueue.name,
                        str(msgqueue.entry),
                        str(msgqueue.msg_size),
                        str(msgqueue.max_msgs),
                        str(max(msgqueue.max_msgs - msgqueue.entry, 0)),
                        _ipc_policy(flag),
                        _waiter_summary(
                            value,
                            self.layout,
                            "struct rt_messagequeue",
                            "suspend_thread",
                        ),
                        _waiter_summary(
                            value,
                            self.layout,
                            "struct rt_messagequeue",
                            "suspend_sender_thread",
                            available=sender_available,
                        ),
                        hex(msgqueue.address),
                    ]
                )
            return ObjectTable(
                [
                    "Name",
                    "Entry",
                    "MsgSize",
                    "MaxMsgs",
                    "Free",
                    "Policy",
                    "RecvWait",
                    "SendWait",
                    "Addr",
                ],
                rows,
                elastic=("Name", "RecvWait", "SendWait"),
            )
        if kind == "mempool":
            values = self._registered_objects("mempool", "struct rt_mempool")
            if values is None:
                return None
            rows = []
            for value in values:
                mempool = value_to_mempool(value, self.layout)
                rows.append(
                    [
                        mempool.name,
                        str(mempool.block_size),
                        str(mempool.block_total_count),
                        str(mempool.block_free_count),
                        str(
                            max(mempool.block_total_count - mempool.block_free_count, 0)
                        ),
                        _waiter_summary(
                            value, self.layout, "struct rt_mempool", "suspend_thread"
                        ),
                        hex(mempool.address),
                    ]
                )
            return ObjectTable(
                ["Name", "BlockSize", "Total", "Free", "Used", "Waiters", "Addr"],
                rows,
                elastic=("Name", "Waiters"),
            )
        if kind == "timer":
            rows = []
            current_tick = get_tick()
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
                        _timer_expires_in(timer.timeout_tick, current_tick)
                        if timer.active
                        else "N/A",
                        f"<{callback}>" if callback else hex(timer.callback),
                        hex(timer.address),
                    ]
                )
            return ObjectTable(
                [
                    "Name",
                    "State",
                    "Mode",
                    "Type",
                    "InitTick",
                    "TimeoutTick",
                    "ExpiresIn",
                    "Callback",
                    "Addr",
                ],
                rows,
                [f"Kernel tick: {current_tick if current_tick is not None else 'N/A'}"],
                elastic=("Name", "Callback"),
            )
        return None

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:
        """Return vertical detail pairs for one named object.

        ``None`` means the kind is not reliably enumerable; ``ObjectDetail``
        carries ``found=False`` when the object is missing or its type is not
        enabled.
        """
        if kind == "task":
            value = find_thread(name, self.layout)
            if value is None:
                return ObjectDetail(found=False)
            return ObjectDetail(
                pairs=rt_detail.thread_detail(value_to_thread(value, self.layout))
            )
        if kind == "timer":
            for value in iter_timers(self.layout):
                timer = value_to_timer(value, self.layout)
                if timer.name == name:
                    return ObjectDetail(pairs=rt_detail.timer_detail(timer))
            return ObjectDetail(found=False)

        type_code = resolve_object_type_code(kind, self.layout)
        if type_code is None:
            return None
        value = find_rt_object(type_code, name, self.layout)
        if value is None:
            return ObjectDetail(found=False)
        if kind == "event":
            return ObjectDetail(pairs=_event_detail_with_waiters(value, self.layout))
        entry = _DETAIL_BUILDERS.get(kind)
        if entry is None:
            return None
        converter, builder = entry
        converted = converter(value, self.layout)
        if kind in ("mailbox", "msgqueue", "mempool"):
            # Advanced detail walks kernel memory for slots/nodes/free-list
            # validation, so it needs the raw value plus the active layout.
            return ObjectDetail(pairs=builder(converted, value, self.layout))
        return ObjectDetail(pairs=builder(converted))

    def iter_tasks(self):
        yield from iter_threads(self.layout)

    def _summarize_task(self, value: gdb.Value, current_address: int) -> TaskSummary:
        thread = value_to_thread(value, self.layout)
        try:
            state = ThreadState(thread.state).name.title()
        except ValueError:
            state = "Unknown"
        is_current = current_address == thread.address and thread.address
        # SMP reports the real CPU; UP keeps the current marker on core 0.
        current_core = (
            thread.oncpu
            if is_current and thread.oncpu is not None
            else 0
            if is_current
            else None
        )
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
            current_core=current_core,
            bind_cpu=thread.bind_cpu,
            oncpu=thread.oncpu,
        )

    def iter_task_summaries(self):
        current = get_current_thread()
        current_address = _get_addr(current) if current is not None else 0
        for value in iter_threads(self.layout):
            yield self._summarize_task(value, current_address)

    def system_summary(self) -> SystemSummary:
        tasks = list(self.iter_task_summaries())
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
            object_counts=self.object_counts(),
            heap_summary=f"{used} bytes used" if used is not None else "unavailable",
        )
