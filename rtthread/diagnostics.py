"""RT-Thread single-object detail builders.

Produces vertical ``(key, value)`` pairs for the ``rtt <object> <name>``
detail commands. It consumes intermediate models owned by ``adapter.py``;
field names and version conditionals stay in the RT-Thread package, while the
generic renderer in ``gdr.commands`` only prints the pairs.

The advanced builders (mailbox slots, message-queue nodes, memory-pool free
list) take the raw ``gdb.Value`` plus the active ``KernelLayout`` so they can
walk kernel memory with bounded, corruption-guarded traversal and report
consistency checks alongside the readable summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.constants import GDR_MAX_TRAVERSAL_COUNT
from gdr.formatting import format_optional_int, format_symbol_or_address
from gdr.gdb_bridge import (
    get_arch_info,
    lookup_symbol,
    lookup_symbol_at,
    lookup_type,
    read_bytes,
    read_int,
    read_macro_int,
)
from gdr.layout import KernelLayout, StructLayout, member_offset, read_field
from rtthread.layout import ThreadState

if TYPE_CHECKING:
    from rtthread.adapter import (
        Event,
        Mailbox,
        MemoryPool,
        MessageQueue,
        Mutex,
        Semaphore,
        Thread,
        Timer,
    )

# RT-Thread message-queue node header: ``struct rt_mq_message`` embeds a
# single ``next`` pointer before the payload. Verified on 3.1.0/3.1.5/4.0.5/
# 4.1.1: the payload starts right after the one-pointer node header.
_MQ_HEADER_POINTERS = 1


# Bounds for any raw kernel-memory walk performed by detail diagnostics.
def _symbol_or_hex(address: int) -> str:
    """Render an address as ``<symbol+0>`` when symbolised, else hex."""
    symbol = lookup_symbol_at(address) if address else None
    return format_symbol_or_address(address, symbol)


def _common_pairs(obj) -> list[tuple[str, str]]:
    """Name, address, and semantic object type for any kernel object."""
    return [
        ("Name", obj.name),
        ("Address", hex(obj.address)),
        ("Type", type(obj).__name__),
    ]


def _read_ptr(value, layout, struct_name, field_name) -> int | None:
    """Read a pointer-typed layout field from a raw object value."""
    sl = layout.structs.get(struct_name)
    if sl is None:
        return None
    return read_int(read_field(value, sl, field_name))


def _read_slot(addr: int, ptrsize: int, endian: Literal["little", "big"]) -> str:
    """Render one pointer-sized mailbox slot / pool link at *addr*."""
    raw = read_bytes(addr, ptrsize)
    if raw is None:
        return "<invalid>"
    return hex(int.from_bytes(raw, byteorder=endian))


def _walk_list(
    head: int | None, ptrsize: int, endian: Literal["little", "big"], max_count: int
) -> list[int]:
    """Walk a singly/doubly linked list of raw pointers, bounded and cycle-safe.

    Returns the visited node addresses in order. Stops on a null pointer, a
    repeated address (cycle), or after ``max_count`` entries.
    """
    nodes: list[int] = []
    seen: set[int] = set()
    node = head
    while node is not None and node != 0 and len(nodes) < max_count:
        if node in seen:
            break
        seen.add(node)
        nodes.append(node)
        raw = read_bytes(node, ptrsize)
        if raw is None:
            break
        node = int.from_bytes(raw, byteorder=endian)
    return nodes


def _arch() -> tuple[int, Literal["little", "big"]]:
    """Return (ptrsize, endian) for the active target, falling back safely."""
    info = get_arch_info()
    if info is None or info.ptrsize not in (4, 8):
        return (4, "little")
    endian: Literal["little", "big"] = "little" if info.endian == "little" else "big"
    return (info.ptrsize, endian)


def thread_detail(thread: Thread) -> list[tuple[str, str]]:
    """Detail pairs for a thread, including SMP CPU placement when known."""
    try:
        state = ThreadState(thread.state).name
    except ValueError:
        state = "Unknown"
    pairs = _common_pairs(thread) + [
        ("State", state),
        ("Priority", str(thread.current_priority)),
        ("BasePriority", str(thread.init_priority)),
        ("SP", hex(thread.sp)),
        ("Stack", hex(thread.stack_addr)),
        ("StackSize", str(thread.stack_size)),
        ("Used", format_optional_int(thread.stack_used)),
        ("HighWater", format_optional_int(thread.max_stack_used)),
        ("Entry", _symbol_or_hex(thread.entry)),
        # Error/remaining tick stay in detail; they must not widen the shared
        # task table.
        ("Error", str(thread.error)),
        ("RemainingTick", str(thread.remaining_tick)),
    ]
    if thread.bind_cpu is not None:
        pairs.append(("BindCPU", str(thread.bind_cpu)))
    if thread.oncpu is not None:
        pairs.append(("OnCPU", str(thread.oncpu)))
    return pairs


def timer_detail(timer: Timer) -> list[tuple[str, str]]:
    """Detail pairs for a timer with its flag-derived state."""
    return _common_pairs(timer) + [
        ("State", "active" if timer.active else "inactive"),
        ("Mode", "periodic" if timer.periodic else "one-shot"),
        ("TimerType", "soft" if timer.soft_timer else "hard"),
        ("InitTick", str(timer.init_tick)),
        ("TimeoutTick", str(timer.timeout_tick)),
        ("Callback", _symbol_or_hex(timer.callback)),
        ("Parameter", _symbol_or_hex(timer.parameter)),
    ]


def semaphore_detail(semaphore: Semaphore) -> list[tuple[str, str]]:
    """Detail pairs for a semaphore."""
    return _common_pairs(semaphore) + [("Value", str(semaphore.value))]


def mutex_detail(mutex: Mutex) -> list[tuple[str, str]]:
    """Detail pairs for a mutex, including owner and priority inheritance."""
    return _common_pairs(mutex) + [
        ("Value", str(mutex.value)),
        ("Hold", str(mutex.hold)),
        ("Owner", mutex.owner or "N/A"),
        ("OriginalPriority", str(mutex.original_priority)),
    ]


def event_detail(event: Event) -> list[tuple[str, str]]:
    """Detail pairs for an event set."""
    return _common_pairs(event) + [("Set", hex(event.set))]


def mailbox_detail(
    mailbox: Mailbox, value=None, layout: KernelLayout | None = None
) -> list[tuple[str, str]]:
    """Detail pairs for a mailbox: ring position plus FIFO message slots."""
    pairs = _common_pairs(mailbox) + [
        ("Entry", str(mailbox.entry)),
        ("Size", str(mailbox.size)),
        ("InOffset", str(mailbox.in_offset)),
        ("OutOffset", str(mailbox.out_offset)),
    ]
    if value is not None and layout is not None:
        pairs += _mailbox_slot_pairs(mailbox, value, layout)
    return pairs


def _mailbox_slot_pairs(
    mailbox: Mailbox, value, layout: KernelLayout
) -> list[tuple[str, str]]:
    """Walk mailbox slots in FIFO order and validate the ring offsets."""
    msg_pool = _read_ptr(value, layout, "struct rt_mailbox", "msg_pool")
    checks: list[str] = []
    if mailbox.size <= 0:
        checks.append(f"size {mailbox.size} must be positive")
    if mailbox.in_offset >= mailbox.size:
        checks.append(f"in_offset {mailbox.in_offset} >= size {mailbox.size}")
    if mailbox.out_offset >= mailbox.size:
        checks.append(f"out_offset {mailbox.out_offset} >= size {mailbox.size}")
    if mailbox.entry > mailbox.size:
        checks.append(f"entry {mailbox.entry} > size {mailbox.size}")
    pairs: list[tuple[str, str]] = [
        ("OffsetCheck", "; ".join(checks) if checks else "ok")
    ]
    pairs.append(("MsgPool", hex(msg_pool) if msg_pool is not None else "N/A"))
    if msg_pool is None:
        pairs.append(("SlotCheck", "N/A"))
        return pairs
    ptrsize, endian = _arch()
    for i in range(mailbox.entry if mailbox.size > 0 else 0):
        slot = (mailbox.out_offset + i) % mailbox.size
        pairs.append(
            (f"Slot[{slot}]", _read_slot(msg_pool + slot * ptrsize, ptrsize, endian))
        )
    return pairs


def messagequeue_detail(
    msgqueue: MessageQueue, value=None, layout: KernelLayout | None = None
) -> list[tuple[str, str]]:
    """Detail pairs for a message queue, including a node-consistency check."""
    pairs = _common_pairs(msgqueue) + [
        ("Entry", str(msgqueue.entry)),
        ("MsgSize", str(msgqueue.msg_size)),
        ("MaxMsgs", str(msgqueue.max_msgs)),
    ]
    if value is not None and layout is not None:
        pairs += _messagequeue_node_pairs(msgqueue, value, layout)
    return pairs


def _messagequeue_node_pairs(
    msgqueue: MessageQueue, value, layout: KernelLayout
) -> list[tuple[str, str]]:
    """Walk the active and free message nodes and validate their counts.

    Nodes are ``struct rt_mq_message`` whose ``parent.next`` (the first
    pointer) links to the next node; the payload follows the one-pointer
    header. Active nodes form the head→tail chain, free nodes the free chain.
    """
    ptrsize, endian = _arch()
    head = _read_ptr(value, layout, "struct rt_messagequeue", "msg_queue_head")
    free = _read_ptr(value, layout, "struct rt_messagequeue", "msg_queue_free")
    pool = _read_ptr(value, layout, "struct rt_messagequeue", "msg_pool")
    pairs: list[tuple[str, str]] = [
        ("MsgPool", hex(pool) if pool is not None else "N/A")
    ]

    if head is not None:
        active = _walk_list(head, ptrsize, endian, GDR_MAX_TRAVERSAL_COUNT)
        header_bytes = ptrsize * _MQ_HEADER_POINTERS
        payload_size = min(msgqueue.msg_size, 64)
        for index, node in enumerate(active[: max(msgqueue.max_msgs, 1)]):
            raw = read_bytes(node + header_bytes, payload_size)
            payload = raw.hex() if raw is not None else "<invalid>"
            pairs.append((f"Msg[{index}]", f"@{hex(node)} payload={payload}"))
    else:
        active = []
    pairs.append(("ActiveNodes", str(len(active)) if head is not None else "N/A"))

    if free is not None:
        free_nodes = _walk_list(free, ptrsize, endian, GDR_MAX_TRAVERSAL_COUNT)
        pairs.append(("FreeNodes", str(len(free_nodes))))
    else:
        free_nodes = []
        pairs.append(("FreeNodes", "N/A"))

    consistency = (
        _messagequeue_consistency(
            msgqueue.entry, len(active), len(free_nodes), msgqueue.max_msgs
        )
        if head is not None and free is not None
        else "N/A (message-list pointers unavailable)"
    )
    pairs.append(("Consistency", consistency))
    return pairs


def _messagequeue_consistency(
    entry: int, active_count: int, free_count: int, max_msgs: int
) -> str:
    """Return a human-readable consistency verdict for a message queue."""
    problems: list[str] = []
    if entry != active_count:
        problems.append(f"entry {entry} != active nodes {active_count}")
    if max_msgs > 0 and free_count + active_count != max_msgs:
        problems.append(
            f"free {free_count} + active {active_count} != max_msgs {max_msgs}"
        )
    if not problems:
        return f"ok (entry={entry}, free={free_count}, max={max_msgs})"
    return "mismatch: " + "; ".join(problems)


def memorypool_detail(
    pool: MemoryPool, value=None, layout: KernelLayout | None = None
) -> list[tuple[str, str]]:
    """Detail pairs for a memory pool, including free-list validation."""
    pairs = _common_pairs(pool) + [
        ("BlockSize", str(pool.block_size)),
        ("Total", str(pool.block_total_count)),
        ("Free", str(pool.block_free_count)),
    ]
    if value is not None and layout is not None:
        pairs += _memorypool_block_pairs(pool, value, layout)
    return pairs


def _memorypool_block_pairs(
    pool: MemoryPool, value, layout: KernelLayout
) -> list[tuple[str, str]]:
    """Show the pool range and validate block alignment and free count."""
    ptrsize, _endian = _arch()
    start = _read_ptr(value, layout, "struct rt_mempool", "start_address")
    pool_size = read_int(read_field(value, layout.structs["struct rt_mempool"], "size"))
    block_list = _read_ptr(value, layout, "struct rt_mempool", "block_list")
    pairs: list[tuple[str, str]] = [
        ("StartAddress", hex(start) if start is not None else "N/A"),
        ("PoolSize", str(pool_size) if pool_size is not None else "N/A"),
        ("BlockList", hex(block_list) if block_list is not None else "N/A"),
    ]

    checks: list[str] = []
    if pool.block_size <= 0 or pool.block_size % ptrsize != 0:
        checks.append(f"block_size {pool.block_size} not {ptrsize}-aligned")
    if start is not None and pool_size is not None and start + pool_size < start:
        checks.append("pool size overflow")
    pairs.append(("AlignmentCheck", "; ".join(checks) if checks else "ok"))

    free_count = (
        len(_walk_list(block_list, ptrsize, _endian, pool.block_total_count or 1))
        if block_list
        else 0
    )
    if free_count != pool.block_free_count:
        pairs.append(
            (
                "FreeCountCheck",
                f"listed {free_count} != cached {pool.block_free_count}",
            )
        )
    else:
        pairs.append(("FreeCountCheck", f"ok ({free_count})"))
    return pairs


# ---------------------------------------------------------------------------
# System-heap block walks (``rtt heap``)
# ---------------------------------------------------------------------------
#
# These walks mirror the kernel's own ``memtrace``/``memcheck`` loops but run on
# halted memory only: they cast item addresses to the heap block-header DWARF
# type (via ``member_offset``) and read raw bytes. Missing DWARF types or
# unreadable memory degrade to ``None`` so the caller keeps the snapshot and
# marks Blocks/Holes unavailable.

# struct heap_mem / struct rt_memheap_item magic constants (mem.c / memheap.c).
HEAP_MAGIC = 0x1EA0
MEMHEAP_MAGIC = 0x1EA01EA0

# slab page types (slab.c).
PAGE_TYPE_FREE = 0x00

# Kernel MEMTRACE owner names are ``rt_uint8_t thread[4]`` /
# ``owner_thread_name[4]`` (mem.c / memheap.c), not pointer-width fields.
_MEMTRACE_NAME_WIDTH = 4


@dataclass
class HeapWalk:
    """Bounded system-heap block walk result for ``rtt heap`` diagnostics.

    ``used_bytes`` / ``total_bytes`` are set only when the walk closed without
    truncation or corruption, so they are exact chain accounting rather than
    an estimate. Slab page walks leave them unset.
    """

    used_blocks: int
    free_blocks: int
    hole_sizes: list[int]
    occupancy: list[tuple[str, int, int]]  # (thread, blocks, bytes)
    truncated: bool = False
    corrupt: bool = False
    used_bytes: int | None = None
    total_bytes: int | None = None


def _read_field_at(
    addr: int,
    type_name: str,
    layout: StructLayout,
    field: str,
    width: int,
    endian: Literal["little", "big"],
) -> int | None:
    """Read one fixed-width layout field from a raw struct address.

    Offsets come from the DWARF type via ``member_offset``, so a missing type
    or config-conditional field degrades to ``None`` (Blocks/Holes N/A).
    """
    f = layout.fields.get(field)
    if f is None:
        return None
    offset = member_offset(type_name, f.path)
    if offset is None:
        return None
    raw = read_bytes(addr + offset, width)
    if raw is None:
        return None
    return int.from_bytes(raw, byteorder=endian)


def _read_name_at(
    addr: int,
    type_name: str,
    layout: StructLayout,
    field: str,
    width: int,
    _endian: Literal["little", "big"],
) -> str | None:
    """Read a fixed-width MEMTRACE owner name from a raw struct address.

    Kernel MEMTRACE pads names with spaces (``rt_mem_setname``), so the decoded
    name is stripped; a blank name (freed block) returns ``None``.
    """
    f = layout.fields.get(field)
    if f is None:
        return None
    offset = member_offset(type_name, f.path)
    if offset is None:
        return None
    raw = read_bytes(addr + offset, width)
    if raw is None:
        return None
    name = raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
    return name or None


def _header_size(type_name: str) -> int | None:
    """Return ``RT_ALIGN(sizeof(type_name), RT_ALIGN_SIZE)``.

    The kernel rounds the header size for ``SIZEOF_STRUCT_MEM`` /
    ``RT_MEMHEAP_SIZE``; replicating the alignment keeps block sizes in sync
    with ``rt_malloc``/``rt_memheap_alloc`` accounting. Missing ``RT_ALIGN_SIZE``
    falls back to the target pointer width.
    """
    t = lookup_type(type_name)
    if t is None:
        return None
    align = read_macro_int("RT_ALIGN_SIZE")
    if align is None or align <= 0:
        # Reason: ``-g`` without ``-g3`` omits macros. RT_ALIGN_SIZE matches
        # the ABI pointer width (4 on 32-bit, 8 on 64-bit), not a fixed 8.
        align = _arch()[0]
    try:
        size = int(t.sizeof)
    except (TypeError, ValueError, AttributeError):
        return None
    return (size + align - 1) & ~(align - 1)


def _exact_byte_counts(
    *, truncated: bool, corrupt: bool, used_bytes: int, total_bytes: int
) -> tuple[int | None, int | None]:
    """Return used/total only when a walk closed over a consistent arena."""
    if truncated or corrupt or used_bytes < 0 or total_bytes < used_bytes:
        return None, None
    return used_bytes, total_bytes


def _occupancy_rows(
    owner_blocks: dict[str, int], owner_bytes: dict[str, int]
) -> list[tuple[str, int, int]]:
    """Sort per-thread occupancy most-allocating first, then by name."""
    return sorted(
        ((name, owner_blocks[name], owner_bytes[name]) for name in owner_blocks),
        key=lambda row: (-row[1], row[0]),
    )


def _walk_small_mem_chain(
    heap_ptr: int,
    heap_end: int,
    item_type: str,
    item_layout: StructLayout,
    header_size: int,
    *,
    used_from_pool_ptr: bool,
    ptrsize: int,
    endian: Literal["little", "big"],
) -> HeapWalk | None:
    """Walk small_mem blocks via ``next`` offsets from ``heap_ptr``.

    4.0 ``struct heap_mem`` marks used via the ``used`` field; 4.1
    ``struct rt_small_mem_item`` marks used via the LSB of ``pool_ptr``. Both
    store the next block as an offset relative to ``heap_ptr``.
    """
    seen: set[int] = set()
    items = 0
    used = 0
    used_bytes = 0
    holes: list[int] = []
    owner_blocks: dict[str, int] = {}
    owner_bytes: dict[str, int] = {}
    truncated = False
    corrupt = False
    memtrace = "thread" in item_layout.fields
    addr = heap_ptr
    while addr != heap_end:
        if addr < heap_ptr or addr > heap_end:
            corrupt = True
            break
        if addr in seen:
            truncated = True
            break
        if len(seen) >= GDR_MAX_TRAVERSAL_COUNT:
            truncated = True
            break
        seen.add(addr)
        offset = addr - heap_ptr
        if used_from_pool_ptr:
            pool_ptr = _read_field_at(
                addr, item_type, item_layout, "pool_ptr", ptrsize, endian
            )
            next_off = _read_field_at(
                addr, item_type, item_layout, "next", ptrsize, endian
            )
            is_used = pool_ptr is not None and bool(pool_ptr & 0x1)
        else:
            magic = _read_field_at(addr, item_type, item_layout, "magic", 2, endian)
            used_raw = _read_field_at(addr, item_type, item_layout, "used", 2, endian)
            next_off = _read_field_at(
                addr, item_type, item_layout, "next", ptrsize, endian
            )
            is_used = used_raw is not None and bool(used_raw)
            if magic is not None and magic != HEAP_MAGIC:
                corrupt = True
                break
        # Reason: kernel memcheck rejects ``position > mem_size_aligned``. A
        # next offset that lands past ``heap_end`` must not keep walking.
        if next_off is None or next_off <= offset or heap_ptr + next_off > heap_end:
            corrupt = True
            break
        size = next_off - offset - header_size
        items += 1
        if is_used:
            used += 1
            used_bytes += max(size, 0)
            if memtrace:
                owner = _read_name_at(
                    addr,
                    item_type,
                    item_layout,
                    "thread",
                    _MEMTRACE_NAME_WIDTH,
                    endian,
                )
                if owner:
                    owner_blocks[owner] = owner_blocks.get(owner, 0) + 1
                    owner_bytes[owner] = owner_bytes.get(owner, 0) + max(size, 0)
        else:
            holes.append(max(size, 0))
        addr = heap_ptr + next_off
    if items == 0:
        return None
    exact_used, exact_total = _exact_byte_counts(
        truncated=truncated,
        corrupt=corrupt,
        used_bytes=used_bytes,
        total_bytes=heap_end - heap_ptr,
    )
    return HeapWalk(
        used_blocks=used,
        free_blocks=items - used,
        hole_sizes=holes,
        occupancy=_occupancy_rows(owner_blocks, owner_bytes),
        truncated=truncated,
        corrupt=corrupt,
        used_bytes=exact_used,
        total_bytes=exact_total,
    )


def _walk_small_mem(kl: KernelLayout) -> HeapWalk | None:
    """Resolve small_mem bounds and dispatch the block-chain walk."""
    ptrsize, endian = _arch()
    system_heap = lookup_symbol("system_heap")
    if system_heap is not None:
        obj_layout = kl.structs.get("struct rt_small_mem")
        if obj_layout is None:
            return None
        item_layout = kl.structs.get("struct rt_small_mem_item")
        if item_layout is None:
            return None
        try:
            base = int(system_heap)
        except (gdb.error, TypeError, ValueError):
            return None
        heap_ptr = _read_field_at(
            base, "struct rt_small_mem", obj_layout, "heap_ptr", ptrsize, endian
        )
        heap_end = _read_field_at(
            base, "struct rt_small_mem", obj_layout, "heap_end", ptrsize, endian
        )
        header_size = _header_size("struct rt_small_mem_item")
        item_type = "struct rt_small_mem_item"
        used_from_pool_ptr = True
    else:
        item_layout = kl.structs.get("struct heap_mem")
        if item_layout is None:
            return None
        heap_ptr = read_int(lookup_symbol("heap_ptr"))
        heap_end = read_int(lookup_symbol("heap_end"))
        header_size = _header_size("struct heap_mem")
        item_type = "struct heap_mem"
        used_from_pool_ptr = False
    if (
        heap_ptr is None
        or heap_end is None
        or header_size is None
        or heap_ptr >= heap_end
    ):
        return None
    return _walk_small_mem_chain(
        heap_ptr,
        heap_end,
        item_type,
        item_layout,
        header_size,
        used_from_pool_ptr=used_from_pool_ptr,
        ptrsize=ptrsize,
        endian=endian,
    )


def _walk_memheap(kl: KernelLayout) -> HeapWalk | None:
    """Walk the memheap ``block_list`` circular item chain (used+free blocks).

    The 0-size tailer whose ``next`` points back at ``block_list`` is skipped,
    matching kernel ``list_memheap`` / memtrace.
    """
    heap_obj = lookup_symbol("system_heap")
    if heap_obj is None:
        heap_obj = lookup_symbol("_heap")
    if heap_obj is None:
        return None
    heap_layout = kl.structs.get("struct rt_memheap")
    if heap_layout is None:
        return None
    head = read_int(read_field(heap_obj, heap_layout, "block_list"))
    if head is None or head == 0:
        return None
    item_layout = kl.structs.get("struct rt_memheap_item")
    if item_layout is None:
        return None
    header_size = _header_size("struct rt_memheap_item")
    if header_size is None:
        return None
    ptrsize, endian = _arch()
    seen: set[int] = set()
    items = 0
    used = 0
    holes: list[int] = []
    owner_blocks: dict[str, int] = {}
    owner_bytes: dict[str, int] = {}
    truncated = False
    corrupt = False
    memtrace = "owner_thread_name" in item_layout.fields
    addr = head
    closed = False
    while True:
        if addr in seen:
            truncated = True
            break
        if len(seen) >= GDR_MAX_TRAVERSAL_COUNT:
            truncated = True
            break
        seen.add(addr)
        magic = _read_field_at(
            addr, "struct rt_memheap_item", item_layout, "magic", 4, endian
        )
        next_addr = _read_field_at(
            addr, "struct rt_memheap_item", item_layout, "next", ptrsize, endian
        )
        if magic is None or next_addr is None:
            corrupt = True
            break
        if magic & ~0x1 != MEMHEAP_MAGIC:
            corrupt = True
            break
        # Reason: kernel list_memheap/memtrace stop before the tailer, whose
        # ``next`` points back at ``block_list``. Counting it inflates used
        # by one (typically a 0-size used sentinel).
        if next_addr == head:
            closed = True
            break
        is_used = bool(magic & 0x1)
        size = next_addr - addr - header_size
        items += 1
        if is_used:
            used += 1
            if memtrace:
                owner = _read_name_at(
                    addr,
                    "struct rt_memheap_item",
                    item_layout,
                    "owner_thread_name",
                    _MEMTRACE_NAME_WIDTH,
                    endian,
                )
                if owner:
                    owner_blocks[owner] = owner_blocks.get(owner, 0) + 1
                    owner_bytes[owner] = owner_bytes.get(owner, 0) + max(size, 0)
        else:
            holes.append(max(size, 0))
        addr = next_addr
    exact_used = exact_total = None
    if closed:
        exact_used, exact_total = _exact_byte_counts(
            truncated=truncated,
            corrupt=corrupt,
            used_bytes=addr + header_size - head - sum(holes),
            total_bytes=addr + header_size - head,
        )
    if items == 0:
        if corrupt or truncated:
            return None
        return HeapWalk(
            used_blocks=0,
            free_blocks=0,
            hole_sizes=[],
            occupancy=[],
            used_bytes=exact_used,
            total_bytes=exact_total,
        )
    return HeapWalk(
        used_blocks=used,
        free_blocks=items - used,
        hole_sizes=holes,
        occupancy=_occupancy_rows(owner_blocks, owner_bytes),
        truncated=truncated,
        corrupt=corrupt,
        used_bytes=exact_used,
        total_bytes=exact_total,
    )


def _walk_slab_pages(kl: KernelLayout) -> HeapWalk | None:
    """Walk slab ``memusage`` page descriptors for a page/zone summary.

    Slab has no chunk-owner ABI, so occupancy is always ``[]``. Contiguous
    ``PAGE_TYPE_FREE`` pages form the free runs reported as holes.
    """
    ptrsize, endian = _arch()
    system_heap = lookup_symbol("system_heap")
    if system_heap is not None:
        slab_layout = kl.structs.get("struct rt_slab")
        if slab_layout is None:
            return None
        try:
            base = int(system_heap)
        except (gdb.error, TypeError, ValueError):
            return None
        heap_start = _read_field_at(
            base, "struct rt_slab", slab_layout, "heap_start", ptrsize, endian
        )
        heap_end = _read_field_at(
            base, "struct rt_slab", slab_layout, "heap_end", ptrsize, endian
        )
        memusage = _read_field_at(
            base, "struct rt_slab", slab_layout, "memusage", ptrsize, endian
        )
    else:
        heap_start = read_int(lookup_symbol("heap_start"))
        heap_end = read_int(lookup_symbol("heap_end"))
        memusage = read_int(lookup_symbol("memusage"))
    page_size = read_macro_int("RT_MM_PAGE_SIZE")
    if page_size is None or page_size <= 0:
        page_size = 4096  # RT-Thread default page size
    if (
        heap_start is None
        or heap_end is None
        or memusage is None
        or heap_end <= heap_start
    ):
        return None
    npages = (heap_end - heap_start) // page_size
    if npages <= 0:
        return None
    truncated = False
    limit = npages
    if npages > GDR_MAX_TRAVERSAL_COUNT:
        truncated = True
        limit = GDR_MAX_TRAVERSAL_COUNT
    free_pages = 0
    used_pages = 0
    runs: list[int] = []
    run = 0
    corrupt = False
    for index in range(limit):
        raw = read_bytes(memusage + index * 4, 4)
        if raw is None:
            corrupt = True
            break
        page_type = int.from_bytes(raw, byteorder=endian) & 0x3
        if page_type == PAGE_TYPE_FREE:
            free_pages += 1
            run += 1
        else:
            if run:
                runs.append(run)
                run = 0
            used_pages += 1
    if run:
        runs.append(run)
    if free_pages == 0 and used_pages == 0:
        return None
    return HeapWalk(
        used_blocks=used_pages,
        free_blocks=free_pages,
        hole_sizes=[run_length * page_size for run_length in runs],
        occupancy=[],
        truncated=truncated,
        corrupt=corrupt,
    )


def walk_system_heap(heap_type: str, kl: KernelLayout) -> HeapWalk | None:
    """Walk the active system-heap allocator with bounded, guarded reads.

    Returns ``None`` when the allocator's block chain is not resolvable so the
    caller keeps the snapshot and marks Blocks/Holes unavailable.
    """
    if heap_type == "small_mem":
        return _walk_small_mem(kl)
    if heap_type == "memheap":
        return _walk_memheap(kl)
    if heap_type == "slab":
        return _walk_slab_pages(kl)
    return None
