"""Minimal kernel object abstractions.

These are lightweight dataclasses (not ABCs) that the RTOS adapter populates
from ``gdb.Value`` objects.  Each implements ``to_dict()`` for table output.

The design follows the Asterinas GDB helper principle: convenience functions
return raw ``gdb.Value`` for user expression drilling, while these dataclasses
are only used internally by aggregate commands for tabulation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import IntEnum


class ThreadState(IntEnum):
    """RT-Thread thread stat values (low 3 bits of ``rt_thread.stat``)."""

    UNKNOWN = -1
    INIT = 0x00
    READY = 0x01
    SUSPEND = 0x02
    RUNNING = 0x03
    CLOSE = 0x04

    @classmethod
    def from_raw(cls, raw: int) -> ThreadState:
        """Map a raw stat byte to a known state, masking non-state bits."""
        try:
            return cls(raw & 0x07)
        except ValueError:
            return cls.UNKNOWN


class TimerState(IntEnum):
    """Timer activation state (derived from ``rt_object.flag``)."""

    UNKNOWN = -1
    INACTIVE = 0x0
    ACTIVE = 0x1


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

    state: int = ThreadState.UNKNOWN
    current_priority: int = 0
    init_priority: int = 0
    sp: int = 0
    stack_addr: int = 0
    stack_size: int = 0
    entry: int = 0
    error: int = 0
    remaining_tick: int = 0
    bind_cpu: int = -1
    oncpu: int = -1


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
