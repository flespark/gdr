"""Kernel object navigation primitives.

Provides RTOS-agnostic functions for locating kernel objects by navigating
global symbols and object containers.  All functions take a ``KernelLayout``
so they never hardcode field names or struct layouts.

Usage (inside GDB)::

    from gdr.kernel import iter_threads, find_thread
    for thr in iter_threads(kl):
        ...
"""

from __future__ import annotations

from collections.abc import Iterator

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import eval_safe, lookup_symbol, read_cstring
from gdr.layout import KernelLayout, ListHook, iter_list, read_field


def get_object_information(type_code: int) -> gdb.Value | None:
    """Obtain the ``rt_object_information`` for a given object type code.

    Tries calling the kernel's ``rt_object_get_information()`` function first
    (reliable when debug info or RTM_EXPORT symbols are present).  Falls back
    to searching the static ``_object_container`` array.

    Args:
        type_code: One of the ``RT_Object_Class_*`` constants.

    Returns:
        Dereferenced ``gdb.Value`` of ``struct rt_object_information``,
        or ``None`` if not found.
    """
    # Reason: rt_object_get_information is RTM_EXPORT'd and present in all
    # RT-Thread builds, making it the most reliable entry point.
    info_ptr = eval_safe(f"rt_object_get_information({type_code})")
    if info_ptr is not None and int(info_ptr) != 0:
        try:
            return info_ptr.dereference()
        except (gdb.error, gdb.MemoryError):
            pass

    # Fallback: search _object_container static array by type field.
    container = lookup_symbol("_object_container")
    if container is None:
        return None
    try:
        # Reason: the array length is config-dependent (conditional enum),
        # so we iterate until we hit an unknown type or run out of elements.
        for i in range(16):
            entry = container[i]
            if int(entry["type"]) == type_code:
                return entry
    except (gdb.error, gdb.MemoryError, IndexError):
        pass

    return None


def iter_objects(type_code: int, kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate all kernel objects of a given type.

    Args:
        type_code: Object type constant (e.g. ``RT_OBJECT_CLASS_THREAD``).
        kl: Kernel layout with object type infos.

    Yields:
        Dereferenced ``gdb.Value`` of each container struct.
    """
    type_info = kl.object_types.get(type_code)
    if type_info is None or not type_info.enabled:
        return

    info = get_object_information(type_code)
    if info is None:
        return

    obj_list = info["object_list"]
    hook = ListHook(
        head_expr="",
        node_path=type_info.list_path,
        container_type=type_info.struct_name,
    )
    yield from iter_list(obj_list, hook)


def find_object(type_code: int, name: str, kl: KernelLayout) -> gdb.Value | None:
    """Find a kernel object by type and name.

    Args:
        type_code: Object type constant.
        name: Object name (C string match).
        kl: Kernel layout.

    Returns:
        ``gdb.Value`` of the matching object, or ``None``.
    """
    type_info = kl.object_types.get(type_code)
    if type_info is None:
        return None
    layout = kl.structs.get(type_info.struct_name)
    if layout is None:
        return None

    for obj in iter_objects(type_code, kl):
        name_val = read_field(obj, layout, "name")
        obj_name = read_cstring(name_val)
        if obj_name == name:
            return obj
    return None


def iter_threads(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate all thread objects in the kernel object container."""
    from rtthread.layout import RT_OBJECT_CLASS_THREAD

    yield from iter_objects(RT_OBJECT_CLASS_THREAD, kl)


def find_thread(name: str, kl: KernelLayout) -> gdb.Value | None:
    """Find a thread by name.

    Args:
        name: Thread name (e.g. ``"main"``).
        kl: Kernel layout.

    Returns:
        ``gdb.Value`` of ``struct rt_thread``, or ``None``.
    """
    from rtthread.layout import RT_OBJECT_CLASS_THREAD

    return find_object(RT_OBJECT_CLASS_THREAD, name, kl)


def get_current_thread() -> gdb.Value | None:
    """Return the currently running thread (``rt_current_thread``).

    Returns:
        Dereferenced ``gdb.Value`` of ``struct rt_thread``, or ``None``
        if ``rt_current_thread`` is NULL or not found.
    """
    ptr = lookup_symbol("rt_current_thread")
    if ptr is None:
        return None
    addr = int(ptr)
    if addr == 0:
        return None
    try:
        return ptr.dereference()
    except (gdb.error, gdb.MemoryError):
        return None


def iter_timers(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate timers via active timer lists plus the object registry.

    Args:
        kl: Kernel layout with timer list hooks.

    Yields:
        Dereferenced ``gdb.Value`` of ``struct rt_timer``.
    """
    from rtthread.layout import RT_OBJECT_CLASS_TIMER

    seen: set[int] = set()

    hook = kl.list_hooks.get("timer_list")
    if hook is not None:
        head = eval_safe(hook.head_expr)
        if head is not None:
            for timer in iter_list(head, hook):
                seen.add(int(timer.address))
                yield timer

    # Also iterate soft timer list if present
    soft_hook = kl.list_hooks.get("soft_timer_list")
    if soft_hook is not None:
        soft_head = eval_safe(soft_hook.head_expr)
        if soft_head is not None:
            for timer in iter_list(soft_head, soft_hook):
                seen.add(int(timer.address))
                yield timer

    # Reason: some RT-Thread 4.0.x builds register timers in the object
    # container before they appear in the active timer lists at our breakpoint.
    for timer in iter_objects(RT_OBJECT_CLASS_TIMER, kl):
        addr = int(timer.address)
        if addr not in seen:
            yield timer


def get_tick() -> int | None:
    """Read the current kernel tick via ``rt_tick_get()``."""
    val = eval_safe("rt_tick_get()")
    if val is not None:
        try:
            return int(val)
        except (gdb.error, ValueError):
            return None
    # Fallback: read rt_tick static variable (non-SMP only)
    tick = lookup_symbol("rt_tick")
    if tick is not None:
        try:
            return int(tick)
        except (gdb.error, ValueError):
            pass
    return None
