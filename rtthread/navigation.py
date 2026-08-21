"""RT-Thread kernel object navigation.

This module owns RT-Thread's global symbols, object registry, traversal
policy, and the halted system-heap snapshot. It returns raw ``gdb.Value``
objects so callers can continue with native GDB expressions and
layout-driven pretty-printers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import lookup_symbol, read_cstring, read_int
from gdr.layout import (
    KernelLayout,
    ListHook,
    StructLayout,
    iter_list,
    read_field,
    resolve_list_head,
)
from rtthread.layout import object_information_layout


def _object_code(kl: KernelLayout, name: str) -> int | None:
    """Return an active target's numeric code for a semantic object name."""
    return kl.object_codes.get(name)


def _object_container() -> gdb.Value | None:
    """Return the object-registry table, tolerating the 3.1.x symbol name.

    RT-Thread 4.x declares ``static _object_container`` while the 3.1 series
    kept the historical ``static rt_object_container`` spelling. Both are
    looked up as identifiers so ``rt_object_get_information()`` (an inferior
    call) is never needed.
    """
    for name in ("_object_container", "rt_object_container"):
        container = lookup_symbol(name)
        if container is not None:
            return container
    return None


def get_object_information(type_code: int, kl: KernelLayout) -> gdb.Value | None:
    """Obtain RT-Thread object registry information for an object type code.

    Reads the static ``_object_container`` / ``rt_object_container`` array.
    The kernel helper ``rt_object_get_information()`` is not called: that
    would resume the inferior.
    """
    info_layout = object_information_layout(kl)
    if info_layout is None:
        return None

    container = _object_container()
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


@dataclass
class HeapSnapshot:
    """Halted-state system-heap summary for ``rtt system`` and ``rtt heap``.

    ``algorithm`` is the probed allocator name; ``used``/``total``/``max_used``
    are ``None`` when the target does not expose the corresponding counter.
    """

    algorithm: str  # "small_mem", "slab", "memheap", "none"
    used: int | None = None
    total: int | None = None
    max_used: int | None = None


def _deref_system_heap() -> gdb.Value | None:
    """Return the dereferenced ``system_heap`` handle, or the value itself.

    4.1 small_mem/slab declare ``system_heap`` as a pointer typedef while
    memheap-as-heap declares it as a plain ``struct rt_memheap`` value.
    """
    heap = lookup_symbol("system_heap")
    if heap is None:
        return None
    try:
        if heap.type.strip_typedefs().code == gdb.TYPE_CODE_PTR and int(heap) != 0:
            return heap.dereference()
        return heap
    except (gdb.error, gdb.MemoryError, TypeError, ValueError):
        return None


def _memheap_snapshot(
    heap: gdb.Value | None, layout: StructLayout | None
) -> HeapSnapshot | None:
    """Read pool/available/max from a memheap object, or ``None`` if unreadable.

    A missing ``struct rt_memheap`` layout must not reach ``read_field``: that
    accessor assumes a concrete ``StructLayout`` and would raise
    ``AttributeError`` instead of degrading.
    """
    if heap is None or layout is None:
        return None
    pool = read_int(read_field(heap, layout, "pool_size"))
    available = read_int(read_field(heap, layout, "available_size"))
    max_used = read_int(read_field(heap, layout, "max_used_size"))
    used = (
        max(pool - available, 0) if pool is not None and available is not None else None
    )
    return HeapSnapshot(algorithm="memheap", used=used, total=pool, max_used=max_used)


def _system_heap_snapshot(heap_type: str, kl: KernelLayout) -> HeapSnapshot | None:
    """Snapshot the 4.1 ``system_heap`` handle (rt_memory or rt_memheap)."""
    heap = _deref_system_heap()
    if heap is None:
        return None
    if heap_type == "memheap":
        return _memheap_snapshot(heap, kl.structs.get("struct rt_memheap"))
    # small_mem/slab embed ``struct rt_memory`` at offset 0, so the totals read
    # through the handle's declared ``struct rt_memory`` type.
    try:
        total = read_int(heap["total"])
        used = read_int(heap["used"])
        max_used = read_int(heap["max"])
    except (gdb.error, gdb.MemoryError, TypeError, KeyError, IndexError, ValueError):
        return HeapSnapshot(algorithm=heap_type)
    return HeapSnapshot(algorithm=heap_type, used=used, total=total, max_used=max_used)


def _globals_small_mem_snapshot(algorithm: str) -> HeapSnapshot | None:
    """Snapshot the 4.0 small_mem / slab static globals.

    small_mem exposes ``mem_size_aligned`` for the total; slab stores only
    ``heap_start``/``heap_end`` (both are ``rt_ubase_t`` globals), so the total
    degrades to ``heap_end - heap_start`` when the aligned size is absent.
    """
    used = read_int(lookup_symbol("used_mem"))
    max_used = read_int(lookup_symbol("max_mem"))
    total = read_int(lookup_symbol("mem_size_aligned"))
    if total is None:
        heap_end = read_int(lookup_symbol("heap_end"))
        heap_start = read_int(lookup_symbol("heap_start"))
        if heap_end is not None and heap_start is not None:
            total = heap_end - heap_start
    if used is None and total is None and max_used is None:
        return None
    return HeapSnapshot(algorithm=algorithm, used=used, total=total, max_used=max_used)


def _globals_memheap_snapshot(kl: KernelLayout) -> HeapSnapshot | None:
    """Snapshot the 4.0 ``_heap`` memheap-as-heap static object."""
    return _memheap_snapshot(
        lookup_symbol("_heap"), kl.structs.get("struct rt_memheap")
    )


def get_heap_snapshot(heap_type: str, kl: KernelLayout) -> HeapSnapshot:
    """Read a halted-state system-heap snapshot without inferior calls.

    Prefers the 4.1 ``system_heap`` handle, then 4.0 static globals, mirroring
    the kernel's ``rt_memory_info`` / ``rt_memheap_info`` formulas. Only the
    system heap is reported; ``struct rt_memory`` objects that are not the
    system allocator are deliberately excluded.
    """
    if heap_type != "none":
        snap = _system_heap_snapshot(heap_type, kl)
        if snap is not None:
            return snap
        if heap_type == "memheap":
            snap = _globals_memheap_snapshot(kl)
        else:
            snap = _globals_small_mem_snapshot(heap_type)
        if snap is not None:
            return snap
    return HeapSnapshot(algorithm=heap_type)
