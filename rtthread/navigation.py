"""RT-Thread kernel object navigation.

This module owns RT-Thread's global symbols, object registry, and traversal
policy. It returns raw ``gdb.Value`` objects so callers can continue with
native GDB expressions and layout-driven pretty-printers.
"""

from __future__ import annotations

from collections.abc import Iterator

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import eval_safe, lookup_symbol, read_cstring
from gdr.layout import KernelLayout, ListHook, iter_list, read_field
from rtthread.layout import (
    RT_OBJECT_CLASS_THREAD,
    RT_OBJECT_CLASS_TIMER,
    object_information_layout,
)


def get_object_information(type_code: int, kl: KernelLayout) -> gdb.Value | None:
    """Obtain RT-Thread object registry information for an object type code.

    Calls ``rt_object_get_information()`` first, then falls back to the static
    ``_object_container`` array when the function is unavailable.
    """
    info_layout = object_information_layout(kl)
    if info_layout is None:
        return None

    # Reason: rt_object_get_information is RTM_EXPORT'd and present in all
    # RT-Thread builds, making it the most reliable entry point.
    info_ptr = eval_safe(f"rt_object_get_information({type_code})")
    if info_ptr is not None and int(info_ptr) != 0:
        try:
            return info_ptr.dereference()
        except (gdb.error, gdb.MemoryError):
            pass

    container = lookup_symbol("_object_container")
    if container is None:
        return None
    try:
        # Reason: the array length is config-dependent (conditional enum), so
        # iterate until an unknown type or the end of the array is reached.
        for index in range(16):
            entry = container[index]
            entry_type = read_field(entry, info_layout, "type")
            if entry_type is not None and int(entry_type) == type_code:
                return entry
    except (gdb.error, gdb.MemoryError, IndexError, TypeError, ValueError):
        pass

    return None


def iter_objects(type_code: int, kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate all RT-Thread objects of a given type."""
    type_info = kl.object_types.get(type_code)
    if type_info is None or not type_info.enabled:
        return

    info = get_object_information(type_code, kl)
    info_layout = object_information_layout(kl)
    if info is None or info_layout is None:
        return

    object_list = read_field(info, info_layout, "object_list")
    if object_list is None:
        return
    hook = ListHook(
        head_expr="",
        node_path=type_info.list_path,
        container_type=type_info.struct_name,
        next_path=type_info.next_path,
    )
    yield from iter_list(object_list, hook)


def find_object(type_code: int, name: str, kl: KernelLayout) -> gdb.Value | None:
    """Find an RT-Thread object by type code and C-string name."""
    type_info = kl.object_types.get(type_code)
    if type_info is None:
        return None
    layout = kl.structs.get(type_info.struct_name)
    if layout is None:
        return None

    for obj in iter_objects(type_code, kl):
        object_name = read_cstring(read_field(obj, layout, "name"))
        if object_name == name:
            return obj
    return None


def iter_threads(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate all RT-Thread thread objects."""
    yield from iter_objects(RT_OBJECT_CLASS_THREAD, kl)


def find_thread(name: str, kl: KernelLayout) -> gdb.Value | None:
    """Find an RT-Thread thread by name."""
    return find_object(RT_OBJECT_CLASS_THREAD, name, kl)


def get_current_thread() -> gdb.Value | None:
    """Return the currently running RT-Thread thread, if available."""
    ptr = lookup_symbol("rt_current_thread")
    if ptr is None or int(ptr) == 0:
        return None
    try:
        return ptr.dereference()
    except (gdb.error, gdb.MemoryError):
        return None


def iter_timers(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate RT-Thread timers via active lists and the object registry."""
    seen: set[int] = set()

    for hook_name in ("timer_list", "soft_timer_list"):
        hook = kl.list_hooks.get(hook_name)
        if hook is None:
            continue
        head = eval_safe(hook.head_expr)
        if head is None:
            continue
        for timer in iter_list(head, hook):
            seen.add(int(timer.address))
            yield timer

    # Reason: some RT-Thread 4.0.x builds register timers in the object
    # container before they appear in active timer lists at our breakpoint.
    for timer in iter_objects(RT_OBJECT_CLASS_TIMER, kl):
        address = int(timer.address)
        if address not in seen:
            yield timer


def get_tick() -> int | None:
    """Read the current RT-Thread kernel tick."""
    value = eval_safe("rt_tick_get()")
    if value is not None:
        try:
            return int(value)
        except (gdb.error, ValueError):
            return None

    tick = lookup_symbol("rt_tick")
    if tick is not None:
        try:
            return int(tick)
        except (gdb.error, ValueError):
            pass
    return None
