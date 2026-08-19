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

from gdr.gdb_bridge import lookup_symbol, read_cstring, read_int
from gdr.layout import KernelLayout, ListHook, iter_list, read_field, resolve_list_head
from rtthread.layout import object_information_layout


def _object_code(kl: KernelLayout, name: str) -> int | None:
    """Return an active target's numeric code for a semantic object name."""
    return kl.object_codes.get(name)


def get_object_information(type_code: int, kl: KernelLayout) -> gdb.Value | None:
    """Obtain RT-Thread object registry information for an object type code.

    Reads the static ``_object_container`` array. The kernel helper
    ``rt_object_get_information()`` is not called: that would resume the
    inferior.
    """
    info_layout = object_information_layout(kl)
    if info_layout is None:
        return None

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
        head_symbol="",
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


MAX_SUSPEND_THREADS = 256


def iter_suspend_threads(
    head_value: gdb.Value,
) -> Iterator[gdb.Value]:
    """Iterate threads linked through one wait-list head.

    Each wait-list node is a ``struct rt_thread.tlist`` embedded node, so
    ``container_of`` recovers the containing thread. Traversal is bounded by
    :data:`MAX_SUSPEND_THREADS` and inherits cycle / bad-pointer / node-limit
    protection from :func:`gdr.layout.iter_list`.
    """
    hook = ListHook(
        head_symbol="",
        node_path=("tlist",),
        container_type="struct rt_thread",
        next_path=("next",),
    )
    yield from iter_list(head_value, hook, max_count=MAX_SUSPEND_THREADS)


def suspend_thread_names(
    value: gdb.Value, kl: KernelLayout, struct_name: str, field_name: str
) -> list[str]:
    """Return a stable waiter-name list for one object's wait list.

    Args:
        value: The object whose wait list is inspected.
        kl: Active kernel layout.
        struct_name: Struct name of the object (e.g. ``struct rt_semaphore``).
        field_name: Layout field holding the wait-list head.

    Returns:
        Thread names in list order; unreadable names render as ``<invalid>``.
        An empty list means the list is empty or the struct/field is absent
        (callers distinguish those cases from the layout).
    """
    sl = kl.structs.get(struct_name)
    thread_layout = kl.structs.get("struct rt_thread")
    if sl is None or thread_layout is None:
        return []
    head = read_field(value, sl, field_name)
    if head is None:
        return []
    names: list[str] = []
    for thread in iter_suspend_threads(head):
        name = read_cstring(read_field(thread, thread_layout, "name"))
        names.append(name or "<invalid>")
    return names


def iter_threads(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate all RT-Thread thread objects."""
    type_code = _object_code(kl, "thread")
    if type_code is not None:
        yield from iter_objects(type_code, kl)


def find_thread(name: str, kl: KernelLayout) -> gdb.Value | None:
    """Find an RT-Thread thread by name."""
    type_code = _object_code(kl, "thread")
    return find_object(type_code, name, kl) if type_code is not None else None


def iter_object_names(kind: str, kl: KernelLayout) -> Iterator[str]:
    """Yield object names of a semantic kind for command completion.

    Args:
        kind: Semantic kind (``task``, ``semaphore``, ``mutex``, ``event``,
            ``mailbox``, ``msgqueue``, ``mempool``, ``timer``).
        kl: Active kernel layout.

    Traverses the live kernel registry so tab-completion reflects objects that
    exist right now, including version/config-conditional ones.
    """
    if kind == "task":
        kind = "thread"
    if kind == "timer":
        for value in iter_timers(kl):
            name = read_cstring(
                read_field(value, kl.structs["struct rt_timer"], "name")
            )
            if name:
                yield name
        return

    type_code = kl.object_codes.get(kind)
    if type_code is None:
        return
    type_info = kl.object_types.get(type_code)
    if type_info is None or not type_info.enabled:
        return
    layout = kl.structs.get(type_info.struct_name)
    if layout is None:
        return
    for value in iter_objects(type_code, kl):
        name = read_cstring(read_field(value, layout, "name"))
        if name:
            yield name


def _dereference_thread(ptr: gdb.Value | None) -> gdb.Value | None:
    """Dereference a non-null RT-Thread handle safely."""
    if ptr is None:
        return None
    try:
        if int(ptr) == 0:
            return None
        return ptr.dereference()
    except (gdb.error, gdb.MemoryError, TypeError, ValueError):
        return None


def _current_thread_from_cpu(cpu: gdb.Value | None) -> gdb.Value | None:
    """Read ``current_thread`` from an RT-Thread per-CPU descriptor."""
    if cpu is None:
        return None
    try:
        if cpu.type.strip_typedefs().code == gdb.TYPE_CODE_PTR:
            if int(cpu) == 0:
                return None
            cpu = cpu.dereference()
        return _dereference_thread(cpu["current_thread"])
    except (gdb.error, gdb.MemoryError, IndexError, TypeError, ValueError):
        return None


def _cpu_from_table(table: gdb.Value | None, cpu_id: int) -> gdb.Value | None:
    """Return a per-CPU descriptor from an array or pointer table."""
    if table is None:
        return None
    try:
        table_type = table.type.strip_typedefs()
        if table_type.code == gdb.TYPE_CODE_ARRAY:
            return table[cpu_id]
        if table_type.code == gdb.TYPE_CODE_PTR and int(table) != 0:
            return table[cpu_id]
    except (gdb.error, gdb.MemoryError, IndexError, TypeError, ValueError):
        pass
    return None


def _selected_cpu_id() -> int:
    """Return GDB's selected CPU index without resuming the inferior.

    Prefers the halted core's affinity register (Cortex-A ``mpidr`` /
    ``mpidr_el1``, RISC-V ``mhartid``), then GDB's selected thread number.
    Falls back to CPU 0 when neither is available.
    """
    if gdb is None:
        return 0
    try:
        frame = gdb.selected_frame()
    except (gdb.error, AttributeError, RuntimeError):
        frame = None
    if frame is not None:
        for name, mask in (("mhartid", None), ("mpidr", 0xFF), ("mpidr_el1", 0xFF)):
            try:
                raw = int(frame.read_register(name))
            except (gdb.error, AttributeError, TypeError, ValueError):
                continue
            cpu_id = raw if mask is None else raw & mask
            if cpu_id >= 0:
                return cpu_id
    try:
        thread = gdb.selected_thread()
        if thread is not None and int(thread.num) >= 1:
            return int(thread.num) - 1
    except (gdb.error, AttributeError, TypeError, ValueError):
        pass
    return 0


def _smp_current_thread() -> gdb.Value | None:
    """Return the current thread for GDB's selected RT-Thread CPU."""
    cpu_id = _selected_cpu_id()
    if cpu_id < 0:
        return None

    # Reason: backing storage changed from rt_cpus[] / rt_cpu_table to _cpus[]
    # across 3.1 and 4.x; rt_cpu_index() is not called because that would
    # resume the inferior.
    for symbol in ("_cpus", "rt_cpus", "rt_cpu_table", "_rt_cpus"):
        cpu = _cpu_from_table(lookup_symbol(symbol), cpu_id)
        current = _current_thread_from_cpu(cpu)
        if current is not None:
            return current
    return None


def get_current_thread() -> gdb.Value | None:
    """Return the thread executing on GDB's selected RT-Thread CPU.

    Non-SMP kernels expose the scalar ``rt_current_thread``. SMP kernels keep
    the handle in the selected CPU's ``struct rt_cpu.current_thread``.
    """
    current = _dereference_thread(lookup_symbol("rt_current_thread"))
    if current is not None:
        return current

    # Reason: FreeRTOS stores current handles in pxCurrentTCBs[] and Zephyr
    # uses _kernel.cpus[].current; their layouts must stay in their adapters.
    return _smp_current_thread()


def iter_timers(kl: KernelLayout) -> Iterator[gdb.Value]:
    """Iterate RT-Thread timers via active lists and the object registry."""
    seen: set[int] = set()

    for hook_name in ("timer_list", "soft_timer_list"):
        hook = kl.list_hooks.get(hook_name)
        if hook is None:
            continue
        head = resolve_list_head(hook)
        if head is None:
            continue
        for timer in iter_list(head, hook):
            seen.add(int(timer.address))
            yield timer

    # Reason: some RT-Thread 4.0.x builds register timers in the object
    # container before they appear in active timer lists at our breakpoint.
    timer_type = _object_code(kl, "timer")
    if timer_type is None:
        return
    for timer in iter_objects(timer_type, kl):
        address = int(timer.address)
        if address not in seen:
            yield timer


def get_tick() -> int | None:
    """Read the current RT-Thread kernel tick without resuming the inferior."""
    tick = lookup_symbol("rt_tick")
    if tick is not None:
        try:
            return int(tick)
        except (gdb.error, TypeError, ValueError):
            pass

    # Reason: SMP builds alias rt_tick to rt_cpu_index(0)->tick; the UP
    # symbol is absent, so read CPU0's tick field from the per-CPU table.
    for symbol in ("_cpus", "rt_cpus", "rt_cpu_table", "_rt_cpus"):
        cpu = _cpu_from_table(lookup_symbol(symbol), 0)
        if cpu is None:
            continue
        try:
            if cpu.type.strip_typedefs().code == gdb.TYPE_CODE_PTR:
                if int(cpu) == 0:
                    continue
                cpu = cpu.dereference()
            return int(cpu["tick"])
        except (gdb.error, gdb.MemoryError, IndexError, TypeError, ValueError):
            continue
    return None


def _memheap_used(kl: KernelLayout) -> int | None:
    """Return used bytes from ``_heap`` when memheap is the system heap."""
    heap = lookup_symbol("_heap")
    if heap is None:
        return None
    layout = kl.structs.get("struct rt_memheap")
    if layout is not None:
        pool = read_int(read_field(heap, layout, "pool_size"))
        available = read_int(read_field(heap, layout, "available_size"))
    else:
        try:
            pool = read_int(heap["pool_size"])
            available = read_int(heap["available_size"])
        except (TypeError, ValueError, KeyError, IndexError):
            return None
    if pool is None or available is None:
        return None
    return max(pool - available, 0)


def _memory_object_used(kl: KernelLayout) -> int | None:
    """Sum ``used`` across 4.1 ``struct rt_memory`` heap objects."""
    type_code = kl.object_codes.get("memory")
    layout = kl.structs.get("struct rt_memory")
    if type_code is None or layout is None:
        return None
    total = 0
    found = False
    for value in iter_objects(type_code, kl):
        used = read_int(read_field(value, layout, "used"))
        if used is None:
            continue
        total += used
        found = True
    return total if found else None


def get_heap_used(heap_type: str, kl: KernelLayout) -> int | None:
    """Read system-heap used bytes from static kernel state.

    small_mem/slab expose ``used_mem``; memheap-as-heap uses ``_heap``;
    4.1 ``struct rt_memory`` objects contribute their ``used`` fields.
    ``rt_memory_info()`` is not called.
    """
    if heap_type in ("small_mem", "slab", "none"):
        used = read_int(lookup_symbol("used_mem"))
        if used is not None:
            return used
    if heap_type in ("memheap", "none"):
        used = _memheap_used(kl)
        if used is not None:
            return used
    return _memory_object_used(kl)
