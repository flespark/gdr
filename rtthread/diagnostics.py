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

from typing import TYPE_CHECKING, Literal

from gdr.constants import MAX_TRAVERSAL_COUNT
from gdr.formatting import format_optional_int, format_symbol_or_address
from gdr.gdb_bridge import get_arch_info, lookup_symbol_at, read_bytes, read_int
from gdr.layout import KernelLayout, read_field
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
        active = _walk_list(head, ptrsize, endian, MAX_TRAVERSAL_COUNT)
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
        free_nodes = _walk_list(free, ptrsize, endian, MAX_TRAVERSAL_COUNT)
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
