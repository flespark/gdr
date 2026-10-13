"""RT-Thread adapter: gdb.Value conversion and semantic capability provider.

Owns RT-Thread's intermediate object models, converts raw ``gdb.Value``
objects into those models for command tables and diagnostics, and implements
the RTOS-neutral task/object contract. The core registers the public GDB
functions, which return raw target values for native GDB expression drilling.

Design follows the Asterinas principle:
- **Navigation belongs to helpers** — convenience functions locate objects
  and return raw ``gdb.Value`` so users can inspect any field with native
  GDB expressions.
- **Display belongs to GDB** — pretty-printers (registered separately) fold
  wrapper types; the dataclasses here support aggregate command tables and
  adapter-owned detail diagnostics rather than native GDB inspection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from gdr.formatting import format_optional_bytes, format_optional_int

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.adapter_api import (
    Capabilities,
    Cell,
    CheckFinding,
    ColumnSpec,
    DetailRow,
    DetailSection,
    HeapMap,
    HeapMapBlock,
    HeapReport,
    KindCapability,
    ObjectDetail,
    ObjectTable,
    RtosAdapter,
    SystemSummary,
    address_cell,
    count_cell,
    merged_detail_table,
    number_cell,
    raw_cell,
    sections_from_pairs,
    symbol_cell,
    text_cell,
    waiters_cell,
)
from gdr.derive import timer_expires_in as derive_timer_expires_in
from gdr.gdb_bridge import (
    arch_or_default,
    eval_identifier,
    lookup_symbol_at,
    read_bytes,
    read_cstring,
    read_int,
    source_location_at,
    value_address,
)
from gdr.layout import KernelLayout, read_field
from rtthread import diagnostics
from rtthread.columns import (
    EVENT_SPECS,
    KIND_SPECS,
    MAILBOX_SPECS,
    MEMPOOL_SPECS,
    MSGQUEUE_SPECS,
    MUTEX_SPECS,
    SEMAPHORE_SPECS,
    TIMER_SPECS,
    smp_columns_present,
    task_specs,
)
from rtthread.heapviews import (
    HeapDetail,
    HeapInfo,
    _heap_accounting_finding,
    _heap_chain_finding,
    _heap_detail_from_walk,
    _heap_status,
)
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
    find_timer_by_target,
    get_current_thread,
    get_heap_snapshot,
    get_tick,
    iter_objects,
    iter_suspend_threads,
    iter_threads,
    iter_timers,
    read_irq_nest,
    read_scheduler_lock_nest,
    suspend_thread_names,
)
from rtthread.stackpc import SavedPcLayout, decode_saved_pc, saved_pc_layout
from rtthread.tableviews import DETAIL_SPECS, detail_cells

# ---------------------------------------------------------------------------
# Adapter-owned intermediate objects. These are semantic values assembled from
# raw target objects for tables and detail renderers; they are not ABI layout
# descriptions and therefore live with the converters that create them.


@dataclass
class KernelObject:
    """Common RT-Thread object fields used by adapter presentations."""

    name: str = ""
    address: int = 0


@dataclass
class Thread(KernelObject):
    state: int = -1
    current_priority: int = 0
    init_priority: int = 0
    sp: int = 0
    stack_addr: int = 0
    stack_size: int = 0
    stack_grows_up: bool | None = None
    max_stack_used: int | None = None
    entry: int = 0
    error: int = 0
    remaining_tick: int = 0
    bind_cpu: int | None = None
    oncpu: int | None = None
    # Last PC: the running PC for the current thread, else the saved switch
    # frame's return address (see ``rtthread.stackpc``); ``None`` when unproven.
    pc: int | None = None

    @property
    def stack_used(self) -> int | None:
        if not self.stack_addr or not self.stack_size or not self.sp:
            return None
        if self.stack_grows_up is None:
            return None
        used = (
            self.sp - self.stack_addr
            if self.stack_grows_up
            else self.stack_size - (self.sp - self.stack_addr)
        )
        return used if 0 <= used <= self.stack_size else None


@dataclass
class Semaphore(KernelObject):
    value: int = 0


@dataclass
class Mutex(KernelObject):
    value: int = 0
    hold: int = 0
    owner: str = ""
    original_priority: int = 0


@dataclass
class Timer(KernelObject):
    active: bool = False
    periodic: bool = False
    soft_timer: bool = False
    init_tick: int = 0
    timeout_tick: int = 0
    callback: int = 0
    parameter: int = 0


@dataclass
class Event(KernelObject):
    set: int = 0


@dataclass
class Mailbox(KernelObject):
    size: int = 0
    entry: int = 0
    in_offset: int = 0
    out_offset: int = 0


@dataclass
class MessageQueue(KernelObject):
    msg_size: int = 0
    max_msgs: int = 0
    entry: int = 0


@dataclass
class MemoryPool(KernelObject):
    block_size: int = 0
    block_total_count: int = 0
    block_free_count: int = 0


# Value → dataclass converters
# ---------------------------------------------------------------------------


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
        address=value_address(val),
        state=state.value,
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
        address=value_address(val),
        value=read_int(read_field(val, sl, "value")) or 0,
    )


def value_to_mutex(val: gdb.Value, layout: KernelLayout) -> Mutex:
    """Convert a ``struct rt_mutex`` gdb.Value to ``Mutex``."""
    sl = layout.structs["struct rt_mutex"]
    owner_val = read_field(val, sl, "owner")
    owner_name = ""
    if owner_val is not None and read_int(owner_val):
        try:
            owner_name = read_cstring(owner_val.dereference()["name"]) or ""
        except (gdb.error, gdb.MemoryError):
            owner_name = "<invalid>"

    return Mutex(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=value_address(val),
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
        address=value_address(val),
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
        address=value_address(val),
        set=read_int(read_field(val, sl, "set")) or 0,
    )


def value_to_mailbox(val: gdb.Value, layout: KernelLayout) -> Mailbox:
    """Convert a ``struct rt_mailbox`` gdb.Value to ``Mailbox``."""
    sl = layout.structs["struct rt_mailbox"]
    return Mailbox(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=value_address(val),
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
        address=value_address(val),
        msg_size=read_int(read_field(val, sl, "msg_size")) or 0,
        max_msgs=read_int(read_field(val, sl, "max_msgs")) or 0,
        entry=read_int(read_field(val, sl, "entry")) or 0,
    )


def value_to_mempool(val: gdb.Value, layout: KernelLayout) -> MemoryPool:
    """Convert a ``struct rt_mempool`` gdb.Value to ``MemoryPool``."""
    sl = layout.structs["struct rt_mempool"]
    return MemoryPool(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=value_address(val),
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


def _detail(pairs: list[tuple[str, str]], *, found: bool = True) -> ObjectDetail:
    """Wrap a flat detail block, adding its grouped verdict rows.

    ``pairs`` remains the complete flat view the text renderer prints, so the
    section adds severity without changing terminal output.
    """
    return ObjectDetail(pairs=pairs, found=found, sections=sections_from_pairs(pairs))


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
    means the timer is inactive or the kernel tick is unavailable.  The
    wrapped-unsigned arithmetic lives in :func:`gdr.derive.timer_expires_in`
    (with ``overdue=False`` so an expired-but-linked timer keeps the wrapped
    value, matching RT-Thread's semantics); this name normalises the shared
    ``N/A`` back to the adapter's ``None`` contract.
    """
    value = derive_timer_expires_in(
        timeout_tick, current_tick, 0xFFFFFFFF, overdue=False
    )
    return None if value == "N/A" else value


def _waiter_summary(
    value: gdb.Value,
    layout: KernelLayout,
    struct_name: str,
    field_name: str,
    *,
    available: bool = True,
) -> str:
    """Return a ``count@names`` waiter summary for one object's wait list.

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
    return f"{len(names)}@{','.join(names)}"


def _waiter_cell(
    value: gdb.Value,
    layout: KernelLayout,
    struct_name: str,
    field_name: str,
    *,
    available: bool = True,
) -> Cell:
    """A waiter column cell carrying both its text and the waiter names.

    The names ride along so a plugin can list who is blocked without a second
    target walk.  An empty list stays distinct from ``None``: the former means
    "nobody waits", the latter "this kernel has no such wait list".
    """
    if not available:
        return waiters_cell("N/A")
    names = suspend_thread_names(value, layout, struct_name, field_name)
    if not names:
        return waiters_cell("0", [])
    return waiters_cell(f"{len(names)}@{','.join(names)}", names)


def _count_cell(text: str | None) -> Cell:
    """A tick-count cell; the shared helper owns the numeric-value rule."""
    return count_cell(text)


def _waiter_detail_pairs(
    value: gdb.Value,
    layout: KernelLayout,
    struct_name: str,
    wait_lists: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Build stable waiter fields for one object's detail view."""
    struct_layout = layout.structs.get(struct_name)
    return [
        (
            label,
            _waiter_summary(
                value,
                layout,
                struct_name,
                field_name,
                available=(
                    struct_layout is not None and field_name in struct_layout.fields
                ),
            ),
        )
        for label, field_name in wait_lists
    ]


def _ipc_detail_pairs(
    value: gdb.Value,
    layout: KernelLayout,
    struct_name: str,
    wait_lists: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Build scheduling-policy and waiter diagnostics for an IPC object."""
    struct_layout = layout.structs.get(struct_name)
    flag = (
        read_int(read_field(value, struct_layout, "flag"))
        if struct_layout is not None
        else None
    )
    return [("Policy", _ipc_policy(flag))] + _waiter_detail_pairs(
        value, layout, struct_name, wait_lists
    )


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
    pairs = diagnostics.event_detail(value_to_event(value, layout))
    pairs += _ipc_detail_pairs(
        value,
        layout,
        "struct rt_event",
        (("Waiters", "suspend_thread"),),
    )
    thread_layout = layout.structs.get("struct rt_thread")
    event_layout = layout.structs.get("struct rt_event")
    if thread_layout is None or event_layout is None:
        return pairs
    head = read_field(value, event_layout, "suspend_thread")
    if head is None:
        return pairs
    for thread in iter_suspend_threads(head):
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
    "semaphore": (value_to_semaphore, diagnostics.semaphore_detail),
    "mutex": (value_to_mutex, diagnostics.mutex_detail),
    "mailbox": (value_to_mailbox, diagnostics.mailbox_detail),
    "msgqueue": (value_to_messagequeue, diagnostics.messagequeue_detail),
    "mempool": (value_to_mempool, diagnostics.memorypool_detail),
}


class RtThreadAdapter(RtosAdapter):
    """Expose RT-Thread navigation through GDR's semantic adapter contract."""

    def __init__(
        self,
        layout: KernelLayout,
        heap_type: str = "none",
        version: tuple[int, int, int] | None = None,
    ) -> None:
        self.layout = layout
        self.heap_type = heap_type
        self.version = version
        # Reason: the saved-PC layout is resolved on first use, so building the
        # adapter never reads target memory (tests build it without GDB).
        self._saved_pc: tuple[SavedPcLayout, int] | None = None
        self._saved_pc_resolved = False
        # Reason: IPC policy decoding lives in ``_ipc_policy``; expose it to the
        # RTOS-neutral pretty-printer through the layout's formatter hook so the
        # core reuses the same decoder rather than a duplicated enum_map.
        for struct in layout.structs.values():
            policy = struct.fields.get("policy")
            if policy is not None:
                policy.formatter = _ipc_policy

    def find_task(self, target: str) -> gdb.Value | None:
        """Return the native thread for a name, address or symbol."""
        return find_thread(target, self.layout)

    def find_object(self, kind: str, target: str) -> gdb.Value | None:
        """Return the native object for a name, address or symbol.

        A registry name wins; an explicit address literal or a plain C
        identifier then matches against live registry addresses, so objects
        with a duplicate, truncated or missing name stay reachable by the
        ``Addr`` column their list row prints.
        """
        if kind.strip().lower() == "task":
            kind = "thread"
        if kind.strip().lower() == "timer":
            return find_timer_by_target(target, self.layout)
        type_code = resolve_object_type_code(kind, self.layout)
        return (
            find_rt_object(type_code, target, self.layout)
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

    def object_summary_table(self) -> ObjectTable | None:
        """Per-kind counts with an explicit registry provenance column.

        RT-Thread enumerates every kernel object from its object registry
        (rt_object_get_information), so all counts share the ``registry``
        source; that column keeps the table shape symmetric with FreeRTOS'
        provenance summary (``frt objects``) through the neutral renderer.
        """
        specs = (
            ColumnSpec("Kind", doc="semantic object kind"),
            ColumnSpec("Count", kind="int", align="right"),
            ColumnSpec("Sources", elastic=True, doc="provenance channel"),
        )
        rows: list[list[Cell]] = []
        for type_code, info in self.layout.object_types.items():
            if not info.enabled:
                continue
            kind = "task" if info.name == "thread" else info.name
            count = sum(1 for _ in iter_objects(type_code, self.layout))
            rows.append(
                [text_cell(kind), number_cell(count), text_cell(f"registry={count}")]
            )
        rows.sort(key=lambda row: row[0].text)
        return ObjectTable.from_specs(
            specs,
            rows,
            messages=[
                "object counts come from the kernel object registry "
                "(rt_object_get_information); a count is a complete inventory "
                "of registered kernel objects"
            ],
            elastic=("Sources",),
            id_key="",
        )

    def _registered_objects(
        self, kind: str, struct_name: str
    ) -> Iterator[gdb.Value] | None:
        """Return one enabled registry route, or ``None`` when unavailable."""
        type_code = self.layout.object_codes.get(kind)
        if struct_name not in self.layout.structs or type_code is None:
            return None
        return iter_objects(type_code, self.layout)

    def object_table(self, kind: str) -> ObjectTable | None:
        # --- semaphore --------------------------------------------------
        if kind == "semaphore":
            values = self._registered_objects("semaphore", "struct rt_semaphore")
            if values is None:
                return None
            specs = SEMAPHORE_SPECS
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
                        text_cell(semaphore.name),
                        number_cell(semaphore.value),
                        raw_cell(_ipc_policy(flag), flag),
                        _waiter_cell(
                            value, self.layout, "struct rt_semaphore", "suspend_thread"
                        ),
                        address_cell(semaphore.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("Waiters", "Name"), id_key="Addr"
            )
        # --- mutex --------------------------------------------------
        if kind == "mutex":
            values = self._registered_objects("mutex", "struct rt_mutex")
            if values is None:
                return None
            specs = MUTEX_SPECS
            rows = []
            for value in values:
                mutex = value_to_mutex(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_mutex"], "flag")
                )
                rows.append(
                    [
                        text_cell(mutex.name),
                        number_cell(mutex.value),
                        number_cell(mutex.hold),
                        number_cell(mutex.original_priority),
                        text_cell(mutex.owner or "N/A"),
                        raw_cell(_ipc_policy(flag), flag),
                        _waiter_cell(
                            value, self.layout, "struct rt_mutex", "suspend_thread"
                        ),
                        address_cell(mutex.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("Waiters", "Owner", "Name"), id_key="Addr"
            )
        # --- event --------------------------------------------------
        if kind == "event":
            values = self._registered_objects("event", "struct rt_event")
            if values is None:
                return None
            specs = EVENT_SPECS
            rows = []
            for value in values:
                event = value_to_event(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_event"], "flag")
                )
                rows.append(
                    [
                        text_cell(event.name),
                        raw_cell(hex(event.set), event.set),
                        raw_cell(_ipc_policy(flag), flag),
                        _waiter_cell(
                            value, self.layout, "struct rt_event", "suspend_thread"
                        ),
                        address_cell(event.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("Waiters", "Name"), id_key="Addr"
            )
        # --- mailbox --------------------------------------------------
        if kind == "mailbox":
            values = self._registered_objects("mailbox", "struct rt_mailbox")
            if values is None:
                return None
            specs = MAILBOX_SPECS
            rows = []
            for value in values:
                mailbox = value_to_mailbox(value, self.layout)
                flag = read_int(
                    read_field(value, self.layout.structs["struct rt_mailbox"], "flag")
                )
                rows.append(
                    [
                        text_cell(mailbox.name),
                        number_cell(mailbox.entry),
                        number_cell(mailbox.size),
                        number_cell(max(mailbox.size - mailbox.entry, 0)),
                        number_cell(mailbox.in_offset),
                        number_cell(mailbox.out_offset),
                        raw_cell(_ipc_policy(flag), flag),
                        _waiter_cell(
                            value, self.layout, "struct rt_mailbox", "suspend_thread"
                        ),
                        _waiter_cell(
                            value,
                            self.layout,
                            "struct rt_mailbox",
                            "suspend_sender_thread",
                        ),
                        address_cell(mailbox.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("RecvWait", "SendWait", "Name"), id_key="Addr"
            )
        # --- msgqueue --------------------------------------------------
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
            specs = MSGQUEUE_SPECS
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
                        text_cell(msgqueue.name),
                        number_cell(msgqueue.entry),
                        number_cell(msgqueue.msg_size),
                        number_cell(msgqueue.max_msgs),
                        number_cell(max(msgqueue.max_msgs - msgqueue.entry, 0)),
                        raw_cell(_ipc_policy(flag), flag),
                        _waiter_cell(
                            value,
                            self.layout,
                            "struct rt_messagequeue",
                            "suspend_thread",
                        ),
                        _waiter_cell(
                            value,
                            self.layout,
                            "struct rt_messagequeue",
                            "suspend_sender_thread",
                            available=sender_available,
                        ),
                        address_cell(msgqueue.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("RecvWait", "SendWait", "Name"), id_key="Addr"
            )
        # --- mempool --------------------------------------------------
        if kind == "mempool":
            values = self._registered_objects("mempool", "struct rt_mempool")
            if values is None:
                return None
            specs = MEMPOOL_SPECS
            rows = []
            for value in values:
                mempool = value_to_mempool(value, self.layout)
                rows.append(
                    [
                        text_cell(mempool.name),
                        number_cell(mempool.block_size),
                        number_cell(mempool.block_total_count),
                        number_cell(mempool.block_free_count),
                        number_cell(
                            max(mempool.block_total_count - mempool.block_free_count, 0)
                        ),
                        _waiter_cell(
                            value, self.layout, "struct rt_mempool", "suspend_thread"
                        ),
                        address_cell(mempool.address),
                    ]
                )
            return ObjectTable.from_specs(
                specs, rows, elastic=("Waiters", "Name"), id_key="Addr"
            )
        # --- timer --------------------------------------------------
        if kind == "timer":
            rows = []
            current_tick = get_tick()
            for value in iter_timers(self.layout):
                timer = value_to_timer(value, self.layout)
                callback = lookup_symbol_at(timer.callback) if timer.callback else None
                expires_text = (
                    _timer_expires_in(timer.timeout_tick, current_tick)
                    if timer.active
                    else "N/A"
                )
                rows.append(
                    [
                        text_cell(timer.name),
                        raw_cell(
                            "active" if timer.active else "inactive", timer.active
                        ),
                        raw_cell(
                            "periodic" if timer.periodic else "one-shot",
                            timer.periodic,
                        ),
                        raw_cell(
                            "soft" if timer.soft_timer else "hard", timer.soft_timer
                        ),
                        number_cell(timer.init_tick),
                        number_cell(timer.timeout_tick),
                        _count_cell(expires_text),
                        raw_cell(
                            f"<{callback}>" if callback else hex(timer.callback),
                            timer.callback,
                        ),
                        address_cell(timer.address),
                    ]
                )
            return ObjectTable.from_specs(
                TIMER_SPECS,
                rows,
                messages=[
                    f"Kernel tick: {current_tick if current_tick is not None else 'N/A'}"
                ],
                elastic=("Callback", "Name"),
                id_key="Addr",
            )
        return None

    def object_detail(self, kind: str, target: str) -> ObjectDetail | None:
        """Return vertical detail pairs for one named or addressed object.

        ``None`` means the kind is not reliably enumerable; ``ObjectDetail``
        carries ``found=False`` when the object is missing or its type is not
        enabled.
        """
        if kind == "task":
            value = find_thread(target, self.layout)
            if value is None:
                return ObjectDetail(found=False)
            return _detail(self._detail_pairs_for("task", value) or [])
        if kind == "timer":
            value = find_timer_by_target(target, self.layout)
            if value is None:
                return ObjectDetail(found=False)
            return _detail(self._detail_pairs_for("timer", value) or [])

        type_code = resolve_object_type_code(kind, self.layout)
        if type_code is None:
            return None
        value = find_rt_object(type_code, target, self.layout)
        if value is None:
            return ObjectDetail(found=False)
        pairs = self._detail_pairs_for(kind, value)
        if pairs is None:
            return None
        return _detail(pairs)

    def _detail_pairs_for(
        self, kind: str, value: gdb.Value
    ) -> list[tuple[str, str]] | None:
        """Flat detail pairs of one raw object, or ``None`` with no builder.

        The single source for ``object_detail`` (one named object) and
        ``detail_table`` (every object of a kind), so both render identical
        properties.
        """
        if kind == "task":
            return diagnostics.thread_detail(value_to_thread(value, self.layout))
        if kind == "timer":
            current_tick = get_tick()
            timer = value_to_timer(value, self.layout)
            return diagnostics.timer_detail(timer) + [
                ("KernelTick", format_optional_int(current_tick)),
                (
                    "ExpiresIn",
                    (_timer_expires_in(timer.timeout_tick, current_tick) or "N/A")
                    if timer.active
                    else "N/A",
                ),
            ]
        if kind == "event":
            return _event_detail_with_waiters(value, self.layout)
        entry = _DETAIL_BUILDERS.get(kind)
        if entry is None:
            return None
        converter, builder = entry
        converted = converter(value, self.layout)
        if kind in ("mailbox", "msgqueue", "mempool"):
            # Advanced detail walks kernel memory for slots/nodes/free-list
            # validation, so it needs the raw value plus the active layout.
            pairs = builder(converted, value, self.layout)
        else:
            pairs = builder(converted)
        struct_name = {
            "semaphore": "struct rt_semaphore",
            "mutex": "struct rt_mutex",
            "mailbox": "struct rt_mailbox",
            "msgqueue": "struct rt_messagequeue",
        }.get(kind)
        if struct_name is not None:
            wait_lists = (
                (("RecvWait", "suspend_thread"), ("SendWait", "suspend_sender_thread"))
                if kind in ("mailbox", "msgqueue")
                else (("Waiters", "suspend_thread"),)
            )
            pairs += _ipc_detail_pairs(value, self.layout, struct_name, wait_lists)
        elif kind == "mempool":
            pairs += _waiter_detail_pairs(
                value,
                self.layout,
                "struct rt_mempool",
                (("Waiters", "suspend_thread"),),
            )
        return pairs

    def _raw_objects(self, kind: str) -> Iterator[gdb.Value] | None:
        """Every raw object of one kind, or ``None`` when it is not enumerable."""
        if kind == "task":
            return iter_threads(self.layout)
        if kind == "timer":
            return iter_timers(self.layout)
        type_code = resolve_object_type_code(kind, self.layout)
        if type_code is None:
            return None
        return iter_objects(type_code, self.layout)

    def detail_table(self, kind: str) -> ObjectTable | None:
        """Every object of one kind as a merged detail table, or ``None``."""
        values = self._raw_objects(kind)
        if values is None:
            return None
        rows: list[list[tuple[str, str]]] = []
        overrides: list[dict[str, Cell]] = []
        for value in values:
            pairs = self._detail_pairs_for(kind, value)
            if pairs is None:
                return None
            # Reason: kind and tick are already identified by the table/System;
            # keep CLI detail prose but omit those redundant table columns.
            pairs = [
                (key, text)
                for key, text in pairs
                if kind == "task" or key not in ("Type", "KernelTick")
            ]
            rows.append(pairs)
            overrides.append(detail_cells(kind, value, self.layout, pairs))
        return merged_detail_table(
            kind, rows, cell_overrides=overrides, column_specs=DETAIL_SPECS
        )

    def iter_tasks(self):
        yield from iter_threads(self.layout)

    def _task_view(
        self, value: gdb.Value, current_address: int
    ) -> tuple[Thread, str, int | None]:
        thread = value_to_thread(value, self.layout)
        try:
            state = ThreadState(thread.state).name.title()
        except ValueError:
            state = "Unknown"
        is_current = current_address == thread.address and thread.address
        current_core = (
            thread.oncpu
            if is_current and thread.oncpu is not None
            else 0
            if is_current
            else None
        )
        thread.pc = self._thread_pc(thread, bool(is_current))
        return thread, state, current_core

    def _saved_pc_setup(self) -> tuple[SavedPcLayout, int] | None:
        """The saved-PC slot layout and ``rt_hw_context_switch``'s address.

        ``None`` when the release is not in the layout table, the port has no
        ``rt_hw_context_switch_exit`` (the common restore path this decoder
        relies on), or the switch symbol cannot be resolved.
        """
        if gdb is None:
            return None
        if not self._saved_pc_resolved:
            self._saved_pc_resolved = True
            pointer_size, _endian = arch_or_default()
            layout = saved_pc_layout(self.version, pointer_size)
            switch = eval_identifier("rt_hw_context_switch")
            exit_path = eval_identifier("rt_hw_context_switch_exit")
            if layout is not None and switch is not None and exit_path is not None:
                self._saved_pc = (layout, value_address(switch))
        return self._saved_pc

    def _thread_pc(self, thread: Thread, is_current: bool) -> int | None:
        """The last PC of one thread, or ``None`` when it cannot be proven.

        The thread on the selected CPU is halted, so its PC is the newest
        frame's PC. A thread running on another CPU has no saved frame to
        decode. Any other thread decodes its saved switch frame.
        """
        if gdb is None:
            return None
        if is_current:
            try:
                return int(gdb.newest_frame().pc())
            except (gdb.error, RuntimeError):
                return None
        if thread.oncpu is not None:
            return None
        setup = self._saved_pc_setup()
        if setup is None or thread.sp == 0:
            return None
        layout, switch_addr = setup
        _pointer_size, endian = arch_or_default()
        decoded = decode_saved_pc(
            thread.sp,
            thread.entry,
            layout,
            read=lambda address, size: read_bytes(address, size),
            switch_addr=switch_addr,
            symbolised=lambda address: lookup_symbol_at(address) is not None,
            endian=endian,
        )
        return decoded.pc if decoded is not None else None

    def _task_views(self):
        current = get_current_thread()
        current_address = value_address(current) if current is not None else 0
        for value in iter_threads(self.layout):
            yield self._task_view(value, current_address)

    def task_table(self) -> ObjectTable:
        smp = smp_columns_present(self.layout)
        rows: list[list[Cell]] = []
        for thread, state, current_core in self._task_views():
            entry_symbol = lookup_symbol_at(thread.entry) if thread.entry else None
            entry_source = source_location_at(thread.entry) if thread.entry else None
            pc = thread.pc & ~1 if thread.pc is not None else None
            pc_symbol = lookup_symbol_at(pc) if pc else None
            pc_source = source_location_at(pc) if pc else None
            row = [
                text_cell(thread.name + (" *" if current_core is not None else "")),
                text_cell(state),
                number_cell(thread.current_priority),
                number_cell(thread.init_priority),
                symbol_cell(pc, pc_symbol, pc_source),
                address_cell(thread.sp),
                number_cell(thread.stack_size or None),
                number_cell(thread.stack_used),
                number_cell(thread.max_stack_used),
                symbol_cell(thread.entry, entry_symbol, entry_source),
            ]
            if smp:
                row += [
                    number_cell(thread.oncpu),
                    number_cell(thread.bind_cpu),
                ]
            row.append(address_cell(thread.address))
            rows.append(row)
        table = ObjectTable.from_specs(
            task_specs(smp), rows, elastic=("Entry", "Name"), id_key="TCB"
        )
        high_water = table.headers.index("HighWater")
        if rows and all(row[high_water].value is None for row in rows):
            table.build_hints.append(
                "HighWater is unavailable: build with kernel debug symbols and "
                "ensure the port preserves RT-Thread's stack watermark fill; "
                "an exhausted or unreadable stack can also make it unavailable."
            )
        return table

    def scheduler_state(self) -> str | None:
        """``In ISR``, ``Locked`` or ``Unlocked``; ``None`` when unreadable.

        An interrupt in progress wins over the lock: a locked scheduler inside
        an ISR is reported as the ISR, the outer condition a reader acts on.
        """
        irq_nest = read_irq_nest()
        if irq_nest is not None and irq_nest > 0:
            return f"In ISR (nest {irq_nest})"
        lock_nest = read_scheduler_lock_nest(get_current_thread())
        if lock_nest is None:
            return None
        if lock_nest > 0:
            return f"Locked (nest {lock_nest})"
        return "Unlocked"

    def system_summary(self) -> SystemSummary:
        tasks = list(self._task_views())
        states: dict[str, int] = {}
        for _thread, state, _current_core in tasks:
            states[state] = states.get(state, 0) + 1
        current = next(
            (
                thread.name
                for thread, _state, current_core in tasks
                if current_core is not None
            ),
            None,
        )
        heap = self.heap_info()
        return SystemSummary(
            current_task=current,
            task_count=len(tasks),
            tick_count=get_tick(),
            scheduler_state=self.scheduler_state(),
            state_counts=states,
            object_counts=self.object_counts(),
            heap_allocator=None if heap.algorithm == "none" else heap.algorithm,
            heap_used=heap.used,
            heap_total=heap.total,
            heap_status=_heap_status(heap.walk, heap.used, heap.total),
        )

    def _memtrace_enabled(self) -> bool:
        """Return whether the probed block headers carry MEMTRACE owner names."""
        for type_name in (
            "struct heap_mem",
            "struct rt_small_mem_item",
            "struct rt_memheap_item",
        ):
            struct_layout = self.layout.structs.get(type_name)
            if struct_layout is None:
                continue
            if (
                "thread" in struct_layout.fields
                or "owner_thread_name" in struct_layout.fields
            ):
                return True
        return False

    def heap_info(self) -> HeapInfo:
        """Collect the system-heap snapshot and walk once.

        ``rtt system`` and ``rtt heap`` both take sizes and walk flags from
        this result so a missing snapshot counter falls back to exact walk
        bytes under the same rule.
        """
        snap = get_heap_snapshot(self.heap_type, self.layout)
        walk = (
            diagnostics.walk_system_heap(self.heap_type, self.layout)
            if self.heap_type != "none"
            else None
        )
        used = snap.used
        total = snap.total
        from_walk = False
        # Reason: only a closed, uncorrupted small_mem/memheap walk yields
        # exact used/total. Truncated walks and slab page counts are estimates
        # and must not fill command counters.
        if (
            (used is None or total is None)
            and walk is not None
            and not walk.truncated
            and not walk.corrupt
            and walk.used_bytes is not None
            and walk.total_bytes is not None
        ):
            used = walk.used_bytes
            total = walk.total_bytes
            from_walk = True
        return HeapInfo(
            algorithm=snap.algorithm,
            used=used,
            total=total,
            max_used=snap.max_used,
            from_walk=from_walk,
            walk=walk,
        )

    def heap_basic_pairs(self, info: HeapInfo | None = None) -> list[tuple[str, str]]:
        """Vertical basics for ``rtt heap``: algorithm, sizes, and MEMTRACE."""
        heap = info if info is not None else self.heap_info()
        pairs = [
            ("Algorithm", heap.algorithm),
            ("TotalSize", format_optional_bytes(heap.total)),
            ("UsedSize", format_optional_bytes(heap.used)),
            ("MaxUsed", format_optional_bytes(heap.max_used)),
            ("MemTrace", "enabled" if self._memtrace_enabled() else "N/A"),
        ]
        if heap.from_walk:
            pairs.append(("Source", "walk"))
        return pairs

    def heap_detail(self, info: HeapInfo | None = None) -> HeapDetail | None:
        """Format the Blocks line from the collected system-heap walk.

        Pass ``heap_info()`` to reuse that collection; otherwise this method
        collects snapshot and walk itself. Returns ``None`` when the block
        chain is not resolvable so the caller keeps the basics and marks
        Blocks unavailable.
        """
        heap = info if info is not None else self.heap_info()
        return _heap_detail_from_walk(heap.walk, heap.used, heap.total)

    def capabilities(self) -> Capabilities:
        """Structured capability claims: kinds, columns and their gating.

        Columns come from the same contracts the tables are built from, so a
        plugin never offers a column this session cannot produce.  ``note``
        carries the reason a column or kind is degraded -- what a UI needs to
        tell the user which kernel option would enable it.
        """
        counts = self.object_counts()
        detail_kinds = frozenset(_DETAIL_BUILDERS) | {"task", "timer", "event"}
        kinds: dict[str, KindCapability] = {}
        for kind, specs in KIND_SPECS.items():
            note = None
            if kind == "msgqueue":
                note = (
                    "SendWait is N/A on kernels whose message queue has no "
                    "sender wait list (before v3.1.4, and v4.0.0-v4.0.1)"
                )
            kinds[kind] = KindCapability(
                enumerable=kind in counts,
                detail=kind in detail_kinds,
                columns=tuple(spec.key for spec in specs),
                note=note,
            )
        smp = smp_columns_present(self.layout)
        kinds["task"] = KindCapability(
            enumerable="task" in counts,
            detail=True,
            columns=tuple(spec.key for spec in task_specs(smp)),
            note=(
                "CPU/Bind appear only on SMP builds; HighWater is N/A unless "
                "the build fills stacks"
            ),
        )
        return Capabilities(
            kinds=kinds,
            extra={"smp": smp, "heap_type": self.heap_type},
            build_hints=(
                [
                    "CPU/Bind are unavailable: build with RT_USING_SMP for a multi-core target."
                ]
                if not smp
                else []
            ),
        )

    def diagnostics_report(self) -> list[CheckFinding]:
        """System-wide verdicts: the heap chain and its counter accounting.

        Per-object verdicts stay in the detail verbs; what is system-wide here
        is whether the block chain walked cleanly and whether the snapshot
        counters agree with the exact chain accounting.
        """
        return self._heap_findings(
            self.heap_info() if self.heap_type != "none" else None
        )

    def _heap_findings(self, info: HeapInfo | None) -> list[CheckFinding]:
        """The heap verdicts, shared by ``checks`` and the heap section."""
        if info is None:
            return [
                CheckFinding(
                    "HeapChain", "skipped", "no system heap in this build", "system"
                )
            ]
        return [_heap_chain_finding(info.walk), _heap_accounting_finding(info)]

    def heap_map(self) -> HeapMap | None:
        """The heap's block layout for ``rtt heap dump``.

        ``None`` when there is no heap or its block chain cannot be resolved;
        a truncated or corrupt walk still returns the blocks it read, flagged.
        """
        if self.heap_type == "none":
            return None
        info = self.heap_info()
        walk = info.walk
        if walk is None:
            return None
        messages: list[str] = []
        if walk.corrupt:
            messages.append(
                "the block chain is corrupt; blocks past the damage are not drawn"
            )
        elif walk.truncated:
            messages.append(
                "the walk hit the traversal bound; later blocks are not drawn"
            )
        return HeapMap(
            algorithm=info.algorithm,
            base=walk.base,
            end=walk.end,
            blocks=[
                HeapMapBlock(
                    address=block.address,
                    size=block.span,
                    used=block.used,
                    owner=block.owner,
                )
                for block in walk.blocks
            ],
            owners=self._memtrace_enabled(),
            truncated=walk.truncated or walk.corrupt,
            messages=messages,
        )

    def heap_report(self) -> HeapReport:
        """One snapshot+walk collection formatted for ``rtt heap``.

        The vertical basics and the Blocks line; the chain and accounting
        verdicts ride as ``sections`` so the panel shows them as heap
        diagnostics. There is no per-thread table: owner data is in
        ``heap_map`` (``rtt heap dump``).
        """
        info_ = self.heap_info()
        pairs = self.heap_basic_pairs(info_)
        detail = self.heap_detail(info_)
        if detail is not None:
            pairs += detail.pairs
        else:
            pairs.append(("Blocks", "N/A"))
        findings = self._heap_findings(info_ if self.heap_type != "none" else None)
        rows = tuple(
            DetailRow(
                key=finding.name,
                value=(
                    finding.verdict
                    if not finding.detail
                    else f"{finding.verdict}: {finding.detail}"
                ),
                severity=finding.verdict,
            )
            for finding in findings
        )
        return HeapReport(
            pairs=pairs,
            table=None,
            sections=[DetailSection(title="Checks", rows=rows)] if rows else [],
        )
