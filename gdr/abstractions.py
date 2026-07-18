"""Minimal kernel object abstractions.

These are lightweight dataclasses (not ABCs) that a platform adapter populates
from ``gdb.Value`` objects.  Each implements ``to_dict()`` for table output.

The design follows the Asterinas GDB helper principle: convenience functions
return raw ``gdb.Value`` for user expression drilling, while these dataclasses
are only used internally by aggregate commands for tabulation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class KernelObject:
    """Base dataclass for all kernel objects."""

    name: str = ""
    address: int = 0
    type_code: int = 0

    def to_dict(self) -> dict[str, str | int | None]:
        d = asdict(self)
        d["address"] = hex(self.address) if self.address else "0x0"
        return d


@dataclass
class Thread(KernelObject):
    """Thread object for table output."""

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
    bind_cpu: int = -1
    oncpu: int = -1

    @property
    def stack_used(self) -> int | None:
        """Return stack consumption when direction and saved SP are valid."""
        if not self.stack_addr or not self.stack_size or not self.sp:
            return None
        if self.stack_grows_up is None:
            return None
        used = (
            self.sp - self.stack_addr
            if self.stack_grows_up
            else self.stack_size - (self.sp - self.stack_addr)
        )
        # Reason: a corrupt or stale saved SP must not be presented as a valid
        # stack measurement.
        return used if 0 <= used <= self.stack_size else None


@dataclass
class Semaphore(KernelObject):
    """Semaphore object."""

    value: int = 0


@dataclass
class Mutex(KernelObject):
    """Mutex object."""

    value: int = 0
    hold: int = 0
    owner: str = ""
    original_priority: int = 0


@dataclass
class Timer(KernelObject):
    """Timer object."""

    active: bool = False
    periodic: bool = False
    soft_timer: bool = False
    init_tick: int = 0
    timeout_tick: int = 0
    callback: int = 0


@dataclass
class Event(KernelObject):
    """Event object."""

    set: int = 0


@dataclass
class Mailbox(KernelObject):
    """Mailbox object."""

    size: int = 0
    entry: int = 0
    in_offset: int = 0
    out_offset: int = 0


@dataclass
class MessageQueue(KernelObject):
    """Message queue object."""

    msg_size: int = 0
    max_msgs: int = 0
    entry: int = 0


@dataclass
class MemoryPool(KernelObject):
    """Memory pool object."""

    block_size: int = 0
    block_total_count: int = 0
    block_free_count: int = 0
