"""Safe traversal of FreeRTOS scheduler lists and system globals.

Also owns the six-channel kernel-object discovery model: every object the
commands can name or count comes from one of the ``iter_*`` channels below,
and each :class:`DiscoveredObject` carries the channel it was found by so
rendering can show provenance instead of pretending the enumeration is
complete (FreeRTOS keeps no global registry for most object kinds).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from gdr.constants import GDR_MAX_CSTRING_LENGTH, GDR_MAX_TRAVERSAL_COUNT
from gdr.gdb_bridge import (
    get_arch_info,
    is_plain_identifier,
    lookup_symbol,
    read_cstring,
    read_int,
    safe_dereference,
    safe_int,
    value_address,
    warn,
)
from gdr.layout import member_offset, read_field, read_path

# TODO: replace by exception guard
# Exception types we degrade from during scheduler-list traversal.  Stored at
# module scope so the tuple stays computable when imported outside GDB (where
# ``gdb`` is ``None``, which would otherwise turn a handler into an
# AttributeError).  Anything outside this set intentionally bubbles to a
# command/function guard for a full diagnostic.
if gdb is not None:
    _TRAVERSAL_ERRORS = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
    )
else:
    _TRAVERSAL_ERRORS = (IndexError, TypeError, ValueError)


def _owner_task(pointer, layout: FreeRtosLayout):
    """Cast ListItem.pvOwner (void *) back to the DWARF TCB type.

    The TCB type name comes from the layout, not a hard-coded literal, so a
    renamed/re-typed kernel struct stays correct.
    """
    return _cast_owner(pointer, layout, "struct tskTaskControlBlock")


def _array_item(value, index):
    try:
        return value[index]
    except _TRAVERSAL_ERRORS:
        return None


_SECTION_RANGE_RE = re.compile(r"0x([0-9a-fA-F]+)\s*-\s*0x([0-9a-fA-F]+)")


def _mapped_ranges() -> tuple[tuple[int, int], ...]:
    """Return loadable ELF section ranges from ``info files``, or empty.

    An empty result means the map is unknown (unit tests, or GDB not ready),
    so callers skip the range check rather than inventing a RAM window.
    """
    if gdb is None:
        return ()
    try:
        output = gdb.execute("info files", to_string=True)
    except Exception:
        return ()
    ranges: list[tuple[int, int]] = []
    for match in _SECTION_RANGE_RE.finditer(output or ""):
        low = int(match.group(1), 16)
        high = int(match.group(2), 16)
        if high > low:
            ranges.append((low, high))
    return tuple(ranges)


def _iter_list(
    head,
    layout: FreeRtosLayout,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
    owner_converter=None,
) -> Iterator:
    """Yield owner structs from a List_t, stopping on corruption or a bound.

    The default owner converter casts ``pvOwner`` to the TCB type; timer
    lists carry a ``Timer_t *`` owner instead, so channels may pass their own
    converter (resolved at call time so tests can monkeypatch ``_owner_task``).
    """
    if head is None:
        return
    try:
        list_layout = layout.structs["struct xLIST"]
        end = read_field(head, list_layout, "end")
        end_addr = value_address(end)
        mini_layout = layout.structs["struct xMINI_LIST_ITEM"]
        node = read_field(end, mini_layout, "next")
        seen: set[int] = set()
        ranges = _mapped_ranges()
        for _ in range(max_count):
            node_addr = safe_int(node)
            if not node_addr or node_addr == end_addr:
                return
            # Reason: a corrupt next pointer into the NULL page or outside
            # every loadable section is not a list node; stop before
            # dereference. Skip the check when no map is available so unit
            # tests with synthetic addresses still exercise the walk.
            if ranges and not any(low <= node_addr < high for low, high in ranges):
                warn(
                    f"FreeRTOS list traversal stopped at out-of-range node {node_addr:#x}"
                )
                return
            if node_addr in seen:
                warn(f"FreeRTOS list traversal stopped at repeated node {node_addr:#x}")
                return
            seen.add(node_addr)
            item = safe_dereference(node)
            if item is None:
                warn(f"FreeRTOS list traversal stopped at invalid node {node_addr:#x}")
                return
            item_layout = layout.structs["struct xLIST_ITEM"]
            owner = (owner_converter or _owner_task)(
                read_field(item, item_layout, "owner"), layout
            )
            if owner is not None:
                yield owner
            node = read_field(item, item_layout, "next")
        warn(f"FreeRTOS list traversal truncated after {max_count} nodes")
    except _TRAVERSAL_ERRORS:
        warn("FreeRTOS list traversal stopped because list data is unreadable")


def _ready_heads(layout: FreeRtosLayout) -> Iterator:
    table = lookup_symbol(layout.lists["ready"])
    if table is None:
        return
    # Reason: the priority count come from the probed config (the DWARF array
    # bound of pxReadyTasksLists), never a guessed (0, 256) fallback which
    # would wastefully scan empty slots and swallow warnings on a small target.
    count = layout.config.max_priorities
    if count is None or count <= 0:
        warn("FreeRTOS ready-queue priority count is unknown; skipping ready list")
        return
    for index in range(count):
        item = _array_item(table, index)
        if item is None:
            return
        yield item


def _head(name: str, layout: FreeRtosLayout):
    value = lookup_symbol(layout.lists[name])
    if name in ("delayed_current", "delayed_overflow"):
        return safe_dereference(value)
    return value


def iter_tasks(layout: FreeRtosLayout) -> Iterator[tuple[object, str, int | None]]:
    """Yield ``(TCB, state, core)`` exactly once per target address.

    Every unique TCB is classified by the precise per-TCB state algorithm
    (``task_state``) rather than the list it happened to be reached from; a
    task can sit on one list while its state is determined by another (e.g. a
    notification-blocked task's ``xEventListItem`` sits on ``xSuspendedTaskList``
    but it is ``Blocked``, not ``Suspended``).
    """
    seen: set[int] = set()
    sources = [_iter_list(head, layout) for head in _ready_heads(layout)]
    for key in (
        "delayed_1",
        "delayed_2",
        "delayed_current",
        "delayed_overflow",
        "pending",
        "suspended",
        "termination",
    ):
        head = _head(key, layout)
        if head is not None:
            sources.append(_iter_list(head, layout))
    for iterator in sources:
        for task in iterator:
            address = value_address(task)
            if not address or address in seen:
                continue
            seen.add(address)
            yield task, *task_state(task, layout)


def _next_global(name: str, deref: bool = False) -> int | None:
    """Return the address of a scheduler-list container, or ``None``.

    ``pxDelayedTaskList`` / ``pxOverflowDelayedTaskList`` are pointers to the
    *active* list, so ListItem containers point at the dereferenced value;
    every other list is a plain ``List_t`` symbol.
    """
    value = lookup_symbol(name)
    if value is None:
        return None
    if deref:
        value = safe_dereference(value)
        if value is None:
            return None
    return value_address(value)


def task_state(tcb, layout: FreeRtosLayout) -> tuple[str, int | None]:
    """Return ``(state_string, core_index_or_None)`` per the \u00a73.3 algorithm.

    Strictly mirrors ``eTaskGetState`` (tasks.c): checks the current-task/run
    state first, then the event list item's container, then the state list
    item's container against the delayed/suspended/termination lists, and
    finishes with a default.  State strings are exactly ``Running``,
    ``Running(yielding)``, ``Ready``, ``Blocked``, ``Suspended``, ``Deleted``.
    """
    sl = layout.structs["struct tskTaskControlBlock"]
    item_layout = layout.structs["struct xLIST_ITEM"]
    si = read_field(tcb, sl, "state_list_item")
    ei = read_field(tcb, sl, "event_list_item")
    # Reason: pxContainer is a List_t * pointer member; comparing list
    # membership needs the pointer value (the list address it points to), not
    # value_address() which yields the address of the pointer field itself.
    state_addr = safe_int(read_field(si, item_layout, "container")) if si else None
    event_addr = safe_int(read_field(ei, item_layout, "container")) if ei else None

    # Step 1: running task.
    if not layout.config.smp:
        current = safe_dereference(lookup_symbol("pxCurrentTCB"))
        if current is not None and value_address(current) == value_address(tcb):
            return "Running", 0
    else:
        run_state = read_int(read_field(tcb, sl, "run_state"))
        # Step 1b: scheduled-to-yield still occupies a core.
        if run_state is not None and run_state == _TASK_SCHEDULED_TO_YIELD:
            core = core_of(tcb, layout)
            if core is not None:
                return "Running(yielding)", core
            # Reason: core_of() can miss a scheduled-to-yield TCB when
            # pxCurrentTCBs is unreadable. Do not invent Ready; fall through
            # Steps 2-6 so delayed/suspended/termination membership still
            # wins, and Step 6's default returns Ready only when nothing else
            # matched (typically the task still sits on a ready list).
        if run_state is not None and 0 <= run_state < layout.config.number_of_cores:
            return "Running", run_state

    # Step 2: event item on the pending-ready list => ready.
    if event_addr is not None and event_addr == _next_global(layout.lists["pending"]):
        return "Ready", None

    # Step 3: state item on the active delayed list => blocked.
    delayed = _next_global(layout.lists["delayed_current"], deref=True)
    overflow = _next_global(layout.lists["delayed_overflow"], deref=True)
    if state_addr is not None and state_addr in (delayed, overflow):
        return "Blocked", None

    # Step 4: state item on the suspended list => blocked unless fully idle.
    suspended = _next_global(layout.lists["suspended"])
    if state_addr is not None and state_addr == suspended:
        if event_addr:
            return "Blocked", None
        if _waiting_notification(tcb, layout):
            return "Blocked", None
        return "Suspended", None

    # Step 5: state item null or on the termination list => deleted.
    termination = _next_global(layout.lists["termination"])
    # Reason: only a *confirmed* NULL container (0) or the termination list
    # means the task object is gone (eTaskGetState reports NULL as deleted,
    # tasks.c:2610-2615). An unreadable container (None) is unknown: rendering
    # it as a definite Deleted would turn every read failure into a wrong,
    # confident answer, so it falls through to the default below.
    if state_addr == 0 or state_addr == termination:
        return "Deleted", None

    # Step 6: default, SMP running check then ready.
    if layout.config.smp:
        run_state = read_int(read_field(tcb, sl, "run_state"))
        if run_state is not None and 0 <= run_state < layout.config.number_of_cores:
            return "Running", run_state
    return "Ready", None


_TASK_SCHEDULED_TO_YIELD = -2


def _waiting_notification(tcb, layout: FreeRtosLayout) -> bool:
    """Whether any notification slot is waiting (ucNotifyState == 1).

    ``ucNotifyState`` only became an array in V10.4.0
    (``configTASK_NOTIFICATION_ARRAY_ENTRIES``); before that it is a plain
    scalar member.  Subscripting the scalar raises, which would silently
    answer "not waiting" and mislabel a notification-blocked task as
    ``Suspended``, so the shape comes from the probed config.
    """
    sl = layout.structs["struct tskTaskControlBlock"]
    states = read_field(tcb, sl, "notify_state")
    if states is None:
        return False
    if not layout.config.notification_array:
        return read_int(states) == 1  # WAITING
    try:
        for index in range(layout.config.notification_count):
            if read_int(states[index]) == 1:  # WAITING
                return True
    except _TRAVERSAL_ERRORS:
        pass
    return False


def core_of(tcb, layout: FreeRtosLayout) -> int | None:
    """Return the core index for a running/yielding task, else ``None``.

    When ``xTaskRunState == -2`` (about to yield) the task still sits on a
    core, so scan ``pxCurrentTCBs[]`` for a pointer matching this TCB.
    """
    if not layout.config.smp:
        return None
    sl = layout.structs["struct tskTaskControlBlock"]
    run_state = read_int(read_field(tcb, sl, "run_state"))
    if run_state is None:
        return None
    if 0 <= run_state < layout.config.number_of_cores:
        return run_state
    if run_state == _TASK_SCHEDULED_TO_YIELD:
        tcb_addr = value_address(tcb)
        value = lookup_symbol("pxCurrentTCBs")
        for core in range(layout.config.number_of_cores):
            pointer = _array_item(value, core)
            task = safe_dereference(pointer)
            if task is not None and value_address(task) == tcb_addr:
                return core
    return None


def is_idle_task(tcb, layout: FreeRtosLayout) -> bool:
    """Return whether *tcb* is the idle task.

    SMP kernels set ``uxTaskAttributes & taskATTRIBUTE_IS_IDLE`` on the idle
    TCB; single-core kernels expose the idle handle as ``xIdleTaskHandles[0]``
    (V11 always defines the array) or the V10 scalar ``xIdleTaskHandle``.
    """
    sl = layout.structs["struct tskTaskControlBlock"]
    attributes = read_int(read_field(tcb, sl, "task_attributes"))
    if attributes is not None and (attributes & 1):  # taskATTRIBUTE_IS_IDLE
        return True
    tcb_addr = value_address(tcb)
    if lookup_symbol("xIdleTaskHandles") is not None:
        handle = _array_item(lookup_symbol("xIdleTaskHandles"), 0)
        task = safe_dereference(handle)
        return task is not None and value_address(task) == tcb_addr
    if lookup_symbol("xIdleTaskHandle") is not None:
        task = safe_dereference(lookup_symbol("xIdleTaskHandle"))
        return task is not None and value_address(task) == tcb_addr
    return False


def current_tasks(layout: FreeRtosLayout) -> list[tuple[int, int]]:
    """Return ``[(core, TCB address)]`` for the currently running tasks."""
    if layout.config.smp:
        value = lookup_symbol("pxCurrentTCBs")
        result = []
        for core in range(layout.config.number_of_cores):
            pointer = _array_item(value, core)
            task = safe_dereference(pointer)
            if task is not None:
                result.append((core, value_address(task)))
        return result
    task = safe_dereference(lookup_symbol("pxCurrentTCB"))
    return [(0, value_address(task))] if task is not None else []


def system_value(name: str) -> int | None:
    return read_int(lookup_symbol(name))


def _list_count_of(head, layout: FreeRtosLayout) -> int | None:
    try:
        return read_int(read_field(head, layout.structs["struct xLIST"], "count"))
    except _TRAVERSAL_ERRORS:
        return None


def list_count(name: str, layout: FreeRtosLayout) -> int | None:
    """Return the item count of a scheduler list, or ``None`` when unknown.

    ``pxReadyTasksLists`` is an *array* of ``List_t`` (one per priority) rather
    than a single list: GDB resolves a struct-member access on the array to
    element 0, so reading it like a plain list would silently report only the
    priority-0 count. Sum every priority instead.
    """
    if name == "ready":
        total: int | None = None
        for head in _ready_heads(layout):
            count = _list_count_of(head, layout)
            if count is None:
                return None
            total = count if total is None else total + count
        return total
    head = _head(name, layout)
    if head is None:
        return None
    return _list_count_of(head, layout)


# ---------------------------------------------------------------------------
# Object discovery (six-channel provenance model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredObject:
    """One kernel object found by a discovery channel.

    Attributes:
        kind: Semantic kind: ``"task"``, ``"queue"``, ``"semaphore"``,
            ``"mutex"``, ``"timer"``, ``"eventgroup"`` or ``"streambuffer"``.
        address: Target address of the object structure.
        name: Display name when the channel provides one (registry name,
            static symbol name, timer name); ``None`` otherwise.
        source: Discovery channel: ``"mpu-pool"``, ``"registry"``,
            ``"active"``, ``"symbol"``, ``"waiter"`` or ``"user"``.
        inferred_kind: True when the channel cannot tell the precise kind.
            Registry and MPU-pool queue slots also cover semaphores, mutexes
            and queue sets; refinement needs Queue_t's ``ucQueueType``.
    """

    kind: str
    address: int
    name: str | None = None
    source: str = ""
    inferred_kind: bool = False


# Static-buffer typedef names (include/FreeRTOS.h) -> semantic kind.  GDB
# keeps the declared typedef spelling in the symbol's type name, so a
# ``static StaticSemaphore_t`` variable stays distinguishable from
# ``StaticQueue_t`` even though both strip to the same struct.
_KIND_BY_STATIC_TYPE: dict[str, str] = {
    "StaticTask_t": "task",
    "StaticQueue_t": "queue",
    "StaticSemaphore_t": "semaphore",
    "StaticEventGroup_t": "eventgroup",
    "StaticTimer_t": "timer",
    "StaticStreamBuffer_t": "streambuffer",
    "StaticMessageBuffer_t": "streambuffer",
}

# Handle typedef names (task.h/queue.h/semphr.h/timers.h/event_groups.h/
# stream_buffer.h/message_buffer.h) -> semantic kind.  Queue-set handles are
# queues themselves and count as ``queue``.
_KIND_BY_HANDLE_TYPE: dict[str, str] = {
    "TaskHandle_t": "task",
    "QueueHandle_t": "queue",
    "QueueSetHandle_t": "queue",
    "QueueSetMemberHandle_t": "queue",
    "SemaphoreHandle_t": "semaphore",
    "TimerHandle_t": "timer",
    "EventGroupHandle_t": "eventgroup",
    "StreamBufferHandle_t": "streambuffer",
    "MessageBufferHandle_t": "streambuffer",
}


def _cstring(value, max_len: int = GDR_MAX_CSTRING_LENGTH) -> str | None:
    """Read a ``char*`` name and stop at the first NUL.

    A bounded ``Value.string(length=N)`` read carries embedded NULs plus
    whatever follows the string (GDB manual); names are C strings, so
    anything after the first NUL is filler that would poison name matching
    and table width.
    """
    raw = read_cstring(value, max_len)
    if raw is None:
        return None
    return raw.split("\x00", 1)[0]


def iter_registry_entries(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:
    """Yield registered queue-family objects from ``xQueueRegistry``.

    Scans the whole array and never stops at the first empty slot:
    ``vQueueUnregisterQueue`` leaves holes behind later entries
    (queue.c), and ``vQueueAddToRegistry`` only fills the first free slot.
    """
    if not layout.config.queue_registry:
        return
    table = lookup_symbol("xQueueRegistry")
    if table is None:
        return
    size = layout.config.queue_registry_size
    if size <= 0:
        return
    for index in range(size):
        item = _array_item(table, index)
        if item is None:
            continue
        name_value = read_path(item, ("pcQueueName",))
        handle = read_int(read_path(item, ("xHandle",)))
        # Reason: an empty slot is officially pcQueueName == NULL; requiring
        # a non-null handle too keeps a half-cleared slot from resurrecting.
        if name_value is None or not handle:
            continue
        name = _cstring(name_value)
        if not name:
            continue
        # Reason: the registry stores QueueHandle_t, which also covers
        # semaphores, mutexes and queue sets; only ucQueueType
        # (configUSE_TRACE_FACILITY) tells them apart, so the precise kind
        # is refined by the queue-discrimination phase.
        yield DiscoveredObject(
            kind="queue",
            address=handle,
            name=name,
            source="registry",
            inferred_kind=True,
        )


def _info_variables_text() -> str:
    """Return ``info variables`` output, or ``""`` when unavailable."""
    if gdb is None:
        return ""
    try:
        return gdb.execute("info variables", to_string=True) or ""
    except Exception:
        warn("symbol object scan failed: 'info variables' is unavailable")
        return ""


# GDB's ``info variables`` prints one declaration per line as
# ``<line>:	[static ][const ]<type> <name>[<dim>];``.  The type is the
# typedef spelling used at the declaration site.
_DECLARATION_RE = re.compile(
    r"^\s*(?:[0-9]+:\s*)?(?:static\s+)?(?:const\s+)?(?:volatile\s+)?"
    r"([A-Za-z_]\w*)\s+([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*;"
)


def _iter_declared_variables(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(type_name, symbol_name)`` pairs from ``info variables``.

    Stops at the ``Non-debugging symbols:`` section: those entries carry no
    type information and cannot be classified by typedef name.
    """
    for line in text.splitlines():
        if line.strip().startswith("Non-debugging symbols:"):
            return
        match = _DECLARATION_RE.match(line)
        if match is None:
            continue
        yield match.group(1), match.group(2)


def _scan_symbol_objects(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:  # noqa: ARG001
    """One pass of the symbol channel; the caller caches its result.

    The layout argument is kept for the uniform channel signature even
    though a symbol scan is layout-independent.
    """
    for type_name, symbol_name in _iter_declared_variables(_info_variables_text()):
        kind = _KIND_BY_STATIC_TYPE.get(type_name)
        if kind is None:
            kind = _KIND_BY_HANDLE_TYPE.get(type_name)
        if kind is None:
            continue
        value = lookup_symbol(symbol_name)
        if value is None:
            continue
        # Reason: a Static*_t variable IS the object buffer (tasks.c passes
        # pxTaskBuffer straight to the TCB), so its symbol address is the
        # object address; a *Handle_t variable holds a pointer to the object,
        # so the pointer value is. Mixing the two up yields fake objects in
        # .bss that still cast and read plausibly.
        if type_name in _KIND_BY_STATIC_TYPE:
            address = value_address(value)
        else:
            address = safe_int(value)
        if not address:
            continue
        yield DiscoveredObject(
            kind=kind, address=address, name=symbol_name, source="symbol"
        )


_SYMBOL_OBJECT_CACHE: tuple[DiscoveredObject, ...] | None = None


def reset_symbol_object_cache() -> None:
    """Drop the cached symbol-channel scan (tests and repeated init)."""
    global _SYMBOL_OBJECT_CACHE
    _SYMBOL_OBJECT_CACHE = None


def iter_static_symbol_objects(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:
    """Yield static-symbol objects, scanning DWARF once per session.

    The first call runs ``info variables`` and caches the result; later
    calls reuse it because the session's symbol table cannot change while
    the target is attached and the scan is the most expensive channel on
    large firmwares.  ``reset_symbol_object_cache()`` forces a fresh scan.
    """
    global _SYMBOL_OBJECT_CACHE
    if _SYMBOL_OBJECT_CACHE is None:
        _SYMBOL_OBJECT_CACHE = tuple(_scan_symbol_objects(layout))
    yield from _SYMBOL_OBJECT_CACHE


def _pointer_size() -> int:
    """Return the target pointer width in bytes (32-bit fallback)."""
    arch = get_arch_info()
    if arch is not None and arch.ptrsize in (4, 8):
        return arch.ptrsize
    warn("target pointer width unknown; assuming 32-bit for MPU object pool")
    return 4


def _value_array_bound(value) -> int | None:
    """Return the element count of an array value, or ``None``."""
    try:
        count = value.type.strip_typedefs().range()[1] + 1
    except _TRAVERSAL_ERRORS:
        return None
    return count if count > 0 else None


# ulKernelObjectType values (portable/Common/mpu_wrappers_v2.c).
_MPU_KIND_BY_TYPE: dict[int, str] = {
    1: "queue",  # also covers semaphore/mutex/queue-set/set-member
    2: "task",
    3: "streambuffer",
    4: "eventgroup",
    5: "timer",
}


def iter_mpu_pool_objects(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:
    """Yield objects from the MPU kernel object pool, if present.

    ``xKernelObjectPool`` is a file-static array (mpu_wrappers_v2.c), so the
    lookup must use the static-symbol path.  Slots with an internal handle of
    0 (empty) or ~0 (reserved while an object is mid-create) are skipped; the
    ~0 sentinel is compared at the target pointer width.
    """
    if not layout.config.mpu_object_pool:
        return
    pool = lookup_symbol("xKernelObjectPool")
    if pool is None:
        return
    count = _value_array_bound(pool)
    if count is None:
        warn("xKernelObjectPool has no decodable array bound; skipping MPU pool")
        return
    mask = (1 << (8 * _pointer_size())) - 1
    for index in range(count):
        item = _array_item(pool, index)
        if item is None:
            continue
        handle = read_int(read_path(item, ("xInternalObjectHandle",)))
        if not handle or handle == mask:
            continue
        type_code = read_int(read_path(item, ("ulKernelObjectType",)))
        if type_code is None:
            continue
        kind = _MPU_KIND_BY_TYPE.get(type_code)
        if kind is None:
            continue
        # Reason: KERNEL_OBJECT_TYPE_QUEUE also covers queue sets and set
        # members (mpu_wrappers_v2.c); only the Queue_t fields can tell, so
        # that kind is flagged inferred until the queue discriminator lands.
        yield DiscoveredObject(
            kind=kind,
            address=handle,
            name=None,
            source="mpu-pool",
            inferred_kind=type_code == 1,
        )


def _cast_owner(pointer, layout: FreeRtosLayout, struct_key: str):
    """Cast a ListItem owner pointer to a layout-described struct value."""
    try:
        if pointer is None or not int(pointer) or gdb is None:
            return None
        struct_name = layout.structs[struct_key].struct_name
        typ = gdb.lookup_type(struct_name).pointer()
        return pointer.cast(typ).dereference()
    except _TRAVERSAL_ERRORS:
        return None


def _owner_timer(pointer, layout: FreeRtosLayout):
    """Cast a timer-list item owner (``Timer_t *``) to its DWARF struct."""
    return _cast_owner(pointer, layout, "struct tmrTimerControl")


def iter_active_timer_hosts(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:
    """Yield active timers from the scheduler's two timer lists.

    ``pxCurrentTimerList`` / ``pxOverflowTimerList`` are ``static List_t *``
    (timers.c) whose list items carry a ``Timer_t *`` owner, so this channel
    replaces the TCB owner cast.  Only *active* timers are reachable:
    stopped and expired one-shot timers were unlinked and are not referenced
    by any kernel global.
    """
    if not layout.config.timers:
        return
    for head_name in ("pxCurrentTimerList", "pxOverflowTimerList"):
        head = safe_dereference(lookup_symbol(head_name))
        if head is None:
            continue
        for timer in _iter_list(head, layout, owner_converter=_owner_timer):
            name = _cstring(
                read_field(timer, layout.structs["struct tmrTimerControl"], "name")
            )
            yield DiscoveredObject(
                kind="timer", address=value_address(timer), name=name, source="active"
            )


def _scheduler_list_addresses(layout: FreeRtosLayout) -> set[int]:
    """Return the addresses of scheduler-owned List_t containers.

    Delayed-list symbols are pointers to the *active* list, so they are
    dereferenced; the other lists are plain List_t symbols.
    """
    addresses: set[int] = set()
    for key, symbol in layout.lists.items():
        address = _next_global(
            symbol, deref=(key in ("delayed_current", "delayed_overflow"))
        )
        if address:
            addresses.add(address)
    return addresses


def _host_from_container(
    container: int, struct_name: str, member: str, layout: FreeRtosLayout
):
    """Dereference the struct that owns the List_t at *container*.

    ``pxContainer`` points at the host's embedded List_t member, so the host
    address is ``container - offsetof(member)``.  Returns the host
    ``gdb.Value`` or ``None`` when the offset or cast fails.
    """
    offset = member_offset(struct_name, (member,))
    if offset is None:
        return None
    host_address = container - offset
    if host_address <= 0 or gdb is None:
        return None
    ranges = _mapped_ranges()
    # Reason: a container pointing outside every loadable section cannot
    # belong to a live object; reject before casting.  The check is skipped
    # when no map is available so unit tests with synthetic addresses work.
    if ranges and not any(low <= host_address < high for low, high in ranges):
        return None
    try:
        # Reason: the cast type comes from the layout struct description (as
        # _owner_task does), never a hard-coded literal, so a renamed struct
        # stays correct.
        struct_layout = layout.structs[struct_name]
        typ = gdb.lookup_type(struct_layout.struct_name).pointer()
        return gdb.Value(host_address).cast(typ).dereference()
    except _TRAVERSAL_ERRORS:
        return None


def _list_contains_item(
    head,
    item_address: int,
    layout: FreeRtosLayout,
    max_count: int = GDR_MAX_TRAVERSAL_COUNT,
) -> bool:
    """Whether *item_address* is a node of the List_t at *head*."""
    try:
        list_layout = layout.structs["struct xLIST"]
        end = read_field(head, list_layout, "end")
        end_address = value_address(end)
        node = read_field(end, layout.structs["struct xMINI_LIST_ITEM"], "next")
        item_layout = layout.structs["struct xLIST_ITEM"]
        for _ in range(max_count):
            node_address = safe_int(node)
            if not node_address or node_address == end_address:
                return False
            if node_address == item_address:
                return True
            node = read_field(node, item_layout, "next")
        return False
    except _TRAVERSAL_ERRORS:
        return False


def _plausible_waiter_host(
    host,
    struct_name: str,
    member: str,
    container: int,
    item_address: int,
    layout: FreeRtosLayout,
) -> bool:
    """Heuristic confirmation that *host* really owns the container list.

    container_of is an identity transform, so every candidate member
    reconstructs the container address; the discriminator is the memory
    around it: the list must actually contain the task's event list item,
    and the host struct fields must be internally consistent.  A wrong
    candidate still slips through occasionally (its fields read plausible
    neighbours), which is why this channel stays heuristic and why dedup in
    :func:`discover` prefers every earlier channel.
    """
    member_list = read_path(host, (member,))
    if member_list is None:
        return False
    if value_address(member_list) != container:
        return False
    if not _list_contains_item(member_list, item_address, layout):
        return False
    if struct_name == "struct EventGroupDef_t":
        # Reason: EventBits_t reserves the high byte for control bits
        # (eventEVENT_BITS_CONTROL_BYTES); a pointer-looking uxEventBits
        # means the "host" is really memory inside another object.
        bits = read_int(read_path(host, ("uxEventBits",)))
        return bits is not None and bits < (1 << (layout.config.tick_bits - 8))
    length = read_int(read_path(host, ("uxLength",)))
    waiting = read_int(read_path(host, ("uxMessagesWaiting",)))
    item_size = read_int(read_path(host, ("uxItemSize",)))
    pc_head = read_int(read_path(host, ("pcHead",)))
    if length is None or waiting is None or item_size is None:
        return False
    # Reason: a real queue holds 1..65536 items of at most 1 MiB each
    # (semaphores and mutexes use item size 0), and the waiting count never
    # exceeds the length; absurd neighbours therefore mean a wrong host.
    if not (
        1 <= length <= 65536 and 0 <= waiting <= length and 0 <= item_size <= 1 << 20
    ):
        return False
    # Reason: xQueueGenericCreate allocates a storage buffer whenever the
    # item size is non-zero, so a queue with items must have a non-null
    # pcHead. Zero-size queues (semaphores/mutexes) legitimately use NULL.
    if item_size != 0 and pc_head in (None, 0):
        return False
    # Reason: vListInitialise stamps xListEnd.xItemValue with portMAX_DELAY
    # on both waiting lists and nothing ever changes it, so a host whose
    # two lists do not both carry that sentinel is really memory inside a
    # neighbour object (this separates every sibling candidate from the
    # true host).
    end_mask = (1 << layout.config.tick_bits) - 1
    for sibling in ("xTasksWaitingToSend", "xTasksWaitingToReceive"):
        end_value = read_int(read_path(host, (sibling, "xListEnd", "xItemValue")))
        if end_value != end_mask:
            return False
    return True


# Waiter-channel candidates: the only object-owned lists a TCB's
# xEventListItem can sit on (queue.c / event_groups.c).
_WAITER_MEMBERS: tuple[tuple[str, str, str], ...] = (
    ("struct QueueDefinition", "xTasksWaitingToSend", "queue"),
    ("struct QueueDefinition", "xTasksWaitingToReceive", "queue"),
    ("struct EventGroupDef_t", "xTasksWaitingForBits", "eventgroup"),
)


def iter_waiter_hosts(layout: FreeRtosLayout) -> Iterator[DiscoveredObject]:
    """Reverse-discover objects from blocked tasks' event list items.

    A task blocked on a queue, semaphore, mutex or event group has its
    ``xEventListItem`` inserted into the host's waiting list with
    ``pxContainer`` pointing back at it.  Scheduler-owned lists are excluded
    first, then each candidate member offset is probed with container_of and
    confirmed by list membership plus struct plausibility.
    """
    scheduler_lists = _scheduler_list_addresses(layout)
    sl = layout.structs["struct tskTaskControlBlock"]
    item_layout = layout.structs["struct xLIST_ITEM"]
    for tcb, _state, _core in iter_tasks(layout):
        event_item = read_field(tcb, sl, "event_list_item")
        if event_item is None:
            continue
        container = safe_int(read_field(event_item, item_layout, "container"))
        # Reason: a scheduler-owned list (xPendingReadyList, the suspended
        # list, ...) is not an object's waiter list -- the task there is
        # ready or notification-blocked, not blocked on a kernel object.
        if not container or container in scheduler_lists:
            continue
        item_address = value_address(event_item)
        for struct_name, member, kind in _WAITER_MEMBERS:
            host = _host_from_container(container, struct_name, member, layout)
            if host is None:
                continue
            if not _plausible_waiter_host(
                host, struct_name, member, container, item_address, layout
            ):
                continue
            yield DiscoveredObject(
                kind=kind,
                address=value_address(host),
                name=None,
                source="waiter",
                inferred_kind=kind == "queue",
            )


def discover(kind: str, layout: FreeRtosLayout) -> list[DiscoveredObject]:
    """Collect objects of *kind* from every channel, deduplicated by address.

    Channels run in priority order (mpu-pool, registry, active, symbol,
    waiter); when two channels find the same address the earlier one keeps
    its name and source, so a registry-named queue never gets its name
    overwritten by the symbol channel.
    """
    kind = kind.strip().lower()
    channels = [
        iter_mpu_pool_objects(layout),
        iter_registry_entries(layout),
        iter_active_timer_hosts(layout),
        iter_static_symbol_objects(layout),
        iter_waiter_hosts(layout),
    ]
    seen: set[int] = set()
    found: list[DiscoveredObject] = []
    for channel in channels:
        for obj in channel:
            if obj.kind != kind or not obj.address or obj.address in seen:
                continue
            seen.add(obj.address)
            found.append(obj)
    return found


def _address_from_symbol(value) -> int | None:
    """Return the object address a Static*_t / *Handle_t symbol refers to.

    Static buffers are the object itself (symbol address); handles hold a
    pointer to the object (pointer value).
    """
    type_name = getattr(value.type, "name", None)
    if type_name in _KIND_BY_STATIC_TYPE:
        return value_address(value)
    if type_name in _KIND_BY_HANDLE_TYPE:
        return safe_int(value)
    return None


def resolve_object(
    kind: str,
    text: str,
    layout: FreeRtosLayout,  # noqa: ARG001 (uniform channel signature)
) -> DiscoveredObject | None:
    """Resolve an explicit address or symbol name to a discovered object.

    Accepts ``0x``/``0X`` hexadecimal addresses, plain decimal addresses and
    plain C identifiers (looked up as a symbol, never evaluated as an
    expression, so the inferior stays halted).
    """
    kind = kind.strip().lower()
    text = text.strip()
    if not text:
        return None
    if text.lower().startswith("0x"):
        try:
            address = int(text, 16)
        except ValueError:
            return None
        if not address:
            return None
        return DiscoveredObject(kind=kind, address=address, source="user")
    if text.isdecimal():
        address = int(text, 10)
        if not address:
            return None
        return DiscoveredObject(kind=kind, address=address, source="user")
    if is_plain_identifier(text):
        value = lookup_symbol(text)
        if value is None:
            return None
        address = _address_from_symbol(value)
        if not address:
            return None
        return DiscoveredObject(kind=kind, address=address, name=text, source="user")
    return None
