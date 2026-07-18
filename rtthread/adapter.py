"""RT-Thread adapter: gdb.Value → dataclass + convenience functions.

Converts raw ``gdb.Value`` objects into the lightweight dataclasses from
``gdr.abstractions`` for tabulation by aggregate commands, and registers
GDB convenience functions (``$gdr_thread``, ``$gdr_threads``, ``$gdr_object``)
that return ``gdb.Value`` for user expression drilling.

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
from gdr.gdb_bridge import read_bytes, read_cstring, read_int
from gdr.layout import KernelLayout, read_field
from rtthread.layout import (
    RT_OBJECT_CLASS_EVENT,
    RT_OBJECT_CLASS_MAILBOX,
    RT_OBJECT_CLASS_MEMPOOL,
    RT_OBJECT_CLASS_MESSAGEQUEUE,
    RT_OBJECT_CLASS_MUTEX,
    RT_OBJECT_CLASS_SEMAPHORE,
    RT_OBJECT_CLASS_THREAD,
    RT_OBJECT_CLASS_TIMER,
    RT_THREAD_STACK_FILL,
    RT_TIMER_FLAG_ACTIVATED,
    RT_TIMER_FLAG_PERIODIC,
    RT_TIMER_FLAG_SOFT_TIMER,
    ThreadState,
)
from rtthread.navigation import find_object, find_thread, iter_threads

# Module-level reference set by register_adapter()
_kl: KernelLayout | None = None


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
        type_code=RT_OBJECT_CLASS_THREAD,
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
        type_code=RT_OBJECT_CLASS_SEMAPHORE,
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
        type_code=RT_OBJECT_CLASS_MUTEX,
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
        type_code=RT_OBJECT_CLASS_TIMER,
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
        type_code=RT_OBJECT_CLASS_EVENT,
        set=read_int(read_field(val, sl, "set")) or 0,
    )


def value_to_mailbox(val: gdb.Value, layout: KernelLayout) -> Mailbox:
    """Convert a ``struct rt_mailbox`` gdb.Value to ``Mailbox``."""
    sl = layout.structs["struct rt_mailbox"]
    return Mailbox(
        name=read_cstring(read_field(val, sl, "name")) or "",
        address=_get_addr(val),
        type_code=RT_OBJECT_CLASS_MAILBOX,
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
        type_code=RT_OBJECT_CLASS_MESSAGEQUEUE,
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
        type_code=RT_OBJECT_CLASS_MEMPOOL,
        block_size=read_int(read_field(val, sl, "block_size")) or 0,
        block_total_count=read_int(read_field(val, sl, "block_total_count")) or 0,
        block_free_count=read_int(read_field(val, sl, "block_free_count")) or 0,
    )


# ---------------------------------------------------------------------------
# Convenience functions (registered as gdb.Function subclasses)
# ---------------------------------------------------------------------------

# Reason: gdb.Function is only available inside a GDB interpreter.  Guard
# the class definitions so the module is importable outside GDB for static
# analysis and unit testing of the converter functions.
if gdb is not None:

    def _value_to_str(val: gdb.Value) -> str:
        """Extract a C string from a gdb.Value.

        GDB string literals have ``type.code == TYPE_CODE_ARRAY`` (char
        array), not ``TYPE_CODE_STRING``.  The ``string()`` method works
        on both; ``str()`` on a char array returns the quoted literal.
        """
        try:
            return val.string()
        except (gdb.error, TypeError):
            return str(val)

    class GdrThreadFunction(gdb.Function):
        """Return the rt_thread gdb.Value for the named thread.

        Usage in GDB::

            p *$gdr_thread("main")
            p $gdr_thread("worker")->current_priority
        """

        def __init__(self):
            super().__init__("gdr_thread")

        def invoke(self, name: gdb.Value) -> gdb.Value:
            """Find and return the thread gdb.Value.

            Args:
                name: Thread name as a gdb.Value (string).

            Returns:
                The thread's gdb.Value, or a void value if not found.
            """
            if _kl is None:
                return gdb.Value(0)
            name_str = _value_to_str(name)
            result = find_thread(name_str, _kl)
            if result is None:
                return gdb.Value(0)
            return result

    class GdrThreadsFunction(gdb.Function):
        """Return the first thread gdb.Value (use `rtthread threads` for full list).

        Usage in GDB::

            p *$gdr_threads()
        """

        def __init__(self):
            super().__init__("gdr_threads")

        def invoke(self) -> gdb.Value:
            """Return the first thread as a gdb.Value."""
            if _kl is None:
                return gdb.Value(0)
            threads = list(iter_threads(_kl))
            if not threads:
                return gdb.Value(0)
            # Reason: GDB convenience functions can only return scalar or
            # simple values. We return the first thread's address; users
            # should use the `rtthread threads` command for a full listing.
            return threads[0]

    class GdrObjectFunction(gdb.Function):
        """Return a kernel object gdb.Value by type code and name.

        Usage in GDB::

            p *$gdr_object(0x02, "my_sem")
        """

        def __init__(self):
            super().__init__("gdr_object")

        def invoke(self, type_code: gdb.Value, name: gdb.Value) -> gdb.Value:
            """Find and return the object's gdb.Value.

            Args:
                type_code: Object type code (e.g. 0x02 for semaphore).
                name: Object name as a gdb.Value (string).

            Returns:
                The object's gdb.Value, or a void value if not found.
            """
            if _kl is None:
                return gdb.Value(0)
            tc = int(type_code)
            name_str = _value_to_str(name)
            result = find_object(tc, name_str, _kl)
            if result is None:
                return gdb.Value(0)
            return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_adapter(kl: KernelLayout) -> None:
    """Register convenience functions and retain the first layout.

    Args:
        kl: Kernel layout built by ``rtthread.layout.build_layouts``.
    """
    global _kl

    if _kl is not None:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")

    # Instantiate function classes to register them with GDB
    GdrThreadFunction()
    GdrThreadsFunction()
    GdrObjectFunction()
    _kl = kl
