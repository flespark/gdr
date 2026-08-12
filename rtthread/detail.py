"""RT-Thread single-object detail builders.

Produces vertical ``(key, value)`` pairs for the ``rtt <object> <name>``
detail commands. Field names and version conditionals stay in the RT-Thread
adapter; the generic renderer in ``gdr.commands`` only prints the pairs.
"""

from __future__ import annotations

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
from gdr.gdb_bridge import lookup_symbol_at
from rtthread.layout import ThreadState


def _symbol_or_hex(address: int) -> str:
    """Render an address as ``<symbol+0>`` when symbolised, else hex."""
    symbol = lookup_symbol_at(address) if address else None
    return f"<{symbol}>" if symbol else hex(address)


def _optional_int(value: int | None) -> str:
    """Render an optional int as its value or ``N/A``."""
    return str(value) if value is not None else "N/A"


def _common_pairs(obj) -> list[tuple[str, str]]:
    """Name, address, and semantic object type for any kernel object."""
    return [
        ("Name", obj.name),
        ("Address", hex(obj.address)),
        ("Type", type(obj).__name__),
    ]


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
        ("Used", _optional_int(thread.stack_used)),
        ("HighWater", _optional_int(thread.max_stack_used)),
        ("Entry", _symbol_or_hex(thread.entry)),
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
        ("Type", "soft" if timer.soft_timer else "hard"),
        ("InitTick", str(timer.init_tick)),
        ("TimeoutTick", str(timer.timeout_tick)),
        ("Callback", _symbol_or_hex(timer.callback)),
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


def mailbox_detail(mailbox: Mailbox) -> list[tuple[str, str]]:
    """Detail pairs for a mailbox ring-buffer position."""
    return _common_pairs(mailbox) + [
        ("Entry", str(mailbox.entry)),
        ("Size", str(mailbox.size)),
        ("InOffset", str(mailbox.in_offset)),
        ("OutOffset", str(mailbox.out_offset)),
    ]


def messagequeue_detail(msgqueue: MessageQueue) -> list[tuple[str, str]]:
    """Detail pairs for a message queue's load and capacity."""
    return _common_pairs(msgqueue) + [
        ("Entry", str(msgqueue.entry)),
        ("MsgSize", str(msgqueue.msg_size)),
        ("MaxMsgs", str(msgqueue.max_msgs)),
    ]


def memorypool_detail(pool: MemoryPool) -> list[tuple[str, str]]:
    """Detail pairs for a memory pool's block accounting."""
    return _common_pairs(pool) + [
        ("BlockSize", str(pool.block_size)),
        ("Total", str(pool.block_total_count)),
        ("Free", str(pool.block_free_count)),
    ]
