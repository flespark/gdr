"""Safe traversal of FreeRTOS scheduler lists and system globals."""

from __future__ import annotations

import re
from collections.abc import Iterator

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from gdr.constants import GDR_MAX_TRAVERSAL_COUNT
from gdr.gdb_bridge import (
    lookup_symbol,
    read_int,
    safe_dereference,
    safe_int,
    value_address,
    warn,
)
from gdr.layout import read_field

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
    try:
        if pointer is None or not int(pointer) or gdb is None:
            return None
        struct_name = layout.structs["struct tskTaskControlBlock"].struct_name
        typ = gdb.lookup_type(struct_name).pointer()
        return pointer.cast(typ).dereference()
    except _TRAVERSAL_ERRORS:
        return None


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
    head, layout: FreeRtosLayout, max_count: int = GDR_MAX_TRAVERSAL_COUNT
) -> Iterator:
    """Yield TCBs from a List_t, stopping on corruption or a bounded count."""
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
            owner = _owner_task(read_field(item, item_layout, "owner"), layout)
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
